"""
daemon.py
=========
The long-running entrypoint. Run it with:  python -m app.daemon

What it does:
  1. Sets up logging and validates configuration.
  2. Logs in to Arlo ONCE (with resilient retry -- never a tight crash-loop).
  3. Starts an internal scheduler that:
       - takes a snapshot every CAPTURE_INTERVAL_MIN minutes (daylight only), and
       - sends a daily digest email at sunset (or a fixed time).
  4. Stays alive forever. pyaarlo refreshes the login in the background.

This is deliberately a DAEMON, not a cron job: logging in once and staying
connected is what keeps Arlo from demanding 2FA repeatedly.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import signal
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import arlo_client, capture, config, daylight, notify, pause, webserver
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def _login_with_resilience() -> None:
    """
    Log in once. If it fails, alert ONCE and keep retrying with exponential
    backoff (capped). We intentionally do NOT crash the process on login failure,
    because a restart-loop would hammer Arlo and risk an account lockout.
    """
    delay = 30
    max_delay = 1800  # cap retries at 30 minutes apart
    alerted = False
    while True:
        try:
            arlo_client.login(interactive=False)
            return
        except Exception as exc:
            log.error("Arlo login failed: %s", exc)
            if not alerted:
                notify.send_alert(
                    "login_failed",
                    "Arlo Timelapse — LOGIN FAILED",
                    f"Could not log in to Arlo: {exc}\n\n"
                    f"Retrying automatically with backoff. Check the Arlo password,\n"
                    f"the 2FA email account, and IMAP access.\n",
                )
                alerted = True
            log.info("Retrying login in %d seconds...", delay)
            time.sleep(delay)
            delay = min(delay * 2, max_delay)


def _build_digest_stats() -> dict:
    """Gather the numbers for the daily digest email."""
    today = daylight.now().date()
    date_str = today.strftime("%Y-%m-%d")
    day_dir = config.SNAP_DIR / date_str

    # Frames captured today + the most recent image to attach.
    frames = sorted(day_dir.glob("arlo_*.jpg")) if day_dir.exists() else []
    last_image = frames[-1] if frames else (config.LATEST_JPG if config.LATEST_JPG.exists() else None)

    # Live battery / signal, if we can reach the camera right now.
    battery, signal = "?", "?"
    try:
        cam = arlo_client.get_camera()
        if cam is not None:
            battery = getattr(cam, "battery_level", "?")
            signal = arlo_client.camera_signal(cam)["signal"]
    except Exception:
        pass

    # Count today's failures from status.csv.
    errors = 0
    try:
        if config.STATUS_CSV.exists():
            with open(config.STATUS_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("timestamp", "").startswith(date_str) and row.get("success") == "no":
                        errors += 1
    except Exception:
        pass

    return {
        "date": date_str,
        "frames_today": len(frames),
        "battery": battery,
        "signal": signal,
        "errors": errors,
        "last_image": last_image,
        "paused": pause.is_paused(),  # so the digest can remind you it's paused
    }


def _send_digest_job() -> None:
    """Scheduler job: build and send the daily digest (never throws)."""
    try:
        notify.send_daily_digest(_build_digest_stats())
    except Exception as exc:
        log.exception("Failed to build/send daily digest: %s", exc)


def _schedule_sunset_digest(scheduler: BlockingScheduler) -> None:
    """Schedule today's digest at sunset, if sunset has not already passed."""
    try:
        sunset = daylight.todays_sunset()
        if sunset > daylight.now():
            scheduler.add_job(
                _send_digest_job, "date", run_date=sunset,
                id="digest_today", replace_existing=True, misfire_grace_time=3600,
            )
            log.info("Daily digest scheduled for sunset today at %s.", sunset.strftime("%H:%M"))
        else:
            log.info("Sunset already passed today; digest will run tomorrow.")
    except Exception as exc:
        log.error("Could not schedule sunset digest: %s", exc)


# ---------------------------------------------------------------------------
# (Re)building the scheduled jobs. These are split into their own functions so
# that BOTH startup AND a live settings change (from the web dashboard) can call
# them. add_job(replace_existing=True) means re-running them just updates the
# existing jobs in place.
# ---------------------------------------------------------------------------
# After a failed scheduled capture, try again this many minutes later. The
# retries are cheap: each one first checks the camera's CACHED availability flag
# and only wakes the radio if the camera looks reachable again -- so a recovered
# camera fills the gap mid-hour, while a still-offline one costs nothing.
_CATCHUP_DELAYS_MIN = (15, 30)


# Dead-session watchdog: pyaarlo's reconnect_every only restarts the event
# stream, never the REST auth token. A dead token looks like an endless run of
# "cached frame" failures with the camera claiming to be fine (and hourly
# "No devices returned" warnings). After this many consecutive failures we
# force a fresh login -- 2FA-safe, it reuses the trusted session on /data.
_RELOGIN_AFTER_FAILURES = 3
_RELOGIN_MIN_GAP_SEC = 45 * 60
_last_relogin_at: float | None = None  # None = never (monotonic starts at boot)


