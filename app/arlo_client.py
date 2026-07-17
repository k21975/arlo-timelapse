"""
arlo_client.py
==============
Everything that talks to Arlo's cloud through the `pyaarlo` library lives here.

The golden rule (see README): we LOG IN ONCE and keep the session alive. We do
NOT log in on every capture -- that would trigger Arlo's 2FA over and over and
get the account rate-limited or locked.

How the session survives restarts:
    pyaarlo can save its login/session state to disk (`storage_dir` + `save_state`).
    We point that at /data/state. As long as that folder persists (it lives on
    your UnRaid volume), a container restart reuses the existing "trusted" session
    and does NOT need a new 2FA code.

How the 2FA code is read:
    With tfa_source="imap" + tfa_type="email", pyaarlo logs into the dedicated
    Gmail over IMAP and reads the 6-digit code itself. No human needed.

How the session is refreshed:
    reconnect_every (minutes) tells pyaarlo to re-login periodically in the
    background, BEFORE Arlo's ~2-hour token expires.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time

import pyaarlo

from . import config

log = logging.getLogger(__name__)

# Module-level handle to the logged-in PyArlo object (set by login()).
_arlo = None

# Serializes relogin() so two callers can't tear down / rebuild concurrently.
_relogin_lock = threading.Lock()


def login(interactive: bool = False):
    """
    Log in to Arlo and return the PyArlo object.

    interactive=False : read the 2FA code automatically from Gmail over IMAP.
    interactive=True  : have pyaarlo prompt on the console for the 2FA code
                        (used by login_once.py as a fallback if IMAP fails).
    """
    global _arlo

    # Settings common to both modes.
    kwargs = dict(
        username=config.ARLO_USERNAME,
        password=config.ARLO_PASSWORD,
        # --- where the trusted session is saved so restarts skip 2FA ---
        storage_dir=str(config.STATE_DIR),
        save_state=True,
        # --- refresh the session before Arlo's ~2h token expires ---
        reconnect_every=config.RECONNECT_EVERY_MIN,
        # --- be patient: the Go 2 over LTE is slower than a wired camera ---
        request_timeout=60,
        stream_timeout=120,
        # pyaarlo ALSO has its own internal snapshot kill-switch (default 60s)
        # that aborts the request regardless of the timeout passed to
        # get_snapshot(). It must match our setting, or a camera that needs
        # 60-90s to wake + shoot + upload over LTE loses the race every time
        # and we get handed the cached previous frame instead.
        snapshot_timeout=config.SNAPSHOT_TIMEOUT_SEC,
        # Re-read the device records from Arlo's CLOUD once an hour so cached
        # attributes (battery, signal, connectivity) can't fossilize between
        # push events. This is a plain HTTPS call to Arlo's database -- it never
        # wakes the camera's radio, so it costs the battery nothing.
        refresh_devices_every=1,
    )

    if interactive:
        # Fallback: type the emailed code yourself at the terminal.
        kwargs.update(tfa_source="console", tfa_type="email")
    else:
        # Normal: read the code automatically from the dedicated Gmail inbox.
        kwargs.update(
            tfa_source="imap",
            tfa_type="email",
            tfa_host=config.IMAP_HOST,
            tfa_username=config.IMAP_USERNAME,
            tfa_password=config.IMAP_PASSWORD,
        )

    log.info("Logging in to Arlo as %s ...", config.ARLO_USERNAME)
    _arlo = pyaarlo.PyArlo(**kwargs)

    # By default, STOP pyaarlo pinging the camera every 60 seconds. pyaarlo
    # deliberately never pings own-base battery cameras ("can rapidly drain the
    # battery") -- but the Go 2's SOLAR charger makes has_charger True, which
    # defeats that protection, so it gets pinged like a mains-powered hub. The
    # pings only maintain the (unreliable) available/unavailable flag; the Arlo
    # LOGIN session is kept alive separately (reconnect_every + saved state),
    # so 2FA behaviour is unchanged.
    if str(getattr(config, "KEEPALIVE_PINGS", "no")).strip().lower() not in ("yes", "true", "1", "on"):
        _disable_keepalive_pings(_arlo)

    # pyaarlo sets .is_connected once it has tried to log in. If it is explicitly
    # False, the login did not work. (If the attribute is missing we don't treat
    # that as fatal -- the camera lookup later will reveal real problems.)
    if getattr(_arlo, "is_connected", True) is False:
        raise RuntimeError("Arlo login did not connect (check credentials / 2FA / IMAP).")

    log.info("Arlo login OK. Cameras found: %s", [c.name for c in _arlo.cameras])
    return _arlo


def get_arlo():
    """Return the current PyArlo object (login() must have been called first)."""
    if _arlo is None:
        raise RuntimeError("Arlo is not logged in yet; call login() first.")
    return _arlo


def relogin() -> bool:
    """Tear down a (presumed dead) Arlo session and log in fresh.

    pyaarlo's own `reconnect_every` only restarts the EVENT STREAM -- it never
    re-authenticates the REST token. When that token dies, every REST call
    quietly fails ("No devices returned...") and every snapshot request serves
    the cached frame, forever. The only cure is a full fresh login -- the same
    thing a container restart does.

    2FA-safe: stop(logout=False) leaves the server-side session valid, and
    login() reuses the trusted session saved in /data/state -- identical to a
    restart, which has never triggered a 2FA prompt. Returns True on success;
    on failure the old (dead) session object is left in place so cached reads
    still work, and the caller can simply try again later.
    """
    global _arlo
    with _relogin_lock:
        old = _arlo
        try:
            if old is not None:
                # Required before building a new PyArlo in the same process,
                # or the old instance's ghost threads fight the new connection.
                old.stop(logout=False)
        except Exception as exc:
            log.warning("Old Arlo session did not stop cleanly (continuing): %s", exc)
        try:
            login(interactive=False)
            log.info("Arlo re-login OK; session is fresh.")
            return True
        except Exception as exc:
            log.error("Arlo re-login failed (will retry later): %s", exc)
            _arlo = old  # keep the old object so cached reads don't crash
            return False


def get_camera():
    """
    Find our camera by name. We look it up FRESH every cycle (a cheap dictionary
    lookup) so we keep working even after a background reconnect replaces objects.
    Returns the camera object, or None if it cannot be found.
    """
    arlo = get_arlo()
    camera = arlo.lookup_camera_by_name(config.CAMERA_NAME)
    if camera is None:
        log.error(
            "Camera named %r not found. Available cameras: %s",
            config.CAMERA_NAME, [c.name for c in arlo.cameras],
        )
    return camera


def _disable_keepalive_pings(arlo) -> None:
    """Remove the ping capability from every 'base station' (the Go 2 is its
    own), so pyaarlo's 60s _fast_refresh loop skips it. Instance-level override
    only -- everything else has_capability answers is passed straight through."""
    try:
        from pyaarlo.constant import PING_CAPABILITY
    except Exception as exc:
        log.warning("Could not disable keepalive pings (pyaarlo layout changed?): %s", exc)
        return
    for base in getattr(arlo, "base_stations", []) or []:
        original = base.has_capability

        def _no_ping(cap, _orig=original):
            return False if cap == PING_CAPABILITY else _orig(cap)

        base.has_capability = _no_ping
        log.info("60s keepalive pings DISABLED for %s (battery protection; "
                 "login/session refresh unaffected).", base.name)


def camera_signal(camera) -> dict:
    """The camera's REAL radio status, dug out of the cloud device record.

    `camera.signal_strength` reads the record's top-level "signalStrength" --
    which on the Go 2 is its DISABLED WiFi radio (permanently 1). The actual
    modem lives in properties.connectivity[], one entry per radio; we pick the
    connected/enabled one (LTE on the Go 2), which matches what the Arlo app
    shows. Returns {"signal", "type", "band", "carrier", "connected"} with
    "?"/None fallbacks -- never raises.
    """
    info = {"signal": "?", "type": "?", "band": None, "carrier": None, "connected": None}
    try:
        attrs = getattr(camera, "_attrs", {}) or {}
        radios = attrs.get("properties", {}).get("connectivity", [])
        best = None
        if isinstance(radios, list):
            for r in radios:
                if isinstance(r, dict) and (r.get("connected") or r.get("radioMode") == "enable"):
                    best = r
                    if r.get("connected"):
                        break  # a connected radio beats a merely-enabled one
        if best is not None:
            info["signal"] = best.get("signalStrength", "?")
            info["type"] = best.get("type", "?")
            if best.get("type") == "lte" and best.get("rfBand"):
                info["band"] = f"LTE B{best['rfBand']}"
            carrier = (best.get("networkName") or best.get("carrierFw") or "").strip()
            info["carrier"] = carrier or None
            info["connected"] = bool(best.get("connected"))
        if info["signal"] in ("?", None):
            info["signal"] = getattr(camera, "signal_strength", "?")
    except Exception as exc:
        log.debug("Could not read radio connectivity: %s", exc)
    return info


def take_snapshot(camera, last_hash: str | None = None,
                  attempts: int | None = None) -> bytes | None:
    """
    Ask the camera for a FRESH snapshot and return the JPEG bytes, or None.

    We try up to `attempts` times (default CAPTURE_RETRIES) with exponential
    backoff (5s, 15s, 45s...) because a single LTE hiccup should not count as a
    real failure. The caller passes attempts=1 when the camera is flagged
    offline: a struggling camera hands back the same cached frame on every try,
    so extra attempts just wake its LTE radio for nothing.

    `last_hash` is the md5 of the frame we last saved. A degraded camera times
    out internally and pyaarlo hands back its cached `last_image` -- byte-for-byte
    identical to that frame. We treat such a stale/duplicate frame as a soft
    failure and retry, since a later attempt may still produce a fresh capture.
    If every attempt is stale, we return the stale bytes anyway (not None) so the
    caller's own duplicate check can report it honestly as "stale/duplicate".
    """
    tries = max(1, int(attempts if attempts is not None else config.CAPTURE_RETRIES))
    delay = 5
    stale_image = None  # last cached/duplicate frame seen; returned only as a last resort
    for attempt in range(1, tries + 1):
        try:
            log.debug("Snapshot attempt %d/%d", attempt, tries)
            # get_snapshot() requests a NEW snapshot AND waits for it, returning
            # the image as bytes. `timeout` is how long we wait for the camera.
            image = camera.get_snapshot(timeout=config.SNAPSHOT_TIMEOUT_SEC)
            if image:
                image = bytes(image)  # pyaarlo returns a bytearray; make it bytes
                if last_hash is not None and hashlib.md5(image).hexdigest() == last_hash:
                    log.warning(
                        "Snapshot attempt %d returned a stale/duplicate frame; retrying.",
                        attempt,
                    )
                    stale_image = image
                else:
                    return image
            else:
                log.warning("Snapshot attempt %d returned no image.", attempt)
        except Exception as exc:
            log.warning("Snapshot attempt %d raised: %s", attempt, exc)

        if attempt < tries:
            time.sleep(delay)
            delay *= 3  # exponential backoff: 5 -> 15 -> 45 ...
    # No fresh frame after all retries. Hand back the stale one (if we ever got an
    # image) so the caller reports "stale/duplicate"; otherwise None ("no image").
    return stale_image
