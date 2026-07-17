#!/usr/bin/env python3
"""
compile.py
==========
Build the final time-lapse MP4 from the captured JPEGs, using ffmpeg.

Run it inside the container (ffmpeg is installed there):
    docker compose run --rm arlo-timelapse python compile.py
With options, for example:
    docker compose run --rm arlo-timelapse python compile.py \
        --fps 30 --scale 1920:-2 --overlay --deflicker 10 \
        --from 2025-07-01 --to 2025-07-31

What it does:
  1. Finds every snapshot under /data/snapshots (optionally within a date range).
  2. Sorts them chronologically (our filenames sort correctly by time).
  3. Prints how many frames there are and how long the video will be.
  4. Optionally burns a date/time stamp onto each frame (parsed from the filename).
  5. Optionally applies a "deflicker" filter to smooth auto-exposure brightness jumps.
  6. Renders an H.264 MP4 that plays everywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Default locations (overridable with --src / --out). DATA_DIR matches the app.
_DATA = os.environ.get("DATA_DIR", "/data")
DEFAULT_SRC = f"{_DATA}/snapshots"
DEFAULT_OUT_DIR = f"{_DATA}/output"

# Our snapshot filenames look like:  arlo_YYYYMMDD_HHMMSS.jpg
FILENAME_RE = re.compile(r"arlo_(\d{8})_(\d{6})\.jpg$")


def timestamp_from_name(path: Path):
    """Pull the datetime out of a snapshot filename, or None if it doesn't match."""
    m = FILENAME_RE.search(path.name)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")


def gather_frames(src: Path, dfrom, dto) -> list[Path]:
    """Find all snapshot JPEGs, filter by date range, and sort chronologically."""
    frames = []
    for p in src.rglob("arlo_*.jpg"):
        ts = timestamp_from_name(p)
        if ts is None:
            continue
        if dfrom and ts.date() < dfrom:
            continue
        if dto and ts.date() > dto:
            continue
        frames.append(p)
    # Sorting by FILENAME works because the timestamp inside it is zero-padded,
    # so alphabetical order == chronological order (even across day folders).
    frames.sort(key=lambda p: p.name)
    return frames


def human_duration(total_seconds: float) -> str:
    """Turn a number of seconds into a friendly 'Xh Ym Zs' / 'Ym Zs' string."""
    total = int(round(total_seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def stamp_frames(frames: list[Path], tmpdir: str) -> list[Path]:
    """
    OVERLAY MODE: draw each frame's real date/time (read from its filename) in the
    bottom-left corner, writing the stamped copies into a temp folder.

    Why do it here with Pillow instead of a single ffmpeg `drawtext`?
        Our frames are NOT evenly spaced in real time (we only shoot in daylight,
        so there are big overnight gaps). A plain ffmpeg drawtext can only show a
        clock derived from the steady frame rate, which would be wrong. Stamping
        each frame from its own filename is a little slower but always accurate.
    """
    from PIL import Image, ImageDraw, ImageFont  # imported here so non-overlay runs don't need Pillow

    # The DejaVu font is installed in the Docker image (fonts-dejavu-core).
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except Exception:
        font = ImageFont.load_default()

    out_paths = []
    total = len(frames)
    for i, src in enumerate(frames, 1):
        ts = timestamp_from_name(src)
        label = ts.strftime("%Y-%m-%d  %H:%M") if ts else src.stem
        img = Image.open(src).convert("RGB")
        draw = ImageDraw.Draw(img)
        x, y = 20, img.height - 60
        # Dark rectangle behind the text so it stays readable over any background.
        draw.rectangle([x - 10, y - 8, x + 380, y + 46], fill=(0, 0, 0))
        draw.text((x, y), label, fill=(255, 255, 255), font=font)
        out = Path(tmpdir) / f"frame_{i:06d}.jpg"
        img.save(out, "JPEG", quality=95)
        out_paths.append(out)
        if i % 250 == 0:
            print(f"  ...stamped {i}/{total} frames")
    return out_paths


def write_concat_file(frames: list[Path], fps: int, list_path: Path) -> None:
    """
    Write the ffmpeg 'concat demuxer' list. Each image is shown for 1/fps seconds.
    Using this list (instead of a numbered pattern) lets us keep the exact order
    and use the real files wherever they live. NOTE: paths must not contain a
    single quote -- ours never do.
    """
    per = 1.0 / fps
    with open(list_path, "w", encoding="utf-8") as f:
        for p in frames:
            f.write(f"file '{Path(p).as_posix()}'\n")
            f.write(f"duration {per:.6f}\n")
        # The concat demuxer ignores the LAST duration unless the last file is
        # listed once more, so we repeat it.
        f.write(f"file '{Path(frames[-1]).as_posix()}'\n")


def render(frames: list[Path], args) -> None:
    """Assemble the frames into an MP4 with the chosen options."""
    # Build the ffmpeg video-filter chain (order matters).
    filters = []
    if args.deflicker is not None:
        # deflicker=size=N averages each frame's brightness over N frames to smooth
        # the Go 2's auto-exposure jumps -- the #1 quality risk in an outdoor lapse.
        filters.append(f"deflicker=size={args.deflicker}")
    if args.scale:
        # e.g. "1920:-2" -> width 1920, height auto (keeps aspect, stays even).
        filters.append(f"scale={args.scale}")
    vf = ",".join(filters) if filters else None

    with tempfile.TemporaryDirectory() as tmp:
        used_frames = frames
        if args.overlay:
            print("Burning date/time overlay onto each frame (this can take a while)...")
            used_frames = stamp_frames(frames, tmp)

        list_path = Path(tmp) / "frames.txt"
        write_concat_file(used_frames, args.fps, list_path)

        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path)]
        if vf:
            cmd += ["-vf", vf]
        cmd += [
            "-r", str(args.fps),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",          # required for broad player compatibility
            "-crf", str(args.crf),          # quality (lower = better/larger)
            "-movflags", "+faststart",      # lets the video start playing while downloading
            str(args.out),
        ]
        print("Running:", " ".join(cmd))
        subprocess.run(cmd, check=True)
        print("Done ->", args.out)


