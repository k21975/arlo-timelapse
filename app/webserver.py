"""
webserver.py
============
A TINY local web dashboard for the time-lapse, built with ONLY the Python
standard library (no Flask, no extra dependencies). It shows live battery,
signal, photo counts and the latest snapshot, and gives you a PAUSE / RESUME
button.

Design rules (this is a reliability-critical, unattended deployment):
  * It runs in a BACKGROUND DAEMON THREAD started from daemon.py. A daemon thread
    dies automatically when the main process exits, so it never holds shutdown up.
  * Every request handler is wrapped in try/except. If the web UI ever breaks, it
    must NOT affect the capture loop or the daily heartbeat email.
  * It only ever READS camera attributes (battery/signal are cached values that
    pyaarlo keeps up to date in the background) -- it never asks the camera to do
    work, so a page refresh costs the battery nothing.

Routes:
  GET  /              -> the HTML dashboard page
  GET  /api/status    -> JSON with all the numbers (the page polls this)
  GET  /latest.jpg    -> the most recent snapshot image
  POST /api/pause     -> pause captures (create the flag file)
  POST /api/resume    -> resume captures (delete the flag file)
"""

from __future__ import annotations

import csv
import html
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

from . import arlo_client, builder, capture, config, daylight, pause, version

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The site title is editable from Settings (two lines). These helpers read the
# CURRENT values and fall back to sensible defaults if either is left blank, so
# the page, the browser tab, the link-preview card and the app icon all stay in
# sync with whatever the operator typed.
# ---------------------------------------------------------------------------
def _title_lines() -> tuple[str, str]:
    """The two title lines (uppercased for the synthwave look), with fallbacks."""
    line1 = (getattr(config, "SITE_TITLE_LINE1", None) or "TIME LAPSE").strip().upper()
    line2 = (getattr(config, "SITE_TITLE_LINE2", None) or "").strip().upper()
    return line1, line2


def _title_full() -> str:
    """The two lines joined into one string, e.g. 'ARLO TIME LAPSE CAPTURE'."""
    line1, line2 = _title_lines()
    return " ".join(p for p in (line1, line2) if p)


# ---------------------------------------------------------------------------
# App icon (favicon + iPhone "Add to Home Screen" icon).
# We draw a neon bicycle over a synthwave sun with Pillow (already a dependency)
# so there is no binary asset to ship. It is generated ONCE and cached in
# memory, then served as a PNG for both <link rel="icon"> and apple-touch-icon.
# ---------------------------------------------------------------------------
_ICON_CACHE: dict[int, bytes] = {}


def _build_icon_png(size: int = 180) -> bytes | None:
    """Return PNG bytes for a themed app icon, or None if Pillow is unavailable."""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFilter
    except Exception as exc:  # Pillow missing -> just skip the icon, no big deal.
        log.debug("Could not build app icon (Pillow unavailable): %s", exc)
        return None

    W = H = size
    s = size / 180.0  # geometry is authored at 180px, then scaled to `size`

    def P(x, y):
        return (x * s, y * s)

    # --- Background: vertical synthwave gradient (deep purple -> magenta -> ink)
    bg = Image.new("RGB", (W, H), (10, 1, 24))
    top, mid, bot = (42, 17, 99), (120, 24, 92), (10, 1, 24)
    d = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / (H - 1)
        if t < 0.55:
            f = t / 0.55
            c = tuple(int(top[i] + (mid[i] - top[i]) * f) for i in range(3))
        else:
            f = (t - 0.55) / 0.45
            c = tuple(int(mid[i] + (bot[i] - mid[i]) * f) for i in range(3))
        d.line([(0, y), (W, y)], fill=c)

    # --- The classic synthwave sun (pink -> gold), masked to a circle ---------
    sun = Image.new("RGB", (W, H))
    sd = ImageDraw.Draw(sun)
    top_s, bot_s = (255, 46, 136), (255, 196, 90)
    for y in range(H):
        f = y / (H - 1)
        sd.line([(0, y), (W, y)],
                fill=tuple(int(top_s[i] + (bot_s[i] - top_s[i]) * f) for i in range(3)))
    cx, cy, r = 90 * s, 72 * s, 44 * s
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    bg.paste(sun, (0, 0), mask)
    # horizontal slits across the lower half of the sun
    d = ImageDraw.Draw(bg)
    for yy in range(int(cy + 6 * s), int(cy + r), max(1, int(7 * s))):
        d.rectangle([0, yy, W, yy + max(2, int(3 * s))], fill=(70, 14, 60))

    # --- Bicycle on its own layer so we can give it a neon glow ---------------
    ink = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(ink)
    cyan = (31, 229, 255, 255)
    lw = max(3, int(6 * s))

    rear, front, bb = P(54, 126), P(126, 126), P(86, 126)
    seat, head = P(74, 84), P(120, 86)
    wheel_r = 26 * s
    for c in (rear, front):
        bd.ellipse([c[0] - wheel_r, c[1] - wheel_r, c[0] + wheel_r, c[1] + wheel_r],
                   outline=cyan, width=lw)
    bd.line([rear, bb], fill=cyan, width=lw)     # chainstay
    bd.line([bb, seat], fill=cyan, width=lw)     # seat tube
    bd.line([seat, rear], fill=cyan, width=lw)   # seat stay
    bd.line([seat, head], fill=cyan, width=lw)   # top tube
    bd.line([bb, head], fill=cyan, width=lw)     # down tube
    bd.line([head, front], fill=cyan, width=lw)  # fork
    bd.line([P(66, 82), P(80, 82)], fill=cyan, width=lw)    # saddle
    bd.line([P(112, 80), P(128, 80)], fill=cyan, width=lw)  # handlebar

    glow = ink.filter(ImageFilter.GaussianBlur(radius=max(2, int(4 * s))))
    base = bg.convert("RGBA")
    base.alpha_composite(glow)
    base.alpha_composite(glow)  # twice for a stronger neon halo
    base.alpha_composite(ink)

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _icon_png(size: int = 180) -> bytes | None:
    """Cached accessor for the app icon PNG at a given size."""
    if size not in _ICON_CACHE:
        png = _build_icon_png(size)
        if png is None:
            return None
        _ICON_CACHE[size] = png
    return _ICON_CACHE[size]


_FAVICON_CACHE: dict[str, bytes] = {}


def _favicon_ico() -> bytes | None:
    """A REAL multi-size .ico for the browser tab favicon (cached).

    Safari in particular is fussy: for the tab icon it really wants a genuine
    /favicon.ico (an actual ICO, not a PNG served at that path), or it falls back
    to showing the first letter of the domain. We build the ICO from the same
    neon-bike PNG so it matches everything else.
    """
    if "ico" in _FAVICON_CACHE:
        return _FAVICON_CACHE["ico"]
    try:
        import io
        from PIL import Image
    except Exception as exc:
        log.debug("Could not build favicon.ico (Pillow unavailable): %s", exc)
        return None
    png = _icon_png(180)
    if png is None:
        return None
    try:
        img = Image.open(io.BytesIO(png)).convert("RGBA")
        buf = io.BytesIO()
        # bitmap_format="bmp" is the whole point: by default Pillow stores each
        # image *PNG-compressed* inside the .ico. Chrome reads that fine, but
        # Safari's ICO decoder only understands the classic BMP/DIB encoding --
        # given a PNG-in-ICO it silently fails and shows the first letter of the
        # site in the tab instead of the favicon. Forcing BMP fixes Safari.
        # (bitmap_format was added in Pillow 9.3; we ship 13.0, but fall back to
        # the default save on anything older so the icon never disappears.)
        try:
            img.save(buf, format="ICO",
                     sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
                     bitmap_format="bmp")
        except TypeError:
            buf = io.BytesIO()
            img.save(buf, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        _FAVICON_CACHE["ico"] = buf.getvalue()
        return _FAVICON_CACHE["ico"]
    except Exception as exc:
        log.debug("Could not encode favicon.ico: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Social share / link-preview banner (Open Graph "og:image"). When the site URL
# is shared in iMessage/Slack/etc, this 1200x630 card is shown instead of a
# random (often dark) camera frame. Drawn once with Pillow and cached.
# ---------------------------------------------------------------------------
_OG_CACHE: dict[str, bytes] = {}


def _build_og_png(line1: str, line2: str) -> bytes | None:
    """Return PNG bytes for the synthwave share card, or None if Pillow is missing."""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except Exception as exc:
        log.debug("Could not build OG image (Pillow unavailable): %s", exc)
        return None

    W, H = 1200, 630

    def font(size):
        for p in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # in container
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # mac fallback
            "/Library/Fonts/Arial Bold.ttf",
        ):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def lerp(a, b, f):
        return tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))

    def vgrad(w, h, top, bot):
        g = Image.new("RGB", (w, h))
        gd = ImageDraw.Draw(g)
        for y in range(h):
            gd.line([(0, y), (w, y)], fill=lerp(top, bot, y / max(1, h - 1)))
        return g

    def word_mask(text, f, tracking):
        """Draw text with letter-spacing onto a tight grayscale mask."""
        pad = f.size
        ww = int(sum(f.getlength(c) for c in text) + tracking * len(text) + pad * 2)
        hh = int(f.size * 1.7)
        img = Image.new("L", (ww, hh), 0)
        gd = ImageDraw.Draw(img)
        x = float(pad)
        for c in text:
            gd.text((x, hh * 0.18), c, font=f, fill=255)
            x += f.getlength(c) + tracking
        return img.crop(img.getbbox())

    def fit_width(mask, target):
        """Scale a mask to an exact width (keeps both title lines equal + on-canvas)."""
        h = max(1, int(mask.height * target / mask.width))
        return mask.resize((target, h), Image.LANCZOS)

    def paste_text(base, mask, cx, cy, grad_top, grad_bot, glow, blur=14):
        w, h = mask.size
        x, y = int(cx - w / 2), int(cy - h / 2)
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        layer.paste(glow + (255,), (x, y), mask)
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
        base.alpha_composite(layer)
        base.alpha_composite(layer)
        base.paste(vgrad(w, h, grad_top, grad_bot).convert("RGBA"), (x, y), mask)

    # --- background gradient --------------------------------------------------
    base = Image.new("RGB", (W, H), (10, 1, 24))
    top, mid, bot = (40, 16, 92), (92, 18, 74), (8, 1, 20)
    d = ImageDraw.Draw(base)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)],
               fill=lerp(top, mid, t / 0.5) if t < 0.5 else lerp(mid, bot, (t - 0.5) / 0.5))

    # --- synthwave sun --------------------------------------------------------
    cx, cy, r = W / 2, 215, 200
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    base.paste(vgrad(W, H, (255, 46, 136), (255, 196, 90)), (0, 0), mask)
    d = ImageDraw.Draw(base)
    for yy in range(int(cy + 18), int(cy + r), 16):
        d.rectangle([0, yy, W, yy + 7], fill=lerp(top, mid, (yy - cy) / r))

    base = base.convert("RGBA")

    # --- perspective grid floor ----------------------------------------------
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    horizon = 452
    for k in range(-12, 13):
        gd.line([(W / 2, horizon), (W / 2 + k * 150, H)], fill=(255, 46, 136, 90), width=2)
    y, gap = float(horizon), 4.0
    while y < H:
        a = int(40 + 150 * (y - horizon) / (H - horizon))
        gd.line([(0, int(y)), (W, int(y))], fill=(31, 229, 255, max(20, min(170, a))), width=2)
        gap *= 1.16
        y += gap
    base.alpha_composite(grid)

    # --- eyebrow + title (one or two equal-width lines) -----------------------
    eyebrow = word_mask("TIME-LAPSE", font(40), 22)
    paste_text(base, eyebrow, W / 2, 120, (31, 229, 255), (31, 229, 255), (31, 229, 255), blur=8)
    if line2:
        l1 = fit_width(word_mask(line1, font(150), 16), 1040)
        l2 = fit_width(word_mask(line2, font(150), 16), 1040)
        paste_text(base, l1, W / 2, 255, (255, 255, 255), (255, 46, 136), (255, 46, 136))
        paste_text(base, l2, W / 2, 405, (255, 210, 245), (43, 107, 255), (166, 75, 255))
    else:
        # Single-line title: center it between the two normal line positions.
        l1 = fit_width(word_mask(line1, font(150), 16), 1040)
        paste_text(base, l1, W / 2, 330, (255, 255, 255), (255, 46, 136), (255, 46, 136))

    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _og_png() -> bytes | None:
    """Cached accessor for the share-card PNG (keyed by the current title)."""
    line1, line2 = _title_lines()
    key = line1 + "|" + line2
    if key not in _OG_CACHE:
        png = _build_og_png(line1, line2)
        if png is None:
            return None
        _OG_CACHE[key] = png
    return _OG_CACHE[key]


