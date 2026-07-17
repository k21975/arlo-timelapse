"""
capture.py
==========
A single capture cycle. The scheduler calls run_capture_cycle() every
CAPTURE_INTERVAL_MIN minutes. The whole function is wrapped in a try/except so
that NOTHING it does can ever crash the daemon -- a bad capture just gets logged.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import logging
import shutil
import threading

from . import arlo_client, config, daylight, notify, pause

log = logging.getLogger(__name__)

# Serializes captures so a manual dashboard "Test capture" can never run at the
# same time as the scheduled cycle and corrupt the shared failure/hash counters.
_capture_lock = threading.Lock()

# How many snapshots in a row have failed. Reset to 0 on any success.
_consecutive_failures = 0

# md5 of the last image we actually saved. Used to detect STALE snapshots: a
# degraded camera makes pyaarlo hand back the same cached frame over and over,
# and two genuinely fresh JPEGs are never byte-identical -- so an identical hash
# means the frame is stale and should be skipped, not saved.
#
# The hash is persisted to /data/state so the check survives a daemon restart
# (deploys, crashes): otherwise the first frame after any restart would be
# saved unconditionally, letting a stale cached frame into the time-lapse as a
# byte-identical duplicate. It is always the hash of the RAW camera bytes
# (computed before any timestamp is burned in), so the on-disk file stays
# comparable whatever the stamp setting is.
def _load_last_hash() -> str | None:
    try:
        text = config.LAST_FRAME_HASH_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def _store_last_hash(digest: str) -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.LAST_FRAME_HASH_FILE.write_text(digest, encoding="utf-8")
    except Exception as exc:
        log.warning("Could not persist last-frame hash: %s", exc)


_last_image_hash = _load_last_hash()

# How many cycles we have seen the camera flagged "unavailable" in a row. Drives
# the battery-protective backoff: an offline camera answers no snapshot, so each
# request just blocks for the full timeout waking its LTE radio for nothing. We
# therefore only PROBE every OFFLINE_PROBE_EVERY cycles while it's offline and
# skip the snapshot entirely in between. Reset to 0 the moment it recovers.
_offline_skips = 0


def _append_status(event: str, success: bool, battery, signal, filename: str, note: str = "") -> None:
    """Append one row to /data/logs/status.csv (writing the header if new)."""
    config.ensure_dirs()
    new_file = not config.STATUS_CSV.exists()
    try:
        with open(config.STATUS_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(
                    ["timestamp", "event", "success", "battery_pct", "signal", "filename", "note"]
                )
            writer.writerow([
                daylight.now().strftime("%Y-%m-%d %H:%M:%S"),
                event,
                "yes" if success else "no",
                battery,
                signal,
                filename,
                note,
            ])
    except Exception as exc:
        log.error("Could not write status.csv: %s", exc)


def _stamp_jpeg(image: bytes, when: dt.datetime) -> bytes:
    """Burn a date/time stamp into the bottom-left of the JPEG.

    Matches the look of compile.py's --overlay (DejaVu Bold, white text on a dark
    plate) so live snapshots and the rendered video are consistent. Defensive:
    any failure just returns the original bytes -- a stamp must never lose a photo.
    """
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        log.debug("Timestamp overlay skipped (Pillow unavailable): %s", exc)
        return image
    try:
        img = Image.open(io.BytesIO(image)).convert("RGB")
        # Scale the text to the frame so it reads on any resolution.
        size = max(18, int(img.height * 0.045))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
        except Exception:
            font = ImageFont.load_default()

        label = when.strftime("%Y-%m-%d  %H:%M")
        pad = max(6, size // 4)
        margin = max(12, size // 2)
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = margin
        y = img.height - th - margin - 2 * pad

        # Translucent dark plate behind the text so it stays readable.
        plate = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(plate).rectangle(
            [x - pad, y - pad, x + tw + pad, y + th + pad], fill=(0, 0, 0, 140)
        )
        img = Image.alpha_composite(img.convert("RGBA"), plate).convert("RGB")
        ImageDraw.Draw(img).text((x - bbox[0], y - bbox[1]), label, fill=(255, 255, 255), font=font)

        out = io.BytesIO()
        img.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as exc:
        log.warning("Could not stamp snapshot; saving original: %s", exc)
        return image


def _save_image(image: bytes, when: dt.datetime) -> str:
    """Save JPEG bytes to /data/snapshots/YYYY-MM-DD/arlo_YYYYMMDD_HHMMSS.jpg."""
    # Optionally burn the capture time into the photo (toggle in Settings).
    if str(getattr(config, "STAMP_SNAPSHOTS", "no")).strip().lower() in ("yes", "true", "1", "on"):
        image = _stamp_jpeg(image, when)
    day_dir = config.SNAP_DIR / when.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    # Sortable timestamped filename so ffmpeg ingests them in time order.
    filename = "arlo_" + when.strftime("%Y%m%d_%H%M%S") + ".jpg"
    path = day_dir / filename
    path.write_bytes(image)
    # Also keep a copy at /data/snapshots/latest.jpg for a quick eyeball check.
    try:
        shutil.copyfile(path, config.LATEST_JPG)
    except Exception:
        pass
    return str(path)


def capture_lock():
    """The lock serializing all camera work. The daemon holds it while swapping
    the Arlo session (relogin), so a session swap can never yank pyaarlo out
    from under an in-flight snapshot."""
    return _capture_lock


def consecutive_failures() -> int:
    """How many capture attempts in a row have failed (for the daemon's
    dead-session watchdog)."""
    return _consecutive_failures


def run_capture_cycle() -> str:
    """Take one snapshot if it is daytime; record status; raise alerts if needed.

    Returns "ok" (frame saved), "skip" (paused / outside the window -- nothing to
    do), or "fail" (we wanted a frame and did not get one). The daemon uses a
    "fail" to arm the catch-up retries that fill the gap once the camera recovers.
    """
    try:
        with _capture_lock:
            return _run_capture_cycle_locked()
    except Exception as exc:
        # The safety net: a capture must NEVER kill the daemon.
        log.exception("Unexpected error in capture cycle: %s", exc)
        return "fail"


def _run_capture_cycle_locked() -> str:
    """The body of one scheduled capture cycle (runs under _capture_lock)."""
    global _consecutive_failures, _last_image_hash, _offline_skips
    moment = daylight.now()

    # 0) If the dashboard has paused captures, do nothing this cycle. We skip
    #    BEFORE the daylight check and before touching the camera so a pause
    #    truly stops all activity (and saves battery). The daemon stays logged
    #    in and the daily digest still sends -- only the photo is skipped.
    if pause.is_paused():
        log.info("Captures are PAUSED (flag file present); skipping this cycle.")
        return "skip"

    # 1) Only capture during the daylight window (+ buffer).
    if not daylight.is_daylight(moment):
        log.debug("Outside daylight window; skipping capture.")
        return "skip"

    # 2) Find the camera.
    camera = arlo_client.get_camera()
    if camera is None:
        _consecutive_failures += 1
        _append_status("capture", False, "?", "?", "", "camera not found")
        _maybe_alert_failures()
        return "fail"

    # Read health now so we can log it whatever happens next. These are
    # CACHED cloud values -- reading them does NOT wake the camera radio.
    battery = getattr(camera, "battery_level", "?")
    signal = arlo_client.camera_signal(camera)["signal"]

    # 3) Arlo may flag the camera "unavailable" (its cloud-side connection
    #    state). That flag is sometimes briefly STALE right after a move or
    #    power-cycle, so a snapshot is the only reliable recovery test -- but
    #    when the camera is genuinely offline, every request blocks for the
    #    full SNAPSHOT_TIMEOUT_SEC waking its LTE radio for nothing and hands
    #    back the same stale cached frame. To protect the battery we only
    #    PROBE every OFFLINE_PROBE_EVERY cycles while it's flagged unavailable
    #    and skip the snapshot entirely in between. pyaarlo's background
    #    reconnect keeps updating the flag, so we still self-heal on recovery.
    unavailable = bool(getattr(camera, "is_unavailable", False))
    if unavailable:
        probe_every = max(1, int(getattr(config, "OFFLINE_PROBE_EVERY", 4)))
        is_probe = (_offline_skips % probe_every == 0)
        _offline_skips += 1
        if not is_probe:
            _consecutive_failures += 1
            log.info(
                "Camera offline (signal %s, battery %s%%); skipping snapshot to "
                "save battery (probing every %d cycles).",
                signal, battery, probe_every,
            )
            _append_status("capture", False, battery, signal, "", "offline (backoff)")
            notify.send_alert(
                "camera_offline",
                "Arlo Timelapse — camera OFFLINE",
                f"The camera '{config.CAMERA_NAME}' is reporting offline/unavailable.\n"
                f"Snapshots are being skipped between probes to save battery.\n"
                f"Battery: {battery}%   Signal: {signal}\n",
            )
            _maybe_alert_failures()
            _check_battery(battery)
            return "fail"
        log.warning("Camera flagged unavailable; probing with a snapshot to test recovery.")
    else:
        _offline_skips = 0

    # 4) Take the fresh snapshot. Pass the last saved hash so take_snapshot can
    #    retry through cached/duplicate frames instead of returning one immediately.
    #    A probe of an OFFLINE camera gets a single attempt only: it hands back
    #    the same cached frame on every retry, so repeats just wake its LTE radio
    #    for nothing (the daemon's catch-up retries cover the recovery instead).
    image = arlo_client.take_snapshot(camera, last_hash=_last_image_hash,
                                      attempts=1 if unavailable else None)
    if not image:
        _consecutive_failures += 1
        note = "camera unavailable" if unavailable else "snapshot failed"
        log.error("Snapshot failed (%d in a row): %s", _consecutive_failures, note)
        _append_status("capture", False, battery, signal, "", note)
        # Only raise the OFFLINE alert when the camera was flagged unavailable
        # AND we genuinely could not get an image.
        if unavailable:
            notify.send_alert(
                "camera_offline",
                "Arlo Timelapse — camera OFFLINE",
                f"The camera '{config.CAMERA_NAME}' is reporting offline/unavailable "
                f"and no snapshot could be taken.\n"
                f"Battery: {battery}%   Signal: {signal}\n",
            )
        _maybe_alert_failures()
        _check_battery(battery)
        return "fail"

    # 4b) Reject STALE snapshots: when the camera is in a degraded state it can
    #     return the SAME cached image repeatedly (pyaarlo falls back to the last
    #     image if a fresh one never arrives). That would pad the time-lapse with
    #     identical frames under new timestamps. Two real captures are never
    #     byte-identical, so an identical hash => stale; skip it and count it as a
    #     failure (so the usual consecutive-failure alert eventually fires).
    digest = hashlib.md5(image).hexdigest()
    if digest == _last_image_hash:
        _consecutive_failures += 1
        log.warning(
            "Snapshot identical to the previous frame (stale cached image); "
            "skipping (%d in a row).", _consecutive_failures,
        )
        # Say WHY in the status note: an offline probe never really had a chance,
        # while a "healthy" camera handing back its cache means the fresh shot
        # lost the race against the snapshot timeout.
        note = ("offline probe: cached frame only" if unavailable
                else "no fresh frame within timeout (got cached frame)")
        _append_status("capture", False, battery, signal, "", note)
        _maybe_alert_failures()
        _check_battery(battery)
        return "fail"

    # 5) Success -> save it, remember its hash, and reset the failure counter.
    path = _save_image(image, moment)
    _last_image_hash = digest
    _store_last_hash(digest)
    _consecutive_failures = 0
    _offline_skips = 0  # camera answered -> back to normal cadence
    log.info("Saved snapshot %s (battery %s%%, signal %s)", path, battery, signal)
    _append_status("capture", True, battery, signal, path, "")

    # 6) Even on success, warn if the battery is getting low.
    _check_battery(battery)
    return "ok"


def run_manual_capture() -> dict:
    """Force ONE snapshot right now for the dashboard's "Test capture" button.

    Unlike the scheduled cycle, this deliberately IGNORES pause, the daylight
    window and the offline backoff -- the whole point is to wake the camera on
    demand and confirm the snapshot pipeline works. It still records a status row,
    updates latest.jpg on success, and never throws (returns a result dict for the
    web UI instead). Runs under the same lock as the scheduled cycle.
    """
    global _consecutive_failures, _last_image_hash, _offline_skips
    try:
        with _capture_lock:
            moment = daylight.now()
            camera = arlo_client.get_camera()
            if camera is None:
                _append_status("capture", False, "?", "?", "", "manual test: camera not found")
                return {"ok": False, "note": "Camera not found — check the camera name in Settings."}

            battery = getattr(camera, "battery_level", "?")
            signal = arlo_client.camera_signal(camera)["signal"]
            unavailable = bool(getattr(camera, "is_unavailable", False))
            log.info("Manual test capture requested (battery %s%%, signal %s, unavailable=%s).",
                     battery, signal, unavailable)

            # Same single-attempt rule as the scheduled cycle when the camera is
            # flagged offline: retries only re-fetch the same cached frame, and
            # the UI promises an answer in about a minute, not five.
            image = arlo_client.take_snapshot(camera, last_hash=_last_image_hash,
                                              attempts=1 if unavailable else None)
            if not image:
                note = "manual test: camera unavailable" if unavailable else "manual test: snapshot failed"
                _append_status("capture", False, battery, signal, "", note)
                _check_battery(battery)
                return {"ok": False, "battery": battery, "signal": signal,
                        "note": "Camera did not return an image (offline or timed out)."}

            # If the camera handed back the same bytes as the last saved frame it
            # is a stale cached image; report it honestly and don't pad the
            # time-lapse with a duplicate.
            digest = hashlib.md5(image).hexdigest()
            if digest == _last_image_hash:
                note = ("manual test: offline, cached frame only" if unavailable
                        else "manual test: no fresh frame within timeout")
                _append_status("capture", False, battery, signal, "", note)
                _check_battery(battery)
                return {"ok": False, "battery": battery, "signal": signal,
                        "note": ("Arlo flags the camera offline and it only sent its cached frame."
                                 if unavailable else
                                 "No fresh image arrived within the snapshot timeout (got the cached frame).")}

            path = _save_image(image, moment)
            _last_image_hash = digest
            _store_last_hash(digest)
            _consecutive_failures = 0
            _offline_skips = 0
            log.info("Manual test snapshot saved %s (battery %s%%, signal %s).", path, battery, signal)
            _append_status("capture", True, battery, signal, path, "manual test")
            _check_battery(battery)
            return {"ok": True, "battery": battery, "signal": signal,
                    "note": "Fresh snapshot captured and saved."}
    except Exception as exc:
        log.exception("Manual test capture failed: %s", exc)
        return {"ok": False, "note": f"Error: {exc}"}


def _maybe_alert_failures() -> None:
    """Alert once we've hit CONSECUTIVE_FAIL_ALERT failures in a row."""
    if _consecutive_failures >= config.CONSECUTIVE_FAIL_ALERT:
        notify.send_alert(
            "consecutive_failures",
            f"Arlo Timelapse — {_consecutive_failures} snapshot failures in a row",
            f"The last {_consecutive_failures} snapshot attempts for "
            f"'{config.CAMERA_NAME}' failed.\nCheck the camera and the logs.\n",
        )


def _check_battery(battery) -> None:
    """Send a low-battery alert (cooldown-limited) but keep capturing as normal."""
    try:
        if battery != "?" and float(battery) < config.BATTERY_ALERT_PCT:
            notify.send_alert(
                "low_battery",
                f"Arlo Timelapse — battery low ({battery}%)",
                f"Camera '{config.CAMERA_NAME}' battery is at {battery}% "
                f"(threshold {config.BATTERY_ALERT_PCT}%).\n"
                f"Captures continue as normal; solar should recover it.\n",
            )
    except (TypeError, ValueError):
        pass
