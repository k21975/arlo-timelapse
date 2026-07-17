"""
logging_setup.py
================
Configure logging ONCE for the whole app.

Every log message goes to TWO places:
  1. stdout            -> so `docker logs` / `docker compose logs -f` shows live activity.
  2. /data/logs/app.log -> so you keep history even after a restart. "Rotating"
     means when the file hits a size limit it is renamed (app.log.1, app.log.2, ...)
     and a fresh one starts, so it never grows without bound.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import config


class _DropArloModeNoise(logging.Filter):
    """Silence two harmless pyaarlo ERRORs the Go 2 emits every ~10 minutes.

    The Go 2 doesn't expose Arlo's "modes" (arm/disarm automation) API the way
    pyaarlo's periodic refresh expects, so it logs `failed to read modes.` and
    `failed to read active mode.` on a loop. They are not actionable and only
    clutter the log, so we drop exactly those two messages -- every other line
    from pyaarlo is kept untouched.
    """

    _NOISE = ("failed to read modes", "failed to read active mode")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True  # never let the filter itself swallow a real log line
        return not any(n in msg for n in self._NOISE)


def setup_logging() -> logging.Logger:
    """Set up and return the root logger. Safe to call once at startup."""
    config.ensure_dirs()  # make sure /data/logs exists before we open a file there

    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    # A clear, readable line format with a timestamp.
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # If handlers already exist (e.g. this was called twice), don't add duplicates.
    if root.handlers:
        return root

    # Drop the repetitive Go 2 "failed to read modes" noise on every handler so it
    # never reaches stdout OR the file (and thus the dashboard's Logs panel).
    noise_filter = _DropArloModeNoise()

    # 1) Console / stdout handler.
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(noise_filter)
    root.addHandler(console)

    # 2) Rotating file handler: 5 MB per file, keep 5 old files.
    file_handler = RotatingFileHandler(
        config.APP_LOG, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(noise_filter)
    root.addHandler(file_handler)

    # pyaarlo is very chatty at DEBUG; keep it at INFO unless you raise LOG_LEVEL.
    logging.getLogger("pyaarlo").setLevel(max(level, logging.INFO))

    return root