def _latest_snapshot_path():
    """Return the path of the genuinely most-recent captured photo.

    Captures are stored as /data/snapshots/YYYY-MM-DD/arlo_YYYYMMDD_HHMMSS.jpg,
    and capture.py also keeps a copy at snapshots/latest.jpg. We scan the newest
    day folder for the newest arlo_*.jpg so the page always shows the true last
    capture, and fall back to the latest.jpg copy if the scan finds nothing.
    """
    try:
        if config.SNAP_DIR.exists():
            day_dirs = sorted(
                (d for d in config.SNAP_DIR.iterdir() if d.is_dir()),
                key=lambda p: p.name, reverse=True,
            )
            for d in day_dirs:
                frames = list(d.glob("arlo_*.jpg"))
                if frames:
                    # Filenames sort chronologically, so max() is the newest.
                    return max(frames, key=lambda p: p.name)
    except Exception as exc:
        log.debug("Could not locate latest snapshot: %s", exc)
    if config.LATEST_JPG.exists():
        return config.LATEST_JPG
    return None


# ---------------------------------------------------------------------------
# Captures gallery + deletion. Frames live at snapshots/YYYY-MM-DD/arlo_*.jpg;
# the API identifies one as the relative string "YYYY-MM-DD/arlo_..._....jpg".
# Everything a browser sends is matched against this exact pattern before it
# touches the filesystem, so a crafted path can never reach outside SNAP_DIR.
# ---------------------------------------------------------------------------
_CAPTURE_REL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})/(arlo_\d{8}_\d{6}\.jpg)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _list_captures() -> list:
    """Every saved frame grouped by day, newest first, for the gallery."""
    days = []
    try:
        if config.SNAP_DIR.exists():
            for d in sorted((p for p in config.SNAP_DIR.iterdir() if p.is_dir()),
                            key=lambda p: p.name, reverse=True):
                frames = sorted((f.name for f in d.glob("arlo_*.jpg")), reverse=True)
                if frames:
                    days.append({"date": d.name, "frames": frames})
    except Exception as exc:
        log.debug("Could not list captures: %s", exc)
    return days


def _capture_path(rel: str):
    """The real path for a 'YYYY-MM-DD/arlo_....jpg' string, or None if it does
    not look exactly like one of our frames (rejects any path trickery)."""
    m = _CAPTURE_REL_RE.match(rel or "")
    if not m:
        return None
    return config.SNAP_DIR / m.group(1) / m.group(2)


def _prune_empty_day_dirs() -> None:
    """Remove day folders that deletion has emptied out."""
    try:
        for d in config.SNAP_DIR.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    except Exception:
        pass


def _delete_captures(rels: list) -> int:
    """Delete the listed frames (already-gone files are skipped, not errors)."""
    deleted = 0
    for rel in rels:
        path = _capture_path(str(rel))
        try:
            if path is not None and path.exists():
                path.unlink()
                deleted += 1
        except Exception as exc:
            log.warning("Could not delete capture %s: %s", rel, exc)
    _prune_empty_day_dirs()
    return deleted


def _clear_captures(before: str | None) -> int:
    """Delete every frame, or only those in day folders before `before`
    (frames from the `before` date itself are KEPT). Returns the count."""
    deleted = 0
    try:
        if config.SNAP_DIR.exists():
            for d in (p for p in config.SNAP_DIR.iterdir() if p.is_dir()):
                if before and d.name >= before:
                    continue  # day-folder names are YYYY-MM-DD, so this compares dates
                for f in d.glob("arlo_*.jpg"):
                    try:
                        f.unlink()
                        deleted += 1
                    except Exception as exc:
                        log.warning("Could not delete capture %s: %s", f, exc)
        _prune_empty_day_dirs()
        # Clearing everything also drops the latest.jpg convenience copy.
        if before is None:
            config.LATEST_JPG.unlink(missing_ok=True)
    except Exception as exc:
        log.error("Clear captures failed part-way: %s", exc)
    return deleted


# ---------------------------------------------------------------------------
# Gather every number the dashboard shows, into one plain dict.
# Wrapped defensively: any single failure becomes a "?" rather than an error.
# ---------------------------------------------------------------------------
def _gather_status() -> dict:
    now = daylight.now()
    today = now.date()
    date_str = today.strftime("%Y-%m-%d")

    # --- Photo counts ------------------------------------------------------
    # Today's frames live in /data/snapshots/YYYY-MM-DD/. The all-time total is
    # every arlo_*.jpg across all day folders (cheap even after months).
    frames_today = 0
    frames_total = 0
    try:
        day_dir = config.SNAP_DIR / date_str
        if day_dir.exists():
            frames_today = sum(1 for _ in day_dir.glob("arlo_*.jpg"))
        if config.SNAP_DIR.exists():
            for d in config.SNAP_DIR.iterdir():
                if d.is_dir():
                    frames_total += sum(1 for _ in d.glob("arlo_*.jpg"))
    except Exception as exc:
        log.debug("Could not count frames: %s", exc)

    # --- Live battery / signal from the camera -----------------------------
    battery, signal, signal_note = "?", "?", ""
    camera_found = False
    camera_unavailable = None
    try:
        cam = arlo_client.get_camera()
        if cam is not None:
            camera_found = True
            battery = getattr(cam, "battery_level", "?")
            sig = arlo_client.camera_signal(cam)  # the REAL radio, not the dead WiFi one
            signal = sig["signal"]
            signal_note = " ".join(p for p in (sig["carrier"], sig["band"]) if p)
            camera_unavailable = bool(getattr(cam, "is_unavailable", False))
    except Exception as exc:
        log.debug("Could not read camera health: %s", exc)

    # --- Most recent + last few rows from status.csv -----------------------
    last_capture = None
    recent: list[dict] = []
    try:
        if config.STATUS_CSV.exists():
            with open(config.STATUS_CSV, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            for row in rows[-10:]:  # keep the last 10, newest last
                recent.append({
                    "timestamp": row.get("timestamp", ""),
                    "success": row.get("success", ""),
                    "battery": row.get("battery_pct", ""),
                    "signal": row.get("signal", ""),
                    "note": row.get("note", ""),
                })
            if recent:
                last_capture = recent[-1]
            recent.reverse()  # newest first for display
    except Exception as exc:
        log.debug("Could not read status.csv: %s", exc)

    # --- Errors logged today ----------------------------------------------
    errors_today = 0
    try:
        if config.STATUS_CSV.exists():
            with open(config.STATUS_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("timestamp", "").startswith(date_str) and row.get("success") == "no":
                        errors_today += 1
    except Exception:
        pass

    # --- Daylight window + a rough "next capture" estimate -----------------
    sunrise = sunset = win_start = win_stop = None
    is_day = False
    try:
        sr, ss = daylight.sun_times(today)
        ws, wstop = daylight.daylight_window(today)
        sunrise = sr.strftime("%H:%M")
        sunset = ss.strftime("%H:%M")
        win_start = ws.strftime("%H:%M")
        win_stop = wstop.strftime("%H:%M")
        is_day = daylight.is_daylight(now)
    except Exception as exc:
        log.debug("Could not compute daylight window: %s", exc)

    # Are we using fixed clock times, or sunrise/sunset?
    window_mode = "fixed" if (config.CAPTURE_START and config.CAPTURE_END) else "daylight"

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "paused": pause.is_paused(),
        "paused_since": pause.paused_since(),
        "battery": battery,
        "signal": signal,
        "signal_note": signal_note,
        "camera_found": camera_found,
        "camera_unavailable": camera_unavailable,
        "frames_today": frames_today,
        "frames_total": frames_total,
        "errors_today": errors_today,
        "last_capture": last_capture,
        "recent": recent,
        "interval_min": config.CAPTURE_INTERVAL_MIN,
        "camera_name": config.CAMERA_NAME or "(camera not set)",
        "sunrise": sunrise,
        "sunset": sunset,
        "window_start": win_start,
        "window_stop": win_stop,
        "window_mode": window_mode,
        "is_daylight": is_day,
    }


# ---------------------------------------------------------------------------
# Tail of the rotating app log, for the dashboard's "Logs" panel. We only ever
# read the LAST chunk of the file (never the whole 5 MB) so this is cheap, and
# it is wrapped so a read error becomes a friendly message rather than a 500.
# ---------------------------------------------------------------------------
def _tail_log(max_lines: int = 200, max_bytes: int = 200_000) -> str:
    """Return roughly the last `max_lines` lines of /data/logs/app.log."""
    try:
        path = config.APP_LOG
        if not path.exists():
            return "(no log file yet)"
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial line we landed in the middle of
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]) if lines else "(log is empty)"
    except Exception as exc:
        log.debug("Could not read app log: %s", exc)
        return "(could not read log)"


