"""
builder.py
==========
Drives compile.py from the web dashboard so you can render the time-lapse video
with a button instead of the command line.

Design (matches the rest of this app's "never break the daemon" rule):
  * Only ONE build runs at a time; a second request is rejected, not queued.
  * The build runs in a background daemon thread via a subprocess, so it never
    blocks the web server or the capture loop.
  * We capture compile.py's output into a small ring buffer and derive a coarse
    "phase" from it, so the page can show live progress.
  * The finished .mp4 is remembered so the page can offer it for download.
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

# compile.py lives at the project root, one level up from this app/ package.
_COMPILE_PY = Path(__file__).resolve().parent.parent / "compile.py"

_lock = threading.Lock()
_log_lines: deque[str] = deque(maxlen=300)
_state = {
    "state": "idle",     # idle | running | done | error
    "phase": "",
    "started": None,     # epoch seconds
    "finished": None,    # epoch seconds
    "output": None,      # absolute path of the finished mp4
    "error": None,
    "frames": None,
}


def status() -> dict:
    """A JSON-friendly snapshot of the current/last build."""
    with _lock:
        s = dict(_state)
        s["log"] = "\n".join(_log_lines)
    if s["output"]:
        out = Path(s["output"])
        s["output_name"] = out.name
        try:
            s["output_size"] = out.stat().st_size if out.exists() else None
        except Exception:
            s["output_size"] = None
    if s["started"]:
        end = s["finished"] or time.time()
        s["elapsed_sec"] = int(end - s["started"])
    return s


def latest_output() -> Path | None:
    """Path of the most recently finished video, if it still exists."""
    with _lock:
        out = _state["output"]
    if out:
        p = Path(out)
        if p.exists():
            return p
    return None


def start(options: dict) -> tuple[bool, str]:
    """Begin a build (returns (ok, message)). Rejects if one is already running."""
    with _lock:
        if _state["state"] == "running":
            return False, "A build is already running."
        _log_lines.clear()
        _state.update(state="running", phase="Starting…", started=time.time(),
                      finished=None, output=None, error=None, frames=None)
    threading.Thread(target=_run, args=(options,), name="video-build", daemon=True).start()
    return True, "Build started."


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _append(line: str) -> None:
    with _lock:
        _log_lines.append(line)


def _run(options: dict) -> None:
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = config.OUTPUT_DIR / f"timelapse_{stamp}.mp4"

        cmd = [sys.executable, str(_COMPILE_PY), "--out", str(out),
               "--fps", str(_int(options.get("fps"), 30, 1, 120))]
        if options.get("overlay"):
            cmd.append("--overlay")
        if options.get("deflicker"):
            cmd += ["--deflicker", str(_int(options.get("deflicker_size"), 10, 1, 120))]
        if options.get("scale"):
            cmd += ["--scale", str(options["scale"]).strip()]
        if options.get("dfrom"):
            cmd += ["--from", str(options["dfrom"]).strip()]
        if options.get("dto"):
            cmd += ["--to", str(options["dto"]).strip()]

        log.info("Starting video build: %s", " ".join(cmd))
        _append("$ " + " ".join(cmd))
        _set(phase="Gathering frames…")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(_COMPILE_PY.parent),
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line:
                _append(line)
                _update_phase(line)
        rc = proc.wait()

        if rc == 0 and out.exists():
            _set(state="done", phase="Done", finished=time.time(), output=str(out))
            _append("Finished: " + out.name)
            log.info("Video build finished: %s", out)
        else:
            msg = f"compile.py exited with code {rc}" + ("" if out.exists() else " (no output produced)")
            _set(state="error", phase="Failed", finished=time.time(), error=msg)
            log.error("Video build failed: %s", msg)
    except Exception as exc:
        _set(state="error", phase="Failed", finished=time.time(), error=str(exc))
        _append("ERROR: " + str(exc))
        log.exception("Video build crashed: %s", exc)


def _update_phase(line: str) -> None:
    """Derive a friendly phase string from a line of compile.py output."""
    low = line.lower()
    if line.startswith("Frames found"):
        try:
            _set(frames=int(line.split(":", 1)[1].strip()))
        except Exception:
            pass
    elif "stamped" in low:                      # "  ...stamped 250/1200 frames"
        _set(phase=line.strip(" ."))
    elif line.startswith("Burning"):
        _set(phase="Adding date/time overlay…")
    elif line.startswith("Running:"):
        _set(phase="Encoding video…")
    elif line.startswith("No matching frames"):
        _set(phase="No frames to build")


def _int(val, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default
