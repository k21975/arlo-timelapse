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

Timed pauses ("rest of today" / "until <date>"):
    The flag file's first line is the time the pause began. An OPTIONAL second
    line "resume YYYY-MM-DD" says when captures should start again: on (or
    after) that date the pause clears itself automatically. No second line --
    including an old flag file or an empty hand-made one -- means "paused until
    someone presses Resume", exactly as before.

Pausing does NOT log out of Arlo and does NOT stop the daily digest email -- the
camera session and your heartbeat stay alive. Only the snapshots are skipped.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import config, daylight

log = logging.getLogger(__name__)


_RESUME_PREFIX = "resume "


def _read_flag() -> tuple[str | None, dt.date | None]:
    """(since-text, resume-date) from the flag file. Raises if it can't be read."""
    lines = config.PAUSE_FLAG.read_text(encoding="utf-8").splitlines()
    since = lines[0].strip() if lines else ""
    resume_on = None
    for line in lines[1:]:
        line = line.strip()
        if line.startswith(_RESUME_PREFIX):
            try:
                resume_on = dt.date.fromisoformat(line[len(_RESUME_PREFIX):].strip())
            except ValueError:
                resume_on = None  # unreadable date -> treat as an open-ended pause
    return (since or None), resume_on


def is_paused() -> bool:
    """True if captures are currently paused (the flag file exists).

    A timed pause whose resume date has arrived is cleared here, so captures
    restart on their own on the chosen day.
    """
    try:
        if not config.PAUSE_FLAG.exists():
            return False
    except Exception:
        # If we cannot even check, assume NOT paused so we keep capturing.
        return False
    try:
        _since, resume_on = _read_flag()
        if resume_on is not None and daylight.now().date() >= resume_on:
            config.PAUSE_FLAG.unlink(missing_ok=True)
            log.info("Timed pause ended (resume date %s reached); captures RESUMED.",
                     resume_on.isoformat())
            return False
    except Exception as exc:
        log.debug("Could not read pause flag details (%s); treating as paused.", exc)
    return True


def paused_since() -> str | None:
    """
    Return the human-readable time the pause began (the text we wrote into the
    flag file), or None if not paused / unknown.
    """
    try:
        if config.PAUSE_FLAG.exists():
            return _read_flag()[0]
    except Exception:
        pass
    return None


def resume_on() -> str | None:
    """The date (YYYY-MM-DD) a timed pause will end, or None if open-ended/not paused."""
    try:
        if config.PAUSE_FLAG.exists():
            d = _read_flag()[1]
            return d.isoformat() if d else None
    except Exception:
        pass
    return None


def pause(resume_date: dt.date | None = None) -> None:
    """Pause captures by creating the flag file, stamped with the current time.

    `resume_date` (optional) is the day captures start again by themselves;
    None means paused until someone presses Resume.
    """
    try:
        config.PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        stamp = daylight.now().strftime("%Y-%m-%d %H:%M:%S")
        text = stamp
        if resume_date is not None:
            text += f"\n{_RESUME_PREFIX}{resume_date.isoformat()}"
        config.PAUSE_FLAG.write_text(text + "\n", encoding="utf-8")
        log.info("Captures PAUSED via dashboard at %s%s.", stamp,
                 f" until {resume_date.isoformat()}" if resume_date else " (until resumed)")
    except Exception as exc:
        log.error("Could not create pause flag: %s", exc)


def resume() -> None:
    """Resume captures by deleting the flag file (no error if already gone)."""
    try:
        config.PAUSE_FLAG.unlink(missing_ok=True)
        log.info("Captures RESUMED via dashboard.")
    except Exception as exc:
        log.error("Could not remove pause flag: %s", exc)