def _clear_log() -> bool:
    """Empty the live app.log and delete rotated backups.

    We truncate through the running RotatingFileHandler's own stream (under its
    lock) so logging keeps writing cleanly afterwards instead of producing a
    sparse file. Falls back to truncating the path directly if no handler is found.
    """
    ok = False
    for h in list(logging.getLogger().handlers):
        if isinstance(h, RotatingFileHandler):
            try:
                h.acquire()
                try:
                    if h.stream:
                        h.stream.seek(0)
                        h.stream.truncate()
                        h.stream.flush()
                    ok = True
                finally:
                    h.release()
            except Exception as exc:
                log.debug("Could not truncate log handler: %s", exc)
    if not ok:
        try:
            open(config.APP_LOG, "w").close()
            ok = True
        except Exception as exc:
            log.debug("Could not truncate log file: %s", exc)
    # Remove rotated backups (app.log.1, app.log.2, ...) for a complete clear.
    try:
        for p in config.LOG_DIR.glob("app.log.*"):
            p.unlink()
    except Exception:
        pass
    return ok


def _changelog_html() -> str:
    """Render version.CHANGELOG as the HTML shown in the version-history popup."""
    rows = []
    for rel in version.CHANGELOG:
        items = "".join("<li>" + html.escape(c) + "</li>" for c in rel.get("changes", []))
        rows.append(
            '<div class="rel"><h4>v' + html.escape(str(rel.get("version", "")))
            + '<span class="date">' + html.escape(str(rel.get("date", ""))) + "</span></h4>"
            + "<ul>" + items + "</ul></div>"
        )
    return "".join(rows)