def _maybe_relogin() -> None:
    """Force a fresh Arlo login if captures look session-dead (rate-limited)."""
    global _last_relogin_at
    fails = capture.consecutive_failures()
    if fails < _RELOGIN_AFTER_FAILURES:
        return
    now = time.monotonic()
    if _last_relogin_at is not None and now - _last_relogin_at < _RELOGIN_MIN_GAP_SEC:
        return
    _last_relogin_at = now
    log.warning(
        "%d captures in a row produced no fresh frame -- the Arlo session may "
        "be dead (pyaarlo never re-auths its REST token). Forcing a fresh "
        "login (trusted session, no 2FA).", fails,
    )
    with capture.capture_lock():
        arlo_client.relogin()


def _schedule_relogin_job(scheduler: BlockingScheduler) -> None:
    """PREEMPTIVE session refresh. Arlo's auth token dies ~2 hours after login
    (production logs show captures failing on schedule at the 2h mark), and
    pyaarlo's reconnect_every never re-authenticates it. So we do the full
    re-login ourselves every RECONNECT_EVERY_MIN minutes -- a ~4 second,
    2FA-safe operation -- and the token simply never reaches its expiry."""
    minutes = max(10, int(getattr(config, "RECONNECT_EVERY_MIN", 90)))
    scheduler.add_job(
        _preemptive_relogin, IntervalTrigger(minutes=minutes), id="relogin",
        replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=600,
    )
    log.info("Preemptive Arlo re-login scheduled every %d min "
             "(the token expires ~2h; pyaarlo won't refresh it itself).", minutes)


def _preemptive_relogin() -> None:
    """Scheduler job: refresh the session before the token can expire."""
    global _last_relogin_at
    try:
        log.info("Preemptive Arlo session refresh (before the ~2h token expiry).")
        with capture.capture_lock():
            ok = arlo_client.relogin()
        if ok:
            _last_relogin_at = time.monotonic()
    except Exception as exc:
        log.error("Preemptive re-login failed (watchdog remains as backstop): %s", exc)


def _run_scheduled_capture(scheduler: BlockingScheduler) -> None:
    """One scheduled capture cycle; a failure arms the catch-up retries."""
    result = capture.run_capture_cycle()
    if result == "fail":
        _maybe_relogin()  # heal a dead session BEFORE the catch-ups fire
        _schedule_catchup_retries(scheduler)
    else:
        _cancel_catchup_retries(scheduler)


def _schedule_catchup_retries(scheduler: BlockingScheduler) -> None:
    """Arm one-shot retries after a failed capture (only for intervals long
    enough that the next scheduled attempt would leave a real gap)."""
    now = daylight.now()
    delays = [m for m in _CATCHUP_DELAYS_MIN if m < max(1, config.CAPTURE_INTERVAL_MIN)]
    for i, minutes in enumerate(delays, start=1):
        scheduler.add_job(
            _run_catchup_capture, "date", run_date=now + dt.timedelta(minutes=minutes),
            args=[scheduler], id=f"capture_catchup_{i}",
            replace_existing=True, misfire_grace_time=300,
        )
    if delays:
        log.info("Capture failed; catch-up retries armed for +%s min.",
                 " and +".join(str(m) for m in delays))


def _cancel_catchup_retries(scheduler: BlockingScheduler) -> None:
    """Drop any pending catch-up retries (a capture just succeeded)."""
    for i in range(1, len(_CATCHUP_DELAYS_MIN) + 1):
        try:
            scheduler.remove_job(f"capture_catchup_{i}")
        except Exception:
            pass


def _run_catchup_capture(scheduler: BlockingScheduler) -> None:
    """A one-shot catch-up attempt. Reads only CACHED state (pause flag,
    daylight window, the cloud's availability flag) before deciding to wake the
    camera, so a still-offline camera is left alone."""
    try:
        if pause.is_paused() or not daylight.is_daylight():
            return
        cam = arlo_client.get_camera()
        if cam is None or bool(getattr(cam, "is_unavailable", False)):
            log.info("Catch-up capture skipped: camera still flagged offline.")
            return
        log.info("Catch-up capture: camera looks reachable again; taking the missed snapshot.")
        result = capture.run_capture_cycle()
        if result == "ok":
            _cancel_catchup_retries(scheduler)  # got the frame; drop any later retry
        elif result == "fail":
            _maybe_relogin()  # a dead session shouldn't wait for the next hour
    except Exception as exc:
        log.exception("Catch-up capture crashed (daemon unaffected): %s", exc)


