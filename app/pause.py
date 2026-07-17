"""
pause.py
========
One tiny, shared place that answers "are captures paused?" and lets the web
dashboard pause/resume them.

How pausing works (deliberately simple and robust):
    Pausing just creates a FLAG FILE at /data/state/paused.flag. The capture
    cycle checks for that file at the very top of every run and, if it is there,
    skips taking a photo. Resuming deletes the file.

Why a file instead of a variable in memory?
    1. It SURVIVES a container restart -- if you pause to save battery and the
       container restarts overnight, it stays paused.
    2. It is shared safely between the capture thread and the web-server thread
       without any locks: one side checks if the file exists, the other side
       creates or deletes it.
    3. You can pause/resume BY HAND (touch / rm the file) if the web UI is ever
       unavailable.

Pausing does NOT log out of Arlo and does NOT stop the daily digest email -- the
camera session and your heartbeat stay alive. Only the snapshots are skipped.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import config

log = logging.getLogger(__name__)


def is_paused() -> bool:
    """True if captures are currently paused (the flag file exists)."""
    try:
        return config.PAUSE_FLAG.exists()
    except Exception:
        # If we cannot even check, assume NOT paused so we keep capturing.
        return False


def paused_since() -> str | None:
    """
    Return the human-readable time the pause began (the text we wrote into the
    flag file), or None if not paused / unknown.
    """
    try:
        if config.PAUSE_FLAG.exists():
            text = config.PAUSE_FLAG.read_text(encoding="utf-8").strip()
            return text or None
    except Exception:
        pass
    return None


def pause() -> None:
    """Pause captures by creating the flag file, stamped with the current time."""
    try:
        config.PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        config.PAUSE_FLAG.write_text(stamp, encoding="utf-8")
        log.info("Captures PAUSED via dashboard at %s.", stamp)
    except Exception as exc:
        log.error("Could not create pause flag: %s", exc)


def resume() -> None:
    """Resume captures by deleting the flag file (no error if already gone)."""
    try:
        config.PAUSE_FLAG.unlink(missing_ok=True)
        log.info("Captures RESUMED via dashboard.")
    except Exception as exc:
        log.error("Could not remove pause flag: %s", exc)
