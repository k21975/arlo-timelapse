"""
daylight.py
===========
Work out whether "now" is inside the capture window.

There are TWO ways the window can be defined:

  1. DAYLIGHT (the default): we use the `astral` library to compute sunrise and
     sunset for our latitude/longitude, then add a buffer (default 30 min) so we
     start a little AFTER sunrise and stop a little BEFORE sunset -- the first and
     last minutes of the day are usually too dark to be useful.

  2. FIXED TIMES: if you set BOTH a start and an end time in the dashboard
     (CAPTURE_START / CAPTURE_END, like "07:00" and "19:30"), those exact clock
     times are used instead and the sun is ignored.

Everything here reads `config` LIVE (each call), so changes you make in the web
dashboard -- location, buffer, fixed times, time zone -- take effect immediately
without restarting the daemon.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

from . import config


# ---------------------------------------------------------------------------
# These are built fresh on each call (cheap) so edits in the UI apply live.
# ---------------------------------------------------------------------------
def _tz() -> ZoneInfo:
    return ZoneInfo(config.TZ)


def _location() -> LocationInfo:
    return LocationInfo(
        name="site",
        region="",
        timezone=config.TZ,
        latitude=config.LATITUDE,
        longitude=config.LONGITUDE,
    )


def now() -> dt.datetime:
    """Current local time (timezone-aware) in our configured timezone."""
    return dt.datetime.now(_tz())


def sun_times(date: dt.date | None = None) -> tuple[dt.datetime, dt.datetime]:
    """Return (sunrise, sunset) as timezone-aware datetimes for the given date."""
    if date is None:
        date = now().date()
    tz = _tz()
    s = sun(_location().observer, date=date, tzinfo=tz)
    return s["sunrise"], s["sunset"]


def _fixed_window(date: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    """
    If BOTH fixed times are set, return (start, stop) for `date` using them.
    Otherwise return None so the caller falls back to the daylight calculation.
    """
    start_txt = (config.CAPTURE_START or "").strip()
    end_txt = (config.CAPTURE_END or "").strip()
    if not (start_txt and end_txt):
        return None
    try:
        sh, sm = config._parse_hhmm(start_txt)
        eh, em = config._parse_hhmm(end_txt)
    except ValueError:
        return None  # malformed -> ignore and use the sun instead
    tz = _tz()
    start = dt.datetime(date.year, date.month, date.day, sh, sm, tzinfo=tz)
    stop = dt.datetime(date.year, date.month, date.day, eh, em, tzinfo=tz)
    return start, stop


def daylight_window(date: dt.date | None = None) -> tuple[dt.datetime, dt.datetime]:
    """
    Return the capture window (start, stop). Fixed times win if both are set;
    otherwise it is sunrise+buffer .. sunset-buffer.
    """
    if date is None:
        date = now().date()
    fixed = _fixed_window(date)
    if fixed is not None:
        return fixed
    sunrise, sunset = sun_times(date)
    buffer = dt.timedelta(minutes=config.DAYLIGHT_BUFFER_MIN)
    return sunrise + buffer, sunset - buffer


def is_daylight(moment: dt.datetime | None = None) -> bool:
    """True if `moment` (default: now) is inside today's capture window.

    The end check is done at MINUTE granularity: the scheduler fires a capture a
    few hundred microseconds after the top of the minute, so a fixed end time of
    exactly 16:00 must still count a 16:00:00.0004 capture as inside the window
    (otherwise the final frame of every day would be silently skipped).
    """
    if moment is None:
        moment = now()
    start, stop = daylight_window(moment.date())
    return start <= moment and moment.replace(second=0, microsecond=0) <= stop


def todays_sunset() -> dt.datetime:
    """Sunset for today -- used to time the daily digest email."""
    return sun_times()[1]