def _schedule_capture_job(scheduler: BlockingScheduler) -> None:
    """Create/replace the snapshot job from the CURRENT interval setting."""
    interval = max(1, config.CAPTURE_INTERVAL_MIN)
    # Align to the clock where the interval allows it: a minute-step must divide
    # evenly into 60 AND stay below 60 (cron rejects a step of 60, since the
    # minute field only spans 0-59 -- that rejection used to silently keep the
    # old schedule running). Whole-hour intervals become hour-steps instead
    # (fires at :00 of every Nth hour). Anything else is a plain interval.
    try:
        if interval < 60 and 60 % interval == 0:
            trigger = CronTrigger(minute=f"*/{interval}")
        elif interval % 60 == 0 and interval // 60 < 24 and 24 % (interval // 60) == 0:
            trigger = CronTrigger(minute=0, hour=f"*/{interval // 60}")
        else:
            trigger = IntervalTrigger(minutes=interval)
    except Exception as exc:
        # A trigger the library refuses must never crash (or crash-loop) the
        # daemon; a plain interval always works.
        log.error("Could not build a clock-aligned trigger for %d min (%s); "
                  "using a plain interval instead.", interval, exc)
        trigger = IntervalTrigger(minutes=interval)
    scheduler.add_job(
        _run_scheduled_capture, trigger, args=[scheduler], id="capture",
        replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=120,
    )
    log.info("Capture job scheduled every %d min (it skips outside daylight).", interval)


def _schedule_digest_jobs(scheduler: BlockingScheduler) -> None:
    """Create/replace the daily-digest job(s) from the CURRENT digest setting."""
    # The daily digest can be turned off entirely from Settings. When disabled,
    # tear down any existing digest jobs and stop (alerts are unaffected).
    enabled = str(getattr(config, "DAILY_DIGEST_ENABLED", "yes")).strip().lower() in (
        "yes", "true", "1", "on",
    )
    if not enabled:
        for stale in ("digest", "digest_rearm", "digest_today"):
            try:
                scheduler.remove_job(stale)
            except Exception:
                pass
        log.info("Daily digest is turned OFF; no digest email will be sent.")
        return

    digest_when = (config.DAILY_DIGEST_TIME or "sunset").strip().lower()
    if digest_when == "sunset":
        # Remove any fixed-time digest from a previous setting, then arm sunset.
        try:
            scheduler.remove_job("digest")
        except Exception:
            pass
        _schedule_sunset_digest(scheduler)  # arm today's
        # Re-arm every day just after midnight for the new day's sunset.
        scheduler.add_job(
            lambda: _schedule_sunset_digest(scheduler),
            CronTrigger(hour=0, minute=10), id="digest_rearm", replace_existing=True,
        )
    else:
        # Fixed time like "20:30". Drop the sunset re-arm job if it exists.
        for stale in ("digest_rearm", "digest_today"):
            try:
                scheduler.remove_job(stale)
            except Exception:
                pass
        hh, mm = digest_when.split(":")
        scheduler.add_job(
            _send_digest_job, CronTrigger(hour=int(hh), minute=int(mm)),
            id="digest", replace_existing=True,
        )
        log.info("Daily digest scheduled for %s.", digest_when)


def main() -> None:
    setup_logging()
    log.info("=== Arlo Time-Lapse daemon starting ===")
    config.validate_required()
    config.ensure_dirs()

    # Log in ONCE (resilient).
    _login_with_resilience()

    # Start the local web dashboard (battery/signal/photos + Pause button) in a
    # background thread. It is wrapped so it can never affect captures.
    webserver.start_in_background()

    # The scheduler runs in our configured timezone so cron times line up with
    # local clock time and with sunrise/sunset.
    scheduler = BlockingScheduler(timezone=config.TZ)

    # --- Capture + daily-digest + session-refresh jobs (from current settings)
    _schedule_capture_job(scheduler)
    _schedule_digest_jobs(scheduler)
    _schedule_relogin_job(scheduler)

    # --- React to settings changed in the web dashboard ----------------------
    # When you save settings in the UI, config.reload() runs and then calls this.
    # Re-running the schedulers updates the interval / digest time live, so most
    # changes never need a container restart. Wrapped so a hiccup here can never
    # take down the capture loop.
    def _reapply_settings() -> None:
        try:
            _schedule_capture_job(scheduler)
            _schedule_digest_jobs(scheduler)
            _schedule_relogin_job(scheduler)
            log.info("Re-applied schedule after a settings change.")
        except Exception as exc:
            log.error("Could not re-apply schedule after settings change: %s", exc)

    config.on_reload(_reapply_settings)

    # --- Graceful shutdown on Docker stop (SIGTERM) or Ctrl+C (SIGINT) -------
    def _shutdown(signum, frame):
        log.info("Signal %s received; shutting down.", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Daemon ready. Waiting for scheduled jobs...")
    try:
        scheduler.start()  # blocks here until shutdown
    except (KeyboardInterrupt, SystemExit):
        pass
    log.info("=== daemon stopped ===")


if __name__ == "__main__":
    main()