# ---------------------------------------------------------------------------
# The HTML page. It is one self-contained string: HTML + CSS + JS. The JS polls
# /api/status every 30s, wires up Pause/Resume, and drives the Settings editor
# (which talks to /api/settings). Look & feel is an 80s "synthwave" theme --
# neon purple/cyan, a glowing perspective grid, chrome heading. Mobile-friendly.
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE_TAB}}</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="180x180" href="/icon.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-title" content="{{TITLE_APP}}">
<!-- Link-preview / share card (Open Graph + Twitter). -->
<meta property="og:type" content="website">
<meta property="og:title" content="{{TITLE_TAB}}">
<meta property="og:url" content="{{OG_URL}}">
<meta property="og:image" content="{{OG_IMAGE}}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{{TITLE_TAB}}">
<meta name="twitter:image" content="{{OG_IMAGE}}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Audiowide&display=swap" rel="stylesheet">
<style>
  :root {
    color-scheme: dark;
    --bg:#0a0118; --bg2:#150a2e;
    --pink:#ff2e88; --purple:#a64bff; --cyan:#1fe5ff; --blue:#2b6bff;
    --ink:#eae4ff; --dim:#9a86c9; --line:#3a1f6e;
    --panel: rgba(28,12,58,0.55);
    --glow-pink: 0 0 8px rgba(255,46,136,.8), 0 0 18px rgba(255,46,136,.5);
    --glow-cyan: 0 0 8px rgba(31,229,255,.8), 0 0 18px rgba(31,229,255,.45);
    --font: "Orbitron", "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
    --display: "Audiowide", "Orbitron", sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; }
  body {
    min-height:100vh; color:var(--ink); font-family:var(--font);
    background:
      radial-gradient(120% 80% at 50% -10%, #2a1163 0%, var(--bg2) 35%, var(--bg) 75%) fixed;
    position:relative; overflow-x:hidden;
  }
  /* The glowing perspective grid that recedes to the horizon, like the poster. */
  .grid-floor {
    position:fixed; left:-25%; right:-25%; bottom:0; height:48vh; z-index:0;
    background-image:
      linear-gradient(to right, rgba(31,229,255,.5) 1px, transparent 1px),
      linear-gradient(to top, rgba(255,46,136,.5) 1px, transparent 1px);
    background-size: 44px 44px;
    transform: perspective(40vh) rotateX(72deg);
    transform-origin: bottom center;
    animation: roll 6s linear infinite;
    -webkit-mask-image: linear-gradient(to top, #000 0%, rgba(0,0,0,.15) 70%, transparent 100%);
            mask-image: linear-gradient(to top, #000 0%, rgba(0,0,0,.15) 70%, transparent 100%);
    pointer-events:none;
  }
  @keyframes roll { from { background-position: 0 0; } to { background-position: 0 44px; } }
  /* A soft sun glow behind the title. */
  .sun-glow {
    position:fixed; top:-30vh; left:50%; transform:translateX(-50%);
    width:80vh; height:80vh; border-radius:50%; z-index:0; pointer-events:none;
    background: radial-gradient(circle, rgba(255,46,136,.28) 0%, rgba(166,75,255,.14) 40%, transparent 70%);
  }
  .wrap { position:relative; z-index:1; max-width:760px; margin:0 auto; padding:20px 16px 60px; }

  header { text-align:center; margin:6px 0 18px; }
  .eyebrow { font-family:var(--font); letter-spacing:.5em; font-size:.62rem; color:var(--cyan);
             text-shadow:var(--glow-cyan); text-transform:uppercase; margin-bottom:6px; }
  h1 {
    font-family:var(--display); font-weight:700; margin:0 auto; line-height:1.02;
    /* Two stacked lines, each justified to this exact width so both lines end up
       the same width. Font size is capped so the longer line never overflows. */
    width: min(88vw, 460px);
    font-size: clamp(1.4rem, 7.5vw, 2.5rem); letter-spacing:0;
    background: linear-gradient(180deg, #fff 0%, #ffd7f0 18%, var(--pink) 50%, var(--purple) 78%, var(--blue) 100%);
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
    filter: drop-shadow(0 2px 0 rgba(43,107,255,.6)) drop-shadow(0 0 18px rgba(255,46,136,.55));
  }
  /* Each line is a flex row; space-between stretches its letters across the full
     width of the h1, so both title lines come out identical widths. */
  h1 .line { display:flex; justify-content:space-between; width:100%; }
  h1 .line .sp { width:0; }  /* the gap between BIKE and PARK */
  .tagline { font-family:var(--font); letter-spacing:.34em; font-size:.7rem; color:var(--dim);
             text-transform:uppercase; margin-top:8px; }

  .banner {
    border-radius:12px; padding:12px 16px; margin:16px 0; font-weight:700; text-align:center;
    letter-spacing:.12em; text-transform:uppercase; font-size:.85rem;
    backdrop-filter: blur(6px); border:1px solid;
  }
  .banner.running { background:rgba(31,229,255,.08); color:var(--cyan);  border-color:var(--cyan);  box-shadow:var(--glow-cyan); }
  .banner.paused  { background:rgba(255,206,107,.08); color:#ffd16b; border-color:#ffb443; box-shadow:0 0 8px rgba(255,180,67,.7); }

  button {
    width:100%; padding:15px; font-family:var(--font); font-size:1rem; font-weight:700;
    letter-spacing:.14em; text-transform:uppercase; border-radius:12px; cursor:pointer;
    color:#fff; margin-bottom:18px; border:1px solid; transition:transform .08s, box-shadow .2s;
  }
  button:active { transform:translateY(1px); }
  button.pause  { background:linear-gradient(180deg,#ff5ca0,#c41f73); border-color:var(--pink); box-shadow:var(--glow-pink); }
  button.resume { background:linear-gradient(180deg,#37f0c8,#0fb38f); border-color:#23e0b0; box-shadow:0 0 10px rgba(35,224,176,.7); }
  button.test   { background:linear-gradient(180deg,#3f86ff,#1746b8); border-color:#4f86ff; box-shadow:var(--glow-cyan); }
  button:disabled { opacity:.55; cursor:wait; }

  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .card {
    background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px;
    backdrop-filter: blur(6px); box-shadow: inset 0 0 22px rgba(166,75,255,.10);
  }
  .card .label { color:var(--cyan); font-size:.66rem; text-transform:uppercase; letter-spacing:.18em; }
  .card .value { font-family:var(--display); font-size:1.8rem; margin-top:6px; color:#fff;
                 text-shadow:0 0 10px rgba(31,229,255,.45); }
  .card .value small { font-size:.85rem; color:var(--dim); font-family:var(--font); }
  .low { color:var(--pink) !important; text-shadow:var(--glow-pink) !important; }

  img.snap { width:100%; border-radius:12px; margin:16px 0; border:1px solid var(--purple);
             box-shadow:0 0 20px rgba(166,75,255,.4); }

  h3 { font-family:var(--font); letter-spacing:.2em; text-transform:uppercase; font-size:.8rem;
       color:var(--pink); text-shadow:var(--glow-pink); margin:22px 0 6px; }
  table { width:100%; border-collapse:collapse; font-size:.78rem; margin-top:4px; }
  th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:600; letter-spacing:.08em; text-transform:uppercase; font-size:.66rem; }
  td.ok { color:#37f0c8; } td.fail { color:var(--pink); }

  .foot { color:var(--dim); font-size:.72rem; text-align:center; margin-top:22px; letter-spacing:.06em; }
  .gearbtn {
    background:var(--panel); border:1px solid var(--purple); color:var(--ink);
    width:auto; margin:14px auto 0; display:block; padding:11px 22px; font-size:.78rem;
    box-shadow:inset 0 0 18px rgba(166,75,255,.2);
  }
  /* Scrollable log viewer at the bottom of the page. */
  .logs {
    background:rgba(5,1,18,.85); border:1px solid var(--line); border-radius:12px;
    padding:12px; margin:6px 0 0; max-height:300px; overflow:auto;
    font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size:.7rem; line-height:1.45; color:var(--dim); white-space:pre-wrap;
    word-break:break-word; box-shadow:inset 0 0 22px rgba(166,75,255,.10);
  }
  .logbtns { display:flex; gap:12px; justify-content:center; flex-wrap:wrap; }
  .logbtns .gearbtn { margin:14px 0 0; }

  /* Video build controls. */
  .vopts { margin:6px 0 12px; display:flex; gap:18px; flex-wrap:wrap; justify-content:center; }
  .chk { display:inline-flex; align-items:center; gap:7px; font-size:.82rem; color:var(--ink);
         font-weight:400; cursor:pointer; }
  .chk input { width:auto; }
  .gearbtn.dl { border-color:#23e0b0; color:#9af5dd; box-shadow:0 0 10px rgba(35,224,176,.4); }
  .buildstat { font-size:.78rem; color:var(--dim); margin-top:8px; text-align:center; min-height:1.2em; }

  /* Version number + history popup. */
  .version { text-align:center; margin-top:14px; }
  .verbtn {
    width:auto; margin:0; padding:6px 12px; background:none; border:none; box-shadow:none;
    color:var(--dim); font-size:.72rem; letter-spacing:.08em; text-transform:none;
    cursor:pointer; transition:color .2s;
  }
  .verbtn:hover { color:var(--cyan); text-shadow:var(--glow-cyan); }
  .changelog { max-height:55vh; overflow-y:auto; }
  .changelog .rel { margin:14px 0; }
  .changelog .rel h4 { margin:0 0 4px; font-family:var(--font); font-size:.82rem;
                       letter-spacing:.05em; color:var(--cyan); }
  .changelog .rel .date { color:var(--dim); font-weight:400; font-size:.72rem; margin-left:8px; }
  .changelog ul { margin:4px 0 0; padding-left:18px; }
  .changelog li { font-size:.78rem; color:var(--ink); margin:3px 0; line-height:1.4; }

  /* ---- Settings overlay ---- */
  .overlay {
    position:fixed; inset:0; z-index:50; display:none; padding:18px;
    background:rgba(5,1,15,.82); backdrop-filter:blur(4px); overflow-y:auto;
  }
  .overlay.open { display:block; }
  .sheet {
    position:relative; max-width:680px; margin:0 auto;
    background:linear-gradient(180deg,#1a0c3b,#120726);
    border:1px solid var(--purple); border-radius:16px; padding:18px;
    box-shadow:0 0 40px rgba(166,75,255,.5);
  }
  /* Corner close button for overlay sheets. */
  .sheet-x {
    position:absolute; top:12px; right:12px; width:34px; height:34px; padding:0; margin:0;
    border-radius:9px; font-size:.85rem; line-height:1; background:rgba(10,1,24,.7);
    border:1px solid var(--line); color:var(--dim); box-shadow:none;
  }
  .sheet-x:hover { color:var(--pink); border-color:var(--pink); box-shadow:var(--glow-pink); }
  .sheet h2 { font-family:var(--display); font-size:1.3rem; margin:0 0 2px;
              background:linear-gradient(90deg,var(--cyan),var(--pink)); -webkit-background-clip:text;
              background-clip:text; -webkit-text-fill-color:transparent; }
  .sheet .note { color:var(--dim); font-size:.74rem; margin-bottom:14px; }
  .group-title { font-family:var(--font); letter-spacing:.16em; text-transform:uppercase;
                 font-size:.72rem; color:var(--cyan); margin:18px 0 8px; border-bottom:1px solid var(--line);
                 padding-bottom:6px; }
  .field { margin-bottom:13px; }
  .field label { display:block; font-size:.8rem; color:var(--ink); margin-bottom:4px; font-weight:600; }
  .field .badge { font-size:.58rem; letter-spacing:.1em; color:#ffd16b; border:1px solid #ffb443;
                  border-radius:6px; padding:1px 6px; margin-left:8px; text-transform:uppercase; }
  .field .hint { font-size:.7rem; color:var(--dim); margin-top:4px; }
  .field input, .field select {
    width:100%; padding:10px 12px; border-radius:9px; background:rgba(10,1,24,.7);
    border:1px solid var(--line); color:#fff; font-family:var(--font); font-size:.92rem;
  }
  .field input:focus, .field select:focus { outline:none; border-color:var(--cyan);
                                             box-shadow:0 0 8px rgba(31,229,255,.5); }
  .row2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .actions { display:flex; gap:12px; margin-top:18px; }
  .actions button { margin:0; }
  .btn-ghost { background:transparent; border-color:var(--dim); color:var(--dim);
               box-shadow:none; letter-spacing:.1em; }
  .btn-save { background:linear-gradient(180deg,#37f0c8,#0fb38f); border-color:#23e0b0;
              box-shadow:0 0 10px rgba(35,224,176,.6); }
  .toast {
    position:fixed; left:50%; bottom:24px; transform:translateX(-50%) translateY(40px);
    background:#120726; border:1px solid var(--cyan); color:var(--cyan); padding:12px 20px;
    border-radius:10px; z-index:90; opacity:0; transition:all .25s; box-shadow:var(--glow-cyan);
    font-size:.82rem; letter-spacing:.05em; max-width:90vw; text-align:center;
  }
  .toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  .toast.err { border-color:var(--pink); color:var(--pink); box-shadow:var(--glow-pink); }

  /* ---- Captures gallery ---- */
  .captbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:4px 0 12px; }
  .captbar .gearbtn { margin:0; padding:9px 12px; font-size:.68rem; }
  .captbar input[type=date] {
    padding:8px 10px; border-radius:9px; background:rgba(10,1,24,.7);
    border:1px solid var(--line); color:#fff; font-family:var(--font); font-size:.78rem;
  }
  .gearbtn.danger { border-color:var(--pink); color:#ff9ccb; box-shadow:0 0 10px rgba(255,46,136,.3); }
  .btn-danger { background:linear-gradient(180deg,#ff5ca0,#c41f73); border-color:var(--pink); box-shadow:var(--glow-pink); }
  .thumbgrid { display:grid; grid-template-columns:repeat(auto-fill, minmax(96px, 1fr)); gap:8px; margin:8px 0 4px; }
  .thumb { position:relative; border:1px solid var(--line); border-radius:8px; overflow:hidden;
           cursor:pointer; background:rgba(10,1,24,.6); }
  .thumb img { width:100%; height:72px; object-fit:cover; display:block; }
  .thumb .t { position:absolute; left:4px; bottom:4px; font-size:.6rem; color:var(--ink);
              background:rgba(5,1,18,.78); padding:1px 5px; border-radius:5px; letter-spacing:.05em; }
  .thumb .x { position:absolute; top:3px; right:3px; width:22px; height:22px; padding:0; margin:0;
              border-radius:6px; font-size:.68rem; line-height:1; background:rgba(5,1,18,.78);
              border:1px solid var(--pink); color:var(--pink); box-shadow:none; }
  .thumb.sel { outline:2px solid var(--cyan); outline-offset:-2px; box-shadow:var(--glow-cyan); }
  .thumb .tick { position:absolute; top:3px; left:3px; width:20px; height:20px; border-radius:6px;
                 display:none; align-items:center; justify-content:center; font-size:.7rem;
                 background:var(--cyan); color:#04202a; font-weight:900; }
  .thumb.sel .tick { display:flex; }

  @media (max-width:480px) { .row2 { grid-template-columns:1fr; } }
</style>
</head>
<body>
  <div class="sun-glow"></div>
  <div class="grid-floor"></div>

  <div class="wrap">
    <header>
      <div class="eyebrow">Now Streaming</div>
      <h1>{{TITLE_H1}}</h1>
      <div class="tagline" id="subtitle">Time-Lapse · loading…</div>
    </header>

    <div class="banner" id="banner">…</div>
    <button id="toggle" disabled>…</button>
    <button class="test" id="testCapture">📷 Test capture now</button>
    <div id="testStatus" class="buildstat" style="margin:-10px 0 16px;"></div>

    <div class="grid">
      <div class="card"><div class="label">Battery</div><div class="value" id="battery">…</div></div>
      <div class="card"><div class="label">Signal</div><div class="value" id="signal">…</div></div>
      <div class="card"><div class="label">Photos today</div><div class="value" id="today">…</div></div>
      <div class="card"><div class="label">Photos all-time</div><div class="value" id="total">…</div></div>
    </div>

    <!-- No static src: the JS sets a cache-busted URL on load and every refresh,
         so phones / home-screen web apps never show a stale cached photo. -->
    <img class="snap" id="snap" alt="latest snapshot"
         onerror="this.style.display='none'">
    <button class="gearbtn" id="openCaptures" style="margin:-6px auto 16px;">🖼  View captures</button>

    <div class="card">
      <div class="label">Capture window</div>
      <div id="daylight" style="margin-top:8px; font-size:.86rem;">…</div>
    </div>

    <h3>Recent captures</h3>
    <table>
      <thead><tr><th>Time</th><th>OK?</th><th>Batt</th><th>Sig</th><th>Note</th></tr></thead>
      <tbody id="recent"></tbody>
    </table>

    <h3>Time-lapse video</h3>
    <div class="card">
      <div class="field"><label>Frames per second</label>
        <input type="number" id="vfps" value="30" min="1" max="60" step="1"></div>
      <div class="vopts">
        <label class="chk"><input type="checkbox" id="voverlay" {{OVERLAY_DEFAULT}}> Date/time overlay</label>
        <label class="chk"><input type="checkbox" id="vdeflicker" checked> Smooth flicker</label>
      </div>
      <button class="gearbtn" id="buildVideo">🎬  Build video</button>
      <a id="buildDownload" class="gearbtn dl" style="display:none" href="/api/build/video">⬇  Download latest video</a>
      <div id="buildStatus" class="buildstat">Idle.</div>
      <div class="hint" style="margin-top:8px;">Builds one video from all captured frames.
        This can take several minutes for a long project; you can leave the page.</div>
    </div>

    <h3>Logs</h3>
    <pre class="logs" id="logs">Loading…</pre>
    <div class="logbtns">
      <button class="gearbtn" id="refreshLogs">⟳  Refresh logs</button>
      <button class="gearbtn" id="copyLogs">⧉  Copy logs</button>
      <button class="gearbtn" id="clearLogs">🗑  Clear logs</button>
    </div>

    <button class="gearbtn" id="openSettings">⚙  Settings</button>
    <div class="foot" id="foot">…</div>
    <div class="version">
      <button type="button" class="verbtn" id="openChangelog">v{{VERSION}} · what's new ⓘ</button>
    </div>
  </div>

  <!-- Version history -->
  <div class="overlay" id="changelogOverlay">
    <div class="sheet">
      <h2>📋 Version history</h2>
      <div class="note">You're on <b>v{{VERSION}}</b>.</div>
      <div class="changelog">{{CHANGELOG}}</div>
      <div class="actions">
        <button type="button" class="btn-ghost" id="closeChangelog">Close</button>
      </div>
    </div>
  </div>

  <!-- Settings editor -->
  <div class="overlay" id="overlay">
    <div class="sheet">
      <h2>⚙ SETTINGS</h2>
      <div class="note">Change anything here. Most settings apply instantly; ones marked
        <span class="badge">restart</span> take effect next time the container restarts.
        Password fields are left blank — type a new one only to change it.</div>
      <form id="settingsForm"><div id="formBody">Loading…</div>
        <div class="actions">
          <button type="button" class="btn-ghost" id="closeSettings">Cancel</button>
          <button type="submit" class="btn-save">Save changes</button>
        </div>
      </form>
    </div>
  </div>

  <!-- Captures gallery -->
  <div class="overlay" id="capturesOverlay">
    <div class="sheet">
      <button type="button" class="sheet-x" id="closeCapturesX" aria-label="Close" title="Close">✕</button>
      <h2>🖼 CAPTURES</h2>
      <div class="note" id="capCount">Loading…</div>
      <div class="captbar">
        <button type="button" class="gearbtn" id="capSelectMode">☑ Select</button>
        <button type="button" class="gearbtn danger" id="capDeleteSel" style="display:none">🗑 Delete selected (0)</button>
      </div>
      <div class="captbar">
        <input type="date" id="capBeforeDate">
        <button type="button" class="gearbtn danger" id="capClearBefore">🗑 Clear before date</button>
        <button type="button" class="gearbtn danger" id="capClearAll">🗑 Clear ALL</button>
      </div>
      <div id="capBody">Loading…</div>
      <div class="actions">
        <button type="button" class="btn-ghost" id="closeCaptures">Close</button>
      </div>
    </div>
  </div>

  <!-- Generic confirm dialog (deletions + unsaved-settings). Sits above the
       other overlays and only closes via an explicit button press. -->
  <div class="overlay" id="confirmOverlay" style="z-index:70;">
    <div class="sheet" style="max-width:420px;">
      <h2 id="confirmTitle">Are you sure?</h2>
      <div class="note" id="confirmMsg" style="margin-top:10px;"></div>
      <div class="actions" id="confirmBtns"></div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

<script>
let busy = false;

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function toast(msg, isErr){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._t); t._t = setTimeout(()=>{ t.className = 'toast'; }, 3500);
}

// A themed replacement for confirm(): shows a title, a message and the given
// buttons, and resolves with the value of whichever button was pressed. It only
// closes via a button, so a stray tap can't accidentally answer the question.
function askModal(title, msg, buttons){
  return new Promise(resolve => {
    const ov = document.getElementById('confirmOverlay');
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMsg').textContent = msg;
    const box = document.getElementById('confirmBtns');
    box.innerHTML = '';
    buttons.forEach(b => {
      const el = document.createElement('button');
      el.type = 'button'; el.className = b.cls; el.textContent = b.label;
      el.addEventListener('click', () => { ov.classList.remove('open'); resolve(b.value); });
      box.appendChild(el);
    });
    ov.classList.add('open');
  });
}

// ---- Live status ----------------------------------------------------------
async function refresh() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    paint(await r.json());
  } catch (e) {
    document.getElementById('subtitle').textContent = 'Could not reach the daemon.';
  }
}

function paint(s) {
  const banner = document.getElementById('banner');
  const btn = document.getElementById('toggle');
  if (s.paused) {
    banner.className = 'banner paused';
    banner.textContent = '⏸ Paused' + (s.paused_since ? ' · since ' + s.paused_since : '');
    btn.className = 'resume'; btn.textContent = '▶ Resume captures';
  } else {
    banner.className = 'banner running';
    banner.textContent = s.is_daylight ? '● Running' : '● Running · waiting for window';
    btn.className = 'pause'; btn.textContent = '⏸ Pause captures';
  }
  if (!busy) btn.disabled = false;

  document.getElementById('subtitle').textContent =
    'Time-Lapse · every ' + s.interval_min + ' min · ' + s.now;

  const b = document.getElementById('battery');
  const bn = parseFloat(s.battery);
  b.innerHTML = (s.battery === '?' ? '?' : esc(s.battery) + '<small>%</small>');
  b.className = 'value' + (!isNaN(bn) && bn < 20 ? ' low' : '');

  document.getElementById('signal').innerHTML =
    (s.signal === '?' ? '?' : esc(s.signal) + '<small> / 5'
      + (s.signal_note ? ' · ' + esc(s.signal_note) : '') + '</small>');
  document.getElementById('today').textContent = s.frames_today;
  document.getElementById('total').textContent = s.frames_total;

  let dl = '—';
  if (s.window_start) {
    const mode = s.window_mode === 'fixed' ? 'fixed times' : 'sun ' + s.sunrise + '–' + s.sunset;
    dl = 'Capturing <b>' + esc(s.window_start) + '</b> → <b>' + esc(s.window_stop) + '</b>'
       + ' &nbsp;·&nbsp; ' + mode
       + ' &nbsp;·&nbsp; ' + (s.is_daylight ? 'open now ☀️' : 'closed 🌙');
  }
  document.getElementById('daylight').innerHTML = dl;

  const tb = document.getElementById('recent');
  tb.innerHTML = '';
  (s.recent || []).forEach(row => {
    const ok = row.success === 'yes';
    tb.innerHTML += '<tr><td>' + esc(row.timestamp) + '</td>'
      + '<td class="' + (ok ? 'ok' : 'fail') + '">' + (ok ? '✓' : '✕') + '</td>'
      + '<td>' + esc(row.battery) + '</td>'
      + '<td>' + esc(row.signal) + '</td>'
      + '<td>' + esc(row.note) + '</td></tr>';
  });

  const img = document.getElementById('snap');
  img.style.display = '';
  if (!busy) img.src = '/latest.jpg?t=' + Date.now();

  document.getElementById('foot').textContent =
    'Errors today: ' + s.errors_today + ' · auto-refreshes every 30s';
}

// Pause / Resume.
document.getElementById('toggle').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const action = btn.className === 'pause' ? 'pause' : 'resume';
  busy = true; btn.disabled = true; btn.textContent = 'Working…';
  try { await fetch('/api/' + action, { method: 'POST' }); } catch (err) {}
  busy = false;
  await refresh();
});

// Test capture: force a snapshot now and report the result.
document.getElementById('testCapture').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const st = document.getElementById('testStatus');
  const label = btn.textContent;
  busy = true; btn.disabled = true; btn.textContent = '📷 Capturing… (up to ~1 min)';
  st.style.color = ''; st.textContent = 'Waking the camera and requesting a fresh snapshot…';
  try {
    const res = await (await fetch('/api/test-capture', { method: 'POST' })).json();
    if (res.ok) {
      st.style.color = '#37f0c8';
      st.textContent = '✓ ' + (res.note || 'Captured.')
        + (res.battery != null ? '  ·  batt ' + esc(res.battery) + '%  ·  sig ' + esc(res.signal) : '');
      toast('Test snapshot captured.');
    } else {
      st.style.color = 'var(--pink)';
      st.textContent = '✕ ' + (res.note || 'Capture failed.');
      toast(res.note || 'Test capture failed.', true);
    }
  } catch (err) {
    st.style.color = 'var(--pink)';
    st.textContent = '✕ Could not reach the daemon.';
    toast('Could not run test capture.', true);
  }
  busy = false; btn.disabled = false; btn.textContent = label;
  await refresh();  // pull in the new latest.jpg + status row
});

// ---- Settings editor -------------------------------------------------------
const overlay = document.getElementById('overlay');

function fieldHtml(f) {
  const badge = f.restart ? ' <span class="badge">restart</span>' : '';
  let input;
  if (f.type === 'choice') {
    const opts = (f.choices||[]).map(c =>
      '<option value="'+esc(c)+'"'+(String(c)===String(f.value)?' selected':'')+'>'+esc(c)+'</option>').join('');
    input = '<select name="'+esc(f.key)+'">'+opts+'</select>';
  } else if (f.type === 'secret') {
    const ph = f.is_set ? '•••••••• (unchanged)' : 'not set';
    input = '<input type="password" name="'+esc(f.key)+'" autocomplete="new-password" placeholder="'+ph+'">';
  } else {
    const t = (f.type==='int'||f.type==='float') ? 'number' : 'text';
    const step = f.type==='float' ? ' step="any"' : (f.type==='int' ? ' step="1"' : '');
    input = '<input type="'+t+'"'+step+' name="'+esc(f.key)+'" value="'+esc(f.value)+'">';
  }
  return '<div class="field"><label>'+esc(f.label)+badge+'</label>'+input
       + (f.help ? '<div class="hint">'+esc(f.help)+'</div>' : '') + '</div>';
}

// A JSON snapshot of the form taken right after it loads; comparing against it
// later tells us whether the user has unsaved edits.
let settingsInitial = null;

function settingsState() {
  const obj = {};
  new FormData(document.getElementById('settingsForm')).forEach((v, k) => { obj[k] = v; });
  return JSON.stringify(obj);
}

async function loadSettings() {
  const body = document.getElementById('formBody');
  body.innerHTML = 'Loading…';
  settingsInitial = null;
  try {
    const r = await fetch('/api/settings', { cache:'no-store' });
    const data = await r.json();
    body.innerHTML = data.groups.map(g =>
      '<div class="group-title">'+esc(g.group)+'</div>' +
      g.fields.map(fieldHtml).join('')
    ).join('');
    settingsInitial = settingsState();
  } catch (e) {
    body.innerHTML = '<div class="hint">Could not load settings.</div>';
  }
}

async function saveSettings() {
  const form = document.getElementById('settingsForm');
  const payload = {};
  new FormData(form).forEach((v, k) => { payload[k] = v; });
  const saveBtn = form.querySelector('.btn-save');
  saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
  try {
    const r = await fetch('/api/settings', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
    });
    const res = await r.json();
    if (r.ok) {
      // The title/header is rendered server-side, so reload the whole page to pick
      // up any header (or other) change immediately rather than just re-polling.
      location.reload();
      return;
    } else {
      toast(res.error || 'Could not save.', true);
    }
  } catch (err) {
    toast('Could not reach the daemon.', true);
  }
  saveBtn.disabled = false; saveBtn.textContent = 'Save changes';
}

// Closing the settings sheet (Cancel button or a tap on the backdrop) checks
// for unsaved edits first and offers Save / Discard / keep editing.
async function attemptCloseSettings() {
  if (settingsInitial !== null && settingsState() !== settingsInitial) {
    const ans = await askModal('Unsaved changes',
      'You changed some settings but have not saved them yet.', [
      {label:'Save',    cls:'btn-save',   value:'save'},
      {label:'Discard', cls:'btn-danger', value:'discard'},
      {label:'Cancel',  cls:'btn-ghost',  value:'cancel'},
    ]);
    if (ans === 'save') { await saveSettings(); return; }  // reloads the page on success
    if (ans !== 'discard') return;                         // cancel -> keep editing
  }
  overlay.classList.remove('open');
}

document.getElementById('openSettings').addEventListener('click', () => {
  overlay.classList.add('open'); loadSettings();
});
document.getElementById('closeSettings').addEventListener('click', attemptCloseSettings);
overlay.addEventListener('click', (e) => { if (e.target === overlay) attemptCloseSettings(); });

document.getElementById('settingsForm').addEventListener('submit', (e) => {
  e.preventDefault();
  saveSettings();
});

// ---- Logs ------------------------------------------------------------------
async function loadLogs() {
  const el = document.getElementById('logs');
  // Keep the view pinned to the newest lines only if the user is already there.
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  try {
    const r = await fetch('/api/logs', { cache: 'no-store' });
    el.textContent = await r.text();
    if (atBottom) el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.textContent = 'Could not load logs.';
  }
}
document.getElementById('refreshLogs').addEventListener('click', loadLogs);

async function copyLogs() {
  await loadLogs();  // refresh to the very latest right before copying
  const text = document.getElementById('logs').textContent || '';
  // navigator.clipboard only works in a secure context (https/localhost); the
  // dashboard is usually plain http on the LAN, so fall back to execCommand.
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.focus(); ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (!ok) throw new Error('execCommand failed');
    }
    toast('Logs copied to clipboard.');
  } catch (e) {
    toast('Could not copy logs.', true);
  }
}
document.getElementById('copyLogs').addEventListener('click', copyLogs);

async function clearLogs() {
  if (!confirm('Clear the log file? This deletes the current log and its backups and cannot be undone.')) return;
  try {
    const r = await fetch('/api/logs/clear', { method: 'POST' });
    toast(r.ok ? 'Logs cleared.' : 'Could not clear logs.', !r.ok);
  } catch (e) {
    toast('Could not clear logs.', true);
  }
  await loadLogs();
}
document.getElementById('clearLogs').addEventListener('click', clearLogs);

// ---- Time-lapse video build ------------------------------------------------
let buildTimer = null;

function paintBuild(s) {
  const el = document.getElementById('buildStatus');
  const dl = document.getElementById('buildDownload');
  const btn = document.getElementById('buildVideo');
  if (s.state === 'running') {
    btn.disabled = true; btn.textContent = '⏳  Building…';
    el.textContent = (s.phase || 'Working…') + (s.elapsed_sec != null ? '  ·  ' + s.elapsed_sec + 's' : '');
    dl.style.display = 'none';
  } else {
    btn.disabled = false; btn.textContent = '🎬  Build video';
    if (s.state === 'done' && s.output_name) {
      const mb = s.output_size ? '  (' + Math.round(s.output_size / 1048576) + ' MB)' : '';
      const fr = s.frames ? '  ·  ' + s.frames + ' frames' : '';
      el.textContent = '✓ ' + s.output_name + mb + fr;
      dl.style.display = ''; dl.href = '/api/build/video?t=' + Date.now();
    } else if (s.state === 'error') {
      el.textContent = '✕ ' + (s.error || 'build failed — see Logs'); dl.style.display = 'none';
    } else {
      el.textContent = 'Idle.'; dl.style.display = 'none';
    }
  }
}

async function refreshBuild() {
  try {
    const s = await (await fetch('/api/build/status', { cache: 'no-store' })).json();
    paintBuild(s);
    if (s.state === 'running' && !buildTimer) buildTimer = setInterval(refreshBuild, 3000);
    if (s.state !== 'running' && buildTimer) { clearInterval(buildTimer); buildTimer = null; }
  } catch (e) {}
}

document.getElementById('buildVideo').addEventListener('click', async () => {
  const payload = {
    fps: parseInt(document.getElementById('vfps').value, 10) || 30,
    overlay: document.getElementById('voverlay').checked,
    deflicker: document.getElementById('vdeflicker').checked,
  };
  const btn = document.getElementById('buildVideo');
  btn.disabled = true; btn.textContent = '⏳  Starting…';
  try {
    const r = await fetch('/api/build/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    const res = await r.json();
    if (!r.ok) toast(res.message || 'Could not start build.', true);
  } catch (e) { toast('Could not start build.', true); }
  refreshBuild();
});

// ---- Captures gallery --------------------------------------------------------
const capOverlay = document.getElementById('capturesOverlay');
let capSelecting = false;   // selection mode on/off
let capDays = [];           // the /api/captures payload
const capSel = new Set();   // selected frames, as "YYYY-MM-DD/arlo_....jpg"

function capUpdateBar(){
  const del = document.getElementById('capDeleteSel');
  del.style.display = capSelecting ? '' : 'none';
  del.textContent = '🗑 Delete selected (' + capSel.size + ')';
  document.getElementById('capSelectMode').textContent = capSelecting ? '✓ Done selecting' : '☑ Select';
}

function renderCaptures(){
  const body = document.getElementById('capBody');
  const total = capDays.reduce((n, d) => n + d.frames.length, 0);
  document.getElementById('capCount').textContent = total
    ? total + ' photo' + (total === 1 ? '' : 's') + ' across ' + capDays.length
      + ' day' + (capDays.length === 1 ? '' : 's') + '. Tap a photo to view it full-size.'
    : 'No captures yet.';
  body.innerHTML = capDays.map(d =>
    '<div class="group-title">' + esc(d.date) + ' · ' + d.frames.length + '</div>'
    + '<div class="thumbgrid">' + d.frames.map(f => {
        const rel = d.date + '/' + f;
        // arlo_YYYYMMDD_HHMMSS.jpg -> "HH:MM"
        const t = f.slice(14, 16) + ':' + f.slice(16, 18);
        return '<div class="thumb' + (capSel.has(rel) ? ' sel' : '') + '" data-rel="' + esc(rel) + '">'
          + '<img loading="lazy" src="/snapshots/' + esc(rel) + '" alt="">'
          + '<span class="tick">✓</span>'
          + '<span class="t">' + t + '</span>'
          + '<button type="button" class="x" title="Delete this photo">✕</button>'
          + '</div>';
      }).join('') + '</div>'
  ).join('');
  capUpdateBar();
}

async function loadCaptures(){
  const body = document.getElementById('capBody');
  body.innerHTML = 'Loading…';
  try {
    const r = await fetch('/api/captures', { cache:'no-store' });
    capDays = (await r.json()).days || [];
    // Drop selections for photos that no longer exist.
    const alive = new Set();
    capDays.forEach(d => d.frames.forEach(f => alive.add(d.date + '/' + f)));
    [...capSel].forEach(rel => { if (!alive.has(rel)) capSel.delete(rel); });
    renderCaptures();
  } catch (e) {
    body.innerHTML = '<div class="hint">Could not load captures.</div>';
  }
}

document.getElementById('openCaptures').addEventListener('click', () => {
  capOverlay.classList.add('open'); loadCaptures();
});
document.getElementById('closeCaptures').addEventListener('click', () => capOverlay.classList.remove('open'));
document.getElementById('closeCapturesX').addEventListener('click', () => capOverlay.classList.remove('open'));
capOverlay.addEventListener('click', (e) => { if (e.target === capOverlay) capOverlay.classList.remove('open'); });

document.getElementById('capSelectMode').addEventListener('click', () => {
  capSelecting = !capSelecting;
  if (!capSelecting) capSel.clear();
  renderCaptures();
});

// One listener handles every thumbnail: the ✕ deletes (after confirming), a tap
// either toggles selection (in select mode) or opens the photo full-size.
document.getElementById('capBody').addEventListener('click', async (e) => {
  const thumb = e.target.closest('.thumb');
  if (!thumb) return;
  const rel = thumb.dataset.rel;
  if (e.target.closest('.x')) {
    const ans = await askModal('Delete this photo?',
      rel + ' will be deleted permanently. This cannot be undone.', [
      {label:'Delete', cls:'btn-danger', value:'yes'},
      {label:'Cancel', cls:'btn-ghost',  value:'no'},
    ]);
    if (ans === 'yes') await capDelete([rel]);
    return;
  }
  if (capSelecting) {
    capSel.has(rel) ? capSel.delete(rel) : capSel.add(rel);
    thumb.classList.toggle('sel', capSel.has(rel));
    capUpdateBar();
  } else {
    window.open('/snapshots/' + rel, '_blank');
  }
});

async function capDelete(files){
  try {
    const r = await fetch('/api/captures/delete', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({files}),
    });
    const res = await r.json();
    toast(r.ok ? 'Deleted ' + res.deleted + ' photo' + (res.deleted === 1 ? '' : 's') + '.'
               : (res.error || 'Delete failed.'), !r.ok);
  } catch (e) { toast('Could not reach the daemon.', true); }
  await loadCaptures();
  refresh();  // photo counts on the main page just changed
}

document.getElementById('capDeleteSel').addEventListener('click', async () => {
  if (!capSel.size) { toast('No photos selected yet — tap photos to select them.', true); return; }
  const n = capSel.size;
  const ans = await askModal('Delete ' + n + ' selected photo' + (n === 1 ? '' : 's') + '?',
    'They will be deleted permanently. This cannot be undone.', [
    {label:'Delete', cls:'btn-danger', value:'yes'},
    {label:'Cancel', cls:'btn-ghost',  value:'no'},
  ]);
  if (ans === 'yes') await capDelete([...capSel]);
});

async function capClear(before){
  try {
    const r = await fetch('/api/captures/clear', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(before ? {before} : {all: true}),
    });
    const res = await r.json();
    toast(r.ok ? 'Deleted ' + res.deleted + ' photo' + (res.deleted === 1 ? '' : 's') + '.'
               : (res.error || 'Clear failed.'), !r.ok);
  } catch (e) { toast('Could not reach the daemon.', true); }
  capSel.clear();
  await loadCaptures();
  refresh();
}

document.getElementById('capClearAll').addEventListener('click', async () => {
  const total = capDays.reduce((n, d) => n + d.frames.length, 0);
  if (!total) { toast('There are no captures to delete.', true); return; }
  const ans = await askModal('Delete ALL captures?',
    'All ' + total + ' photos will be deleted permanently and the time-lapse starts over from nothing. This cannot be undone.', [
    {label:'Delete everything', cls:'btn-danger', value:'yes'},
    {label:'Cancel',            cls:'btn-ghost',  value:'no'},
  ]);
  if (ans === 'yes') await capClear(null);
});

document.getElementById('capClearBefore').addEventListener('click', async () => {
  const d = document.getElementById('capBeforeDate').value;
  if (!d) { toast('Pick a date first (photos BEFORE it will be deleted).', true); return; }
  const ans = await askModal('Delete captures before ' + d + '?',
    'Every photo taken before ' + d + ' will be deleted permanently (photos from ' + d
    + ' itself onward are kept). This cannot be undone.', [
    {label:'Delete', cls:'btn-danger', value:'yes'},
    {label:'Cancel', cls:'btn-ghost',  value:'no'},
  ]);
  if (ans === 'yes') await capClear(d);
});

// ---- Version history popup --------------------------------------------------
const clOverlay = document.getElementById('changelogOverlay');
document.getElementById('openChangelog').addEventListener('click', () => clOverlay.classList.add('open'));
document.getElementById('closeChangelog').addEventListener('click', () => clOverlay.classList.remove('open'));
clOverlay.addEventListener('click', (e) => { if (e.target === clOverlay) clOverlay.classList.remove('open'); });

refresh();
loadLogs();
refreshBuild();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


def _letter_spans(text: str) -> str:
    """Render a title line as the per-letter <span>s the CSS justifies edge-to-edge.

    Each character becomes its own <span> so flex `space-between` stretches the
    letters across the full width; a SPACE becomes an empty `.sp` span (zero width)
    so multi-word lines like "BIKE PARK" keep their word break.
    """
    out = []
    for ch in text:
        if ch == " ":
            out.append('<span class="sp"></span>')
        else:
            out.append("<span>" + html.escape(ch) + "</span>")
    return "".join(out)


def _render_page(base_url: str = "") -> str:
    """Fill the editable title into the page template (server-side, so the browser
    tab and link-preview crawlers — which don't run our JS — get the real title).

    `base_url` is this deployment's absolute origin (e.g. https://example.com),
    derived from the request so the Open Graph share-card URLs are correct for ANY
    domain. It falls back to a relative path if the origin can't be determined.
    """
    line1, line2 = _title_lines()
    full = _title_full()
    h1 = '<span class="line">' + _letter_spans(line1) + "</span>"
    if line2:
        h1 += '\n        <span class="line">' + _letter_spans(line2) + "</span>"
    # Append "Time-Lapse" to the tab/share title only when the title doesn't already
    # say "lapse" -- avoids redundant names like "ARLO TIME LAPSE CAPTURE Time-Lapse".
    tab = full if "lapse" in full.lower() else (full + " Time-Lapse")
    # The video overlay checkbox defaults to the OPPOSITE of the capture-time stamp,
    # so frames are never double-stamped: stamped frames -> overlay off, and clean
    # frames -> overlay on.
    stamped = str(getattr(config, "STAMP_SNAPSHOTS", "no")).strip().lower() in ("yes", "true", "1", "on")
    return (
        PAGE
        .replace("{{TITLE_H1}}", h1)
        .replace("{{TITLE_TAB}}", html.escape(tab))
        .replace("{{OVERLAY_DEFAULT}}", "" if stamped else "checked")
        .replace("{{TITLE_APP}}", html.escape(line1.title()))
        .replace("{{OG_URL}}", html.escape(base_url + "/"))
        .replace("{{OG_IMAGE}}", html.escape(base_url + "/og-image.png"))
        .replace("{{VERSION}}", html.escape(version.VERSION))
        .replace("{{CHANGELOG}}", _changelog_html())
    )


# ---------------------------------------------------------------------------
# The request handler. One method per HTTP verb; everything is defensive.
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    # Silence the default noisy per-request stderr logging; route it to our logger.
    def log_message(self, fmt, *args):
        log.debug("web: " + fmt, *args)

    def _origin(self) -> str:
        """This deployment's absolute origin (scheme://host) from the request, for
        building correct Open Graph URLs behind any domain / reverse proxy. Returns
        "" if the Host header is missing, so the share URLs degrade to relative."""
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host") or "").strip()
        if not host:
            return ""
        # Respect a reverse proxy's scheme (eaglesnest etc. terminate TLS upstream).
        scheme = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        return f"{scheme}://{host}"

    def _send_file_stream(self, path, content_type: str, download_name: str | None = None) -> None:
        """Stream a (possibly large) file from disk in chunks, so a big rendered
        video never has to be loaded fully into memory."""
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _send(self, code: int, body: bytes, content_type: str,
              cache_control: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # API data and the page itself are "no-store"; the icons/images pass a
        # cacheable value so the browser keeps them (and Safari trusts the icon).
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        # A HEAD request gets the headers only -- never the body.
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self) -> dict | None:
        """Parse a small JSON-object request body, or None if absent/invalid."""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None
        if not (0 < length <= 64 * 1024):
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _handle_save_settings(self) -> None:
        """Read a JSON body, validate + persist it via config, return the result."""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        # Guard against an absurdly large body (this is a tiny settings form).
        if length > 64 * 1024:
            self._send(413, b'{"error": "request too large"}', "application/json")
            return

        raw = self.rfile.read(length) if length > 0 else b""
        try:
            incoming = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(incoming, dict):
                raise ValueError("expected a JSON object")
        except Exception:
            self._send(400, b'{"error": "invalid JSON"}', "application/json")
            return

        try:
            changed = config.update_settings(incoming)
        except ValueError as exc:
            # A validation failure: nothing was saved. Tell the user what was wrong.
            body = json.dumps({"error": str(exc)}).encode("utf-8")
            self._send(400, body, "application/json")
            return

        log.info("Settings updated via dashboard: %s", changed or "(no changes)")
        body = json.dumps({"ok": True, "changed": changed}).encode("utf-8")
        self._send(200, body, "application/json")

    def _handle_build_start(self) -> None:
        """Read JSON build options and kick off a video build (one at a time)."""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if 0 < length <= 64 * 1024 else b""
        try:
            opts = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(opts, dict):
                opts = {}
        except Exception:
            opts = {}

        ok, message = builder.start(opts)
        code = 200 if ok else 409  # 409 Conflict = a build is already running
        self._send(code, json.dumps({"ok": ok, "message": message}).encode("utf-8"),
                   "application/json")

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0]  # drop any ?cache-buster
            if path == "/" or path == "/index.html":
                self._send(200, _render_page(self._origin()).encode("utf-8"),
                           "text/html; charset=utf-8")
            elif path == "/api/status":
                body = json.dumps(_gather_status()).encode("utf-8")
                self._send(200, body, "application/json")
            elif path == "/api/settings":
                # The current value of every editable setting (secrets masked).
                body = json.dumps({"groups": config.public_settings()}).encode("utf-8")
                self._send(200, body, "application/json")
            elif path == "/api/logs":
                self._send(200, _tail_log().encode("utf-8"), "text/plain; charset=utf-8")
            elif path == "/api/build/status":
                self._send(200, json.dumps(builder.status()).encode("utf-8"), "application/json")
            elif path == "/api/build/video":
                vid = builder.latest_output()
                if vid is not None:
                    self._send_file_stream(vid, "video/mp4", download_name=vid.name)
                else:
                    self._send(404, b"no video built yet", "text/plain")
            elif path == "/latest.jpg":
                snap = _latest_snapshot_path()
                if snap is not None:
                    self._send(200, snap.read_bytes(), "image/jpeg")
                else:
                    self._send(404, b"no snapshot yet", "text/plain")
            elif path == "/api/captures":
                body = json.dumps({"days": _list_captures()}).encode("utf-8")
                self._send(200, body, "application/json")
            elif path == "/api/camera-raw":
                # Diagnostics: the camera's raw cloud-cached attributes, so we
                # can see exactly what the Go 2 reports (LTE signal, band, ...).
                try:
                    cam = arlo_client.get_camera()
                    attrs = dict(getattr(cam, "_attrs", {}) or {}) if cam else {}
                except Exception as exc:
                    attrs = {"error": str(exc)}
                body = json.dumps(attrs, default=str, indent=2).encode("utf-8")
                self._send(200, body, "application/json")
            elif path.startswith("/snapshots/"):
                # One saved frame, addressed as /snapshots/YYYY-MM-DD/arlo_....jpg
                # (_capture_path validates the shape, so no traversal is possible).
                img = _capture_path(path[len("/snapshots/"):])
                if img is not None and img.exists():
                    self._send(200, img.read_bytes(), "image/jpeg",
                               cache_control="public, max-age=3600")
                else:
                    self._send(404, b"no such capture", "text/plain")
            elif path == "/favicon.ico":
                ico = _favicon_ico()
                if ico is not None:
                    self._send(200, ico, "image/x-icon", cache_control="public, max-age=86400")
                else:
                    self._send(404, b"no icon", "text/plain")
            elif path == "/icon.png":
                png = _icon_png(180)
                if png is not None:
                    self._send(200, png, "image/png", cache_control="public, max-age=86400")
                else:
                    self._send(404, b"no icon", "text/plain")
            elif path == "/apple-touch-icon.png" or path == "/apple-touch-icon-precomposed.png":
                png = _icon_png(180)
                if png is not None:
                    self._send(200, png, "image/png", cache_control="public, max-age=86400")
                else:
                    self._send(404, b"no icon", "text/plain")
            elif path == "/og-image.png":
                png = _og_png()
                if png is not None:
                    self._send(200, png, "image/png", cache_control="public, max-age=86400")
                else:
                    self._send(404, b"no image", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            # The browser gave up / navigated away before we finished writing.
            # Normal on slow gallery loads -- not a server problem.
            log.debug("web GET %s: client disconnected mid-response.", self.path)
        except Exception as exc:
            log.error("web GET %s failed: %s", self.path, exc)
            try:
                self._send(500, b"server error", "text/plain")
            except Exception:
                pass

    def do_HEAD(self) -> None:
        # Reuse GET routing; _send() sends headers only for HEAD requests. Some
        # favicon fetchers probe with HEAD first, so this must not 501.
        self.do_GET()

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/pause":
                pause.pause()
                self._send(200, b'{"paused": true}', "application/json")
            elif path == "/api/resume":
                pause.resume()
                self._send(200, b'{"paused": false}', "application/json")
            elif path == "/api/test-capture":
                # Force one snapshot now (ignores pause/daylight/backoff). May take
                # up to a minute on LTE; the request handler runs in its own thread.
                result = capture.run_manual_capture()
                self._send(200, json.dumps(result).encode("utf-8"), "application/json")
            elif path == "/api/settings":
                self._handle_save_settings()
            elif path == "/api/logs/clear":
                ok = _clear_log()
                log.info("Logs cleared via dashboard.")
                self._send(200 if ok else 500,
                           json.dumps({"cleared": ok}).encode("utf-8"), "application/json")
            elif path == "/api/build/start":
                self._handle_build_start()
            elif path == "/api/captures/delete":
                # Delete specific frames: {"files": ["YYYY-MM-DD/arlo_....jpg", ...]}
                data = self._read_json()
                files = data.get("files") if data else None
                if not isinstance(files, list) or not files:
                    self._send(400, b'{"error": "expected {\\"files\\": [...]}"}',
                               "application/json")
                    return
                n = _delete_captures(files)
                log.info("Deleted %d capture(s) via dashboard.", n)
                self._send(200, json.dumps({"deleted": n}).encode("utf-8"),
                           "application/json")
            elif path == "/api/captures/clear":
                # Bulk delete: {"all": true} or {"before": "YYYY-MM-DD"}.
                data = self._read_json()
                before = data.get("before") if data else None
                if before is not None and not _DATE_RE.match(str(before)):
                    self._send(400, b'{"error": "before must be YYYY-MM-DD"}',
                               "application/json")
                    return
                if before is None and not (data and data.get("all") is True):
                    self._send(400, b'{"error": "pass \\"all\\": true or a \\"before\\" date"}',
                               "application/json")
                    return
                n = _clear_captures(str(before) if before else None)
                log.info("Cleared %d capture(s) via dashboard (%s).",
                         n, f"before {before}" if before else "ALL")
                self._send(200, json.dumps({"deleted": n}).encode("utf-8"),
                           "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            # The browser stopped waiting (e.g. a long test-capture) -- the work
            # itself still completed; only the response had nowhere to go.
            log.debug("web POST %s: client disconnected before the response.", self.path)
        except Exception as exc:
            log.error("web POST %s failed: %s", self.path, exc)
            try:
                self._send(500, b"server error", "text/plain")
            except Exception:
                pass


def _serve() -> None:
    """Run the HTTP server forever (called inside the background thread)."""
    try:
        # Bind to 0.0.0.0 so it is reachable from other machines on your LAN
        # (Docker publishes the port to your UnRaid host).
        server = ThreadingHTTPServer(("0.0.0.0", config.WEB_PORT), _Handler)
        log.info("Web dashboard listening on port %d.", config.WEB_PORT)
        server.serve_forever()
    except Exception as exc:
        # The dashboard is a convenience; if it cannot start, log loudly but let
        # the capture daemon carry on regardless.
        log.error("Web dashboard could not start (captures continue): %s", exc)


def start_in_background() -> None:
    """Start the dashboard in a daemon thread. Safe to call once at startup."""
    thread = threading.Thread(target=_serve, name="webserver", daemon=True)
    thread.start()
