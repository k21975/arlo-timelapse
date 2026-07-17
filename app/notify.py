"""
notify.py
=========
All email goes through here. There are two kinds:

  1. IMMEDIATE ALERTS -- something is wrong (login failed, camera offline, too
     many failed snapshots in a row, low battery). To avoid flooding your inbox
     we apply a per-kind "cooldown": the same KIND of alert is not re-sent until
     ALERT_COOLDOWN_HOURS have passed. The cooldown timestamps are saved to disk
     so a container restart does not immediately re-spam you.

  2. DAILY DIGEST -- a once-a-day summary at sunset. This is ALSO your heartbeat:
     if it stops arriving, you know something is wrong (so it is never throttled).

SMTP is the protocol for SENDING email. Gmail uses:
   - port 465 with SSL, or
   - port 587 with STARTTLS.
We pick automatically based on SMTP_PORT.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level: actually send a prepared EmailMessage over SMTP.
# ---------------------------------------------------------------------------
def _send(msg: EmailMessage) -> bool:
    """Send an EmailMessage. Returns True on success, False on any failure."""
    try:
        if config.SMTP_PORT == 465:
            # Implicit SSL/TLS from the very first byte (Gmail default).
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                config.SMTP_HOST, config.SMTP_PORT, context=context, timeout=30
            ) as server:
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
                server.send_message(msg)
        else:
            # Plain connection upgraded to TLS via STARTTLS (e.g. port 587).
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
                server.send_message(msg)
        return True
    except Exception as exc:  # never let an email problem crash the caller
        log.error("Failed to send email %r: %s", msg.get("Subject"), exc)
        return False


def send_email(subject: str, body: str, attach: Path | None = None, inline: bool = False) -> bool:
    """
    Build and send an email. Returns True on success.

    attach : optional path to a JPEG to include.
    inline : if True AND an image is attached, also show it inside the body.
    """
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.FROM_EMAIL
        msg["To"] = config.ALERT_EMAIL

        # Try to read the image, if one was requested.
        image_bytes = None
        if attach is not None:
            try:
                image_bytes = Path(attach).read_bytes()
            except Exception as exc:
                log.warning("Could not read image %s to attach: %s", attach, exc)

        if image_bytes and inline:
            # Plain-text part first (fallback), then an HTML part that shows the
            # image via a Content-ID ("cid") reference.
            cid = "snapshot1"
            msg.set_content(body)
            msg.add_alternative(
                f"<html><body><pre>{body}</pre>"
                f'<img src="cid:{cid}" style="max-width:100%;"></body></html>',
                subtype="html",
            )
            # Attach the image to the HTML part and tag it with the matching cid.
            html_part = msg.get_payload()[1]
            html_part.add_related(image_bytes, maintype="image", subtype="jpeg", cid=f"<{cid}>")
        else:
            msg.set_content(body)
            if image_bytes:
                msg.add_attachment(
                    image_bytes, maintype="image", subtype="jpeg",
                    filename=Path(attach).name,
                )
    except Exception as exc:
        log.error("Failed to build email %r: %s", subject, exc)
        return False

    ok = _send(msg)
    if ok:
        log.info("Sent email: %s", subject)
    return ok


# ---------------------------------------------------------------------------
# Alert cooldown: don't send the same KIND of alert too often.
# ---------------------------------------------------------------------------
def _load_cooldowns() -> dict:
    try:
        return json.loads(config.ALERT_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_cooldowns(data: dict) -> None:
    try:
        config.ALERT_STATE_FILE.write_text(json.dumps(data))
    except Exception as exc:
        log.warning("Could not save alert cooldown state: %s", exc)


def send_alert(kind: str, subject: str, body: str, attach: Path | None = None) -> bool:
    """
    Send an alert email of a given `kind` (e.g. "low_battery"), but only if we
    have not already sent that kind within the last ALERT_COOLDOWN_HOURS.
    """
    cooldowns = _load_cooldowns()
    last = cooldowns.get(kind, 0)
    now_ts = time.time()
    cooldown_secs = config.ALERT_COOLDOWN_HOURS * 3600

    if now_ts - last < cooldown_secs:
        remaining_min = int((cooldown_secs - (now_ts - last)) / 60)
        log.info("Suppressing '%s' alert (cooldown, ~%d min left): %s", kind, remaining_min, subject)
        return False

    ok = send_email(subject, body, attach=attach)
    if ok:
        cooldowns[kind] = now_ts
        _save_cooldowns(cooldowns)
    return ok


def send_daily_digest(stats: dict) -> bool:
    """
    Send the end-of-day summary (always sent -- it is the heartbeat).

    `stats` is a dict built by the caller with keys:
        date, frames_today, battery, signal, errors, last_image (Path or None)
    """
    date = stats.get("date", "")
    frames = stats.get("frames_today", 0)
    battery = stats.get("battery", "?")
    signal = stats.get("signal", "?")
    errors = stats.get("errors", 0)
    last_image = stats.get("last_image")
    paused = stats.get("paused", False)

    # A clear banner at the top of the email when captures are paused, so a quiet
    # "0 frames" day is never mistaken for a failure.
    pause_line = "STATUS                : *** PAUSED via dashboard ***\n" if paused else ""
    subject_tag = " [PAUSED]" if paused else ""

    subject = f"Arlo Timelapse — {date} — {frames} frames, battery {battery}%{subject_tag}"
    body = (
        f"Daily time-lapse summary for {date}\n"
        f"-----------------------------------\n"
        f"{pause_line}"
        f"Frames captured today : {frames}\n"
        f"Battery               : {battery}%\n"
        f"Signal (0-5)          : {signal}\n"
        f"Errors logged today   : {errors}\n\n"
        f"If you stop receiving this daily email, something is wrong with the\n"
        f"capture system or the camera -- go check it.\n"
    )
    return send_email(subject, body, attach=last_image, inline=bool(last_image))
