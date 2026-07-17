"""
config.py
=========
ONE place that reads EVERY setting, fills in sensible defaults, validates the
things we cannot run without, and makes sure the folders we write into exist.

Where settings come from (highest priority first):
    1. The SETTINGS FILE  -> /data/state/settings.json
       This is what the WEB DASHBOARD writes when you change something in the UI.
       It lives on the persistent /data volume, so your changes survive restarts.
    2. ENVIRONMENT VARIABLES (the .env file in Docker -- see .env.example)
       These provide the initial values, especially the secrets.
    3. The built-in DEFAULT baked into SETTINGS_SCHEMA below.

Why a file on top of env vars?
    Secrets (passwords) still come from .env so they never live in git. But you
    asked to be able to change EVERY variable from the web page -- so anything you
    set there is written to settings.json and quietly wins over the env value.

If a REQUIRED value is missing, `validate_required()` stops the program with a
clear message instead of crashing in a confusing way later on.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# When you run compile.py or login_once.py BY HAND (outside Docker) there are no
# environment variables set in your shell. python-dotenv lets those scripts read
# a local ".env" file automatically. Inside Docker the variables already exist,
# so this is just a harmless convenience.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # python-dotenv is optional; ignore if it is not installed.
    pass

log = logging.getLogger(__name__)


# ===========================================================================
# THE SETTINGS SCHEMA
# ---------------------------------------------------------------------------
# This single list is the ONE source of truth for every editable setting. It
# drives three things at once:
#     1. the actual config values (computed in _compute() below),
#     2. the validation when the web UI saves a change,
#     3. the form the web dashboard draws.
#
# Each field is a dict:
#     key      - the variable name (also the environment-variable name)
#     label    - friendly name shown in the UI
#     group    - which section of the settings form it belongs to
#     type     - "str" | "int" | "float" | "secret" | "time" | "choice"
#     default  - value used when neither the file nor the environment sets it
#     help     - one-line explanation shown under the field
#     secret   - True for passwords: never sent to the browser; blank = keep
#     restart  - True if a change only takes effect after a daemon restart
#     choices  - allowed values for type "choice"
#     min/max  - optional bounds for "int"/"float"
# ===========================================================================
SETTINGS_SCHEMA = [
    {
        "group": "Site title",
        "fields": [
            {"key": "SITE_TITLE_LINE1", "label": "Title line 1", "type": "str", "default": "ARLO TIME",
             "help": "Top line of the big title (also used for the browser tab and link preview)."},
            {"key": "SITE_TITLE_LINE2", "label": "Title line 2", "type": "str", "default": "LAPSE CAPTURE",
             "help": "Bottom line of the big title. Leave blank for a single-line title."},
        ],
    },
    {
        "group": "Camera & Capture",
        "fields": [
            {"key": "CAMERA_NAME", "label": "Camera name", "type": "str", "default": None,
             "help": "Exactly as it appears in the Arlo app (case & spaces matter)."},
            {"key": "CAPTURE_INTERVAL_MIN", "label": "Photo interval (minutes)", "type": "int",
             "default": 15, "min": 1, "max": 1440,
             "help": "How often a photo is taken during the capture window."},
            {"key": "SNAPSHOT_TIMEOUT_SEC", "label": "Snapshot timeout (seconds)", "type": "int",
             "default": 90, "min": 5, "max": 600, "restart": True,
             "help": "How long to wait for the camera — the Go 2 needs 60-90s to wake, shoot "
                     "and upload over LTE. Takes full effect on restart."},
            {"key": "CAPTURE_RETRIES", "label": "Snapshot retries", "type": "int",
             "default": 3, "min": 1, "max": 10,
             "help": "How many times to retry one snapshot before counting it a failure."},
            {"key": "STAMP_SNAPSHOTS", "label": "Timestamp on photos", "type": "choice",
             "default": "no", "choices": ["yes", "no"],
             "help": "Burn the date & time into each saved photo. Leave OFF (recommended) to keep "
                     "originals clean and add the timestamp only when you build the video."},
        ],
    },
    {
        "group": "Capture window",
        "fields": [
            {"key": "CAPTURE_START", "label": "Fixed start time (HH:MM)", "type": "time", "default": "",
             "help": "Optional. Leave BLANK to use sunrise. Set both start & end to use fixed times."},
            {"key": "CAPTURE_END", "label": "Fixed end time (HH:MM)", "type": "time", "default": "",
             "help": "Optional. Leave BLANK to use sunset. Set both start & end to use fixed times."},
            {"key": "DAYLIGHT_BUFFER_MIN", "label": "Daylight buffer (minutes)", "type": "int",
             "default": 30, "min": 0, "max": 240,
             "help": "Start this long AFTER sunrise and stop this long BEFORE sunset (ignored if fixed times are set)."},
            {"key": "LATITUDE", "label": "Latitude", "type": "float", "default": 42.360,
             "min": -90, "max": 90, "help": "Used to compute sunrise/sunset."},
            {"key": "LONGITUDE", "label": "Longitude", "type": "float", "default": -71.059,
             "min": -180, "max": 180, "help": "Used to compute sunrise/sunset."},
            {"key": "TZ", "label": "Time zone", "type": "str", "default": "America/New_York",
             "restart": True, "help": "IANA name, e.g. America/New_York. Takes effect on restart."},
        ],
    },
    {
        "group": "Alerts & daily email",
        "fields": [
            {"key": "BATTERY_ALERT_PCT", "label": "Low-battery alert (%)", "type": "int",
             "default": 20, "min": 0, "max": 100,
             "help": "Email a warning when battery drops below this percentage."},
            {"key": "CONSECUTIVE_FAIL_ALERT", "label": "Failures before alert", "type": "int",
             "default": 3, "min": 1, "max": 50,
             "help": "Email after this many snapshot failures in a row."},
            {"key": "ALERT_COOLDOWN_HOURS", "label": "Alert cooldown (hours)", "type": "int",
             "default": 6, "min": 0, "max": 168,
             "help": "Don't re-send the same kind of alert until this many hours pass."},
            {"key": "DAILY_DIGEST_ENABLED", "label": "Send daily digest email", "type": "choice",
             "default": "yes", "choices": ["yes", "no"],
             "help": "Email a daily summary (battery, photo count, latest frame). Set to \"no\" to "
                     "stop the nightly email. Failure/low-battery alerts are unaffected."},
            {"key": "DAILY_DIGEST_TIME", "label": "Daily digest time", "type": "str", "default": "sunset",
             "help": "\"sunset\" sends at sunset each day, or a fixed time like \"20:30\". "
                     "Ignored when the daily digest is turned off above."},
            {"key": "ALERT_EMAIL", "label": "Send alerts to", "type": "str", "default": None,
             "help": "Where alerts and the daily digest are emailed."},
            {"key": "FROM_EMAIL", "label": "From address", "type": "str", "default": "",
             "help": "Optional \"From\" address. Blank = use the SMTP username."},
        ],
    },
    {
        "group": "Arlo account",
        "fields": [
            {"key": "ARLO_USERNAME", "label": "Arlo username", "type": "str", "default": None,
             "restart": True, "help": "Use a DEDICATED sub-account. Takes effect on restart."},
            {"key": "ARLO_PASSWORD", "label": "Arlo password", "type": "secret", "default": None,
             "restart": True, "help": "Stored on the /data volume. Takes effect on restart."},
        ],
    },
    {
        "group": "Email (IMAP for 2FA + SMTP for sending)",
        "fields": [
            {"key": "IMAP_HOST", "label": "IMAP host", "type": "str", "default": "imap.gmail.com",
             "restart": True, "help": "Inbox that receives the Arlo 2FA code."},
            {"key": "IMAP_USERNAME", "label": "IMAP username", "type": "str", "default": None,
             "restart": True, "help": "Takes effect on the next login / restart."},
            {"key": "IMAP_PASSWORD", "label": "IMAP password", "type": "secret", "default": None,
             "restart": True, "help": "Gmail 16-char App Password. Takes effect on restart."},
            {"key": "SMTP_HOST", "label": "SMTP host", "type": "str", "default": "smtp.gmail.com",
             "help": "Server used to SEND alert + digest email."},
            {"key": "SMTP_PORT", "label": "SMTP port", "type": "int", "default": 465,
             "min": 1, "max": 65535, "help": "465 = SSL, 587 = STARTTLS."},
            {"key": "SMTP_USERNAME", "label": "SMTP username", "type": "str", "default": None,
             "help": "The account email is sent from."},
            {"key": "SMTP_PASSWORD", "label": "SMTP password", "type": "secret", "default": None,
             "help": "Gmail 16-char App Password."},
        ],
    },
    {
        "group": "System",
        "fields": [
            {"key": "RECONNECT_EVERY_MIN", "label": "Reconnect every (minutes)", "type": "int",
             "default": 90, "min": 10, "max": 240,
             "help": "Do a full fresh Arlo login this often. The token dies ~2 hours after "
                     "login and pyaarlo can't refresh it, so keep this under 120. 2FA-safe."},
            {"key": "OFFLINE_PROBE_EVERY", "label": "Offline probe cadence (cycles)", "type": "int",
             "default": 4, "min": 1, "max": 96,
             "help": "When the camera is offline, only attempt a snapshot every Nth capture "
                     "cycle (the rest are skipped to save battery). 1 = try every cycle."},
            {"key": "KEEPALIVE_PINGS", "label": "60s camera keepalive pings", "type": "choice",
             "default": "no", "choices": ["no", "yes"], "restart": True,
             "help": "pyaarlo pings the camera every 60s to keep it flagged \"available\". The Go 2 "
                     "is its own base station, so each ping wakes its LTE radio — draining the "
                     "battery and fighting power-save mode (the solar charger tricks pyaarlo into "
                     "thinking it is mains-powered). Leave OFF to let the camera sleep. The Arlo "
                     "login/2FA session refresh is separate and unaffected."},
            {"key": "LOG_LEVEL", "label": "Log level", "type": "choice", "default": "INFO",
             "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
             "help": "How chatty the logs are."},
        ],
    },
]

# A flat {key: field} lookup, built once from the schema above.
FIELDS_BY_KEY = {f["key"]: f for group in SETTINGS_SCHEMA for f in group["fields"]}

# Settings the DAEMON cannot run without (compile.py does NOT need these).
REQUIRED_FOR_DAEMON = [
    "ARLO_USERNAME", "ARLO_PASSWORD", "CAMERA_NAME",
    "IMAP_USERNAME", "IMAP_PASSWORD",
    "SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_EMAIL",
]


# ===========================================================================
# FIXED PATHS (not editable from the UI -- changing where data lives at runtime
# would be dangerous). These are computed once from DATA_DIR.
# ===========================================================================
# The dashboard port is deliberately NOT editable from the UI: the container's
# published port is fixed at create-time by docker-compose (from the .env value),
# and compose never reads settings.json -- so changing it at runtime could only
# break access. Set it in .env instead. It is read here from the environment.
try:
    WEB_PORT = int(os.environ.get("WEB_PORT", "").strip() or "8080")
except ValueError:
    WEB_PORT = 8080

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data").strip() or "/data")
SNAP_DIR = DATA_DIR / "snapshots"
STATE_DIR = DATA_DIR / "state"
LOG_DIR = DATA_DIR / "logs"
OUTPUT_DIR = DATA_DIR / "output"

STATUS_CSV = LOG_DIR / "status.csv"
APP_LOG = LOG_DIR / "app.log"
LATEST_JPG = SNAP_DIR / "latest.jpg"
ALERT_STATE_FILE = STATE_DIR / "alert_cooldowns.json"

# When this file EXISTS, captures are paused (see pause.py).
PAUSE_FLAG = STATE_DIR / "paused.flag"

# md5 of the last frame we saved, persisted so the stale-frame check survives a
# container restart (see capture.py). Without it, the first snapshot after every
# restart would be saved unconditionally -- even a stale duplicate.
LAST_FRAME_HASH_FILE = STATE_DIR / "last_frame_hash.txt"

# Where the web dashboard writes the settings you change in the UI.
SETTINGS_FILE = STATE_DIR / "settings.json"


# ===========================================================================
# READING + COERCING VALUES
# ===========================================================================
def _coerce(field: dict, raw):
    """
    Turn a raw value (string from env/file, or already-typed from JSON) into the
    proper Python type for this field. Raises ValueError on bad input.
    """
    t = field["type"]
    if raw is None:
        return None

    if t in ("str", "secret", "time", "choice"):
        value = str(raw).strip()
        if t == "choice" and value and value not in field.get("choices", []):
            raise ValueError(f"{field['key']} must be one of {field['choices']}")
        if t == "time" and value:
            _parse_hhmm(value, field["key"])  # validate "HH:MM"
        return value

    if t == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{field['key']} must be a whole number")
        _check_bounds(field, value)
        return value

    if t == "float":
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{field['key']} must be a number")
        _check_bounds(field, value)
        return value

    return raw


def _check_bounds(field: dict, value) -> None:
    if "min" in field and value < field["min"]:
        raise ValueError(f"{field['key']} must be >= {field['min']}")
    if "max" in field and value > field["max"]:
        raise ValueError(f"{field['key']} must be <= {field['max']}")


def _parse_hhmm(text: str, key: str = "time") -> tuple[int, int]:
    """Validate/parse a "HH:MM" 24-hour string. Raises ValueError if malformed."""
    try:
        hh, mm = text.split(":")
        h, m = int(hh), int(mm)
    except Exception:
        raise ValueError(f"{key} must look like HH:MM (e.g. 06:30)")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"{key} must be a valid 24-hour time (00:00–23:59)")
    return h, m


def _load_overrides() -> dict:
    """Read the UI-written settings.json, or {} if it is missing/unreadable."""
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Could not read settings file %s: %s", SETTINGS_FILE, exc)
    return {}


# Holds whatever the UI has saved. Refreshed by reload().
_overrides: dict = _load_overrides()


def _resolve_raw(key: str):
    """The raw (uncoerced) value for `key`: override file → env → default."""
    if key in _overrides and _overrides[key] not in (None, ""):
        return _overrides[key]
    env = os.environ.get(key, "").strip()
    if env:
        return env
    return FIELDS_BY_KEY[key].get("default")


def _compute() -> None:
    """
    (Re)compute every setting and publish it as a module-level constant, so the
    rest of the app can keep using `config.CAMERA_NAME` etc. Called at import and
    again by reload() whenever the UI saves a change.
    """
    for key, field in FIELDS_BY_KEY.items():
        try:
            globals()[key] = _coerce(field, _resolve_raw(key))
        except ValueError:
            # A bad stored value should never crash startup; fall back to default.
            globals()[key] = field.get("default")

    # FROM_EMAIL defaults to the SMTP username when left blank.
    if not globals().get("FROM_EMAIL"):
        globals()["FROM_EMAIL"] = globals().get("SMTP_USERNAME")


# Compute everything right now so the constants exist at import time.
_compute()


# ===========================================================================
# RELOADING: let other parts of the app react when settings change at runtime.
# ===========================================================================
_reload_callbacks: list = []


def on_reload(callback) -> None:
    """Register a function to be called after settings are reloaded (e.g. the
    daemon re-schedules its capture job)."""
    _reload_callbacks.append(callback)


def reload() -> None:
    """Re-read settings.json, recompute all constants, and notify listeners."""
    global _overrides
    _overrides = _load_overrides()
    _compute()

    # Apply the log level live (the rest of the values are read live elsewhere).
    try:
        logging.getLogger().setLevel(getattr(logging, str(LOG_LEVEL).upper(), logging.INFO))
    except Exception:
        pass

    for cb in list(_reload_callbacks):
        try:
            cb()
        except Exception as exc:
            log.error("A reload callback failed: %s", exc)


# ===========================================================================
# SAVING a change from the web UI.
# ===========================================================================
def public_settings() -> list:
    """
    Return the schema with each field's CURRENT value filled in, for the web UI.
    Secrets are NEVER included -- only a flag saying whether one is set.
    """
    out = []
    for group in SETTINGS_SCHEMA:
        fields = []
        for f in group["fields"]:
            entry = {
                "key": f["key"], "label": f["label"], "type": f["type"],
                "help": f.get("help", ""), "restart": bool(f.get("restart")),
            }
            if "choices" in f:
                entry["choices"] = f["choices"]
            if f["type"] == "secret":
                entry["is_set"] = bool(globals().get(f["key"]))
                entry["value"] = ""  # never reveal the secret
            else:
                value = globals().get(f["key"])
                entry["value"] = "" if value is None else value
            fields.append(entry)
        out.append({"group": group["group"], "fields": fields})
    return out


def update_settings(incoming: dict) -> list[str]:
    """
    Validate and persist settings sent from the web UI, then reload().

    Rules:
      * A SECRET field that arrives BLANK is left unchanged (we never overwrite a
        password with an empty value, since the UI can't show the old one).
      * A NON-secret field that arrives BLANK is cleared (reverts to env/default).
      * Anything invalid raises ValueError and NOTHING is saved.

    Returns the list of keys that were actually changed.
    """
    overrides = _load_overrides()
    changed: list[str] = []

    for key, raw in incoming.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            continue  # ignore unknown keys

        is_blank = (raw is None) or (str(raw).strip() == "")

        if field["type"] == "secret":
            if is_blank:
                continue  # keep the existing secret (we never showed it)
            overrides[key] = _coerce(field, raw)  # may raise ValueError
            changed.append(key)
        elif is_blank:
            # Clearing a field reverts it to the env value / built-in default.
            if key in overrides:
                overrides.pop(key)
                changed.append(key)
        else:
            new_value = _coerce(field, raw)  # may raise ValueError
            current = _coerce(field, _resolve_raw(key))
            if new_value == current and key not in overrides:
                continue  # unchanged default/env value -- don't freeze it
            if overrides.get(key) != new_value:
                overrides[key] = new_value
                changed.append(key)

    # Persist atomically-ish (write to a temp file then replace).
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)

    reload()
    return changed


# ===========================================================================
# FOLDERS + REQUIRED-VALUE VALIDATION (unchanged behaviour).
# ===========================================================================
def ensure_dirs() -> None:
    """Create the /data sub-folders if they do not already exist."""
    for d in (SNAP_DIR, STATE_DIR, LOG_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def validate_required() -> None:
    """Stop the program with a clear message if any required value is missing."""
    missing = [name for name in REQUIRED_FOR_DAEMON if not globals().get(name)]
    if missing:
        print(
            "ERROR: the following required settings are not set:\n"
            + "\n".join(f"  - {name}" for name in missing)
            + "\n\nFill them in your .env file (see .env.example), or set them in\n"
            + "the web dashboard's Settings panel, and try again.",
            file=sys.stderr,
        )
        sys.exit(1)