def parse_args():
    p = argparse.ArgumentParser(description="Compile Arlo time-lapse JPEGs into an MP4.")
    p.add_argument("--src", default=DEFAULT_SRC, help="Folder of snapshots (searched recursively).")
    p.add_argument("--out", default=None, help="Output .mp4 path (default: /data/output/timelapse_<ts>.mp4).")
    p.add_argument("--fps", type=int, default=30, help="Frames per second (default 30).")
    p.add_argument("--scale", default=None, help="ffmpeg scale, e.g. 1920:-2 (default: keep original size).")
    p.add_argument("--from", dest="dfrom", default=None, help="Start date YYYY-MM-DD (inclusive).")
    p.add_argument("--to", dest="dto", default=None, help="End date YYYY-MM-DD (inclusive).")
    p.add_argument("--overlay", action="store_true", help="Burn the date/time onto each frame.")
    p.add_argument("--deflicker", nargs="?", type=int, const=10, default=None,
                   help="Smooth exposure flicker. Optional window size (default 10 when flag is given).")
    p.add_argument("--crf", type=int, default=18, help="Quality 0-51, lower=better (default 18).")
    p.add_argument("--dry-run", action="store_true", help="Only print the frame count + duration, don't render.")
    return p.parse_args()


def main():
    args = parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"Source folder not found: {src}", file=sys.stderr)
        sys.exit(1)

    dfrom = dt.date.fromisoformat(args.dfrom) if args.dfrom else None
    dto = dt.date.fromisoformat(args.dto) if args.dto else None

    frames = gather_frames(src, dfrom, dto)
    n = len(frames)
    if n == 0:
        print("No matching frames found.", file=sys.stderr)
        sys.exit(1)

    # Always tell the user the resulting length BEFORE doing any work.
    duration_s = n / args.fps
    print(f"Frames found : {n}")
    print(f"FPS          : {args.fps}")
    print(f"Video length : {human_duration(duration_s)}  ({duration_s:.1f}s)")
    print(f"First frame  : {frames[0].name}")
    print(f"Last frame   : {frames[-1].name}")

    if args.dry_run:
        print("(dry run -- not rendering)")
        return

    # Decide the output path.
    if args.out:
        out = Path(args.out)
    else:
        Path(DEFAULT_OUT_DIR).mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(DEFAULT_OUT_DIR) / f"timelapse_{stamp}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    args.out = out

    render(frames, args)


if __name__ == "__main__":
    main()
