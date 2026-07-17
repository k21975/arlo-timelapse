"""
version.py
==========
The app version number and the human-readable change history shown in the
dashboard's "version" popup. Bump VERSION and add an entry to the TOP of
CHANGELOG whenever you ship a change.

Note: there is no git history for this project yet, so the entries before the
current release were reconstructed from the codebase + README and their version
numbers are approximate. Adjust freely.
"""

from __future__ import annotations

VERSION = "2.23.1"

# Newest first. Each entry: version, date (YYYY-MM-DD), and a list of changes.
CHANGELOG = [
    {
        "version": "2.23.1",
        "date": "2026-07-06",
        "changes": [
            "Added an ✕ close button to the top-right of the Captures window.",
        ],
    },
    {
        "version": "2.23.0",
        "date": "2026-07-05",
        "changes": [
            "Preemptive session refresh: the Arlo token reliably dies ~2 hours after login and pyaarlo cannot refresh it (its \"reconnect\" never re-authenticates). The daemon now does the full 4-second, 2FA-safe re-login every \"Reconnect every\" minutes (default 90) — before the token expires — instead of losing an hour of frames to the watchdog cycle.",
            "Catch-up retries can now trigger the dead-session watchdog too, so a mid-hour session death is healed within ~35 minutes instead of waiting for the next hourly capture.",
            "Session swaps now wait for any in-flight capture to finish (shared lock), so a re-login can't interrupt a snapshot in progress.",
        ],
    },
    {
        "version": "2.22.0",
        "date": "2026-07-04",
        "changes": [
            "Dead-session watchdog: pyaarlo's periodic \"reconnect\" only restarts the event stream and never re-authenticates its REST token — when the token died, every capture returned the cached frame forever (hourly \"No devices returned\" warnings) and only a container restart fixed it. This was the real cause of multi-hour/multi-day outages (including June 19–20). After 3 straight failed captures, the daemon now forces a fresh login itself — 2FA-safe, using the saved trusted session, at most once every 45 minutes.",
        ],
    },
    {
        "version": "2.21.0",
        "date": "2026-07-03",
        "changes": [
            "Stopped pyaarlo pinging the camera every 60 seconds (new \"60s camera keepalive pings\" setting, default off). pyaarlo never pings own-base battery cameras because it drains them — but the Go 2's solar charger made it look mains-powered, defeating that protection. Power-save mode can now actually work. The Arlo login/2FA session refresh is separate and unaffected.",
        ],
    },
    {
        "version": "2.20.0",
        "date": "2026-07-03",
        "changes": [
            "The Signal tile now shows the LTE modem (with carrier and band, e.g. \"4/5 · Verizon LTE B4\") instead of the Go 2's disabled WiFi radio, which is why it was stuck at 1 forever. Matches the Arlo app.",
            "Device records are re-read from Arlo's cloud hourly so battery/signal can't go stale between events (a cloud call — never wakes the camera).",
            "New /api/camera-raw diagnostics endpoint: the camera's full raw cloud record.",
        ],
    },
    {
        "version": "2.19.0",
        "date": "2026-07-03",
        "changes": [
            "Fixed: the snapshot-timeout setting never fully applied — pyaarlo has an internal 60-second kill-switch that aborted every snapshot request, even with the timeout set to 90s. The Go 2 needs 60–90s to wake, shoot and upload over LTE, so it kept losing the race by seconds and we got the cached frame instead. The internal limit now matches the setting.",
            "Failure notes now say what actually happened: \"offline probe: cached frame only\" vs \"no fresh frame within timeout\" instead of a catch-all \"stale/duplicate frame\".",
            "The Signal tile is labelled \"as last reported\" — it is Arlo's cloud-cached value and can lag far behind the live bars shown in the Arlo app.",
        ],
    },
    {
        "version": "2.18.1",
        "date": "2026-07-03",
        "changes": [
            "Fixed: a fixed capture-window end time (e.g. 16:00) skipped its own final capture — the scheduler fires microseconds after the hour, which fell just outside the window. The end check now works at minute granularity.",
            "The \"Test capture\" button answers in ~1 minute (instead of 5) when the camera is offline: it now makes a single attempt, matching the scheduled captures.",
            "A browser that stops waiting mid-request no longer logs a scary Broken-pipe ERROR.",
        ],
    },
    {
        "version": "2.18.0",
        "date": "2026-07-03",
        "changes": [
            "Catch-up retries: when a scheduled capture fails (e.g. the camera dropped offline), the daemon retries 15 and 30 minutes later — but only wakes the camera if Arlo's cloud says it is reachable again. A camera that recovers mid-hour no longer leaves a gap in the time-lapse.",
            "An offline camera is now probed with a single snapshot attempt instead of three: the retries only ever returned the same cached frame while waking the LTE radio for nothing.",
        ],
    },
    {
        "version": "2.17.1",
        "date": "2026-07-03",
        "changes": [
            "Moved the \"View captures\" button up under the latest-snapshot photo.",
        ],
    },
    {
        "version": "2.17.0",
        "date": "2026-07-03",
        "changes": [
            "New Captures gallery: browse every saved photo by day, tap to view full-size.",
            "Delete captures from the gallery: single photos (✕), multi-select, clear everything before a chosen date, or clear ALL — each with a confirmation warning.",
            "Closing Settings with unsaved changes now asks Save / Discard / Cancel instead of silently losing your edits.",
        ],
    },
    {
        "version": "2.16.0",
        "date": "2026-07-03",
        "changes": [
            "Fixed: setting the photo interval to 60 minutes (or other whole hours) was silently ignored — the scheduler rejected the value and kept the old interval running. Whole-hour intervals now fire on the hour, and a bad interval can no longer crash the daemon at startup.",
            "The stale-frame check now survives restarts: the last saved frame's fingerprint is stored on disk, so the first snapshot after a deploy/restart can no longer sneak a duplicate cached frame into the time-lapse.",
        ],
    },
    {
        "version": "2.15.0",
        "date": "2026-06-22",
        "changes": [
            "Battery-protective backoff when the camera is offline: instead of waking its LTE radio for a full timeout every cycle (which just returns a stale frame), it now only probes for recovery every Nth cycle and skips the rest. New \"Offline probe cadence\" setting (default 4 = probe hourly at a 15-min interval).",
            "New \"Test capture now\" button on the main page — force a fresh snapshot on demand and see the result, ignoring pause/daylight.",
            "The daily digest email can now be turned off from Settings (\"Send daily digest email\").",
        ],
    },
    {
        "version": "2.14.2",
        "date": "2026-06-18",
        "changes": [
            "Skip stale snapshots: if a struggling camera returns the same cached frame again, it's no longer saved as a new photo (so the time-lapse can't fill up with identical frames), and it counts toward the failure alert instead.",
        ],
    },
    {
        "version": "2.14.1",
        "date": "2026-06-18",
        "changes": [
            "Snapshots are no longer timestamped by default — originals stay clean and the date/time is added when you build the video. The builder's overlay toggle defaults to the opposite of the capture-stamp setting, so you never get a double timestamp.",
        ],
    },
    {
        "version": "2.14.0",
        "date": "2026-06-17",
        "changes": [
            "Build the time-lapse video right from the dashboard — pick FPS, date/time overlay and flicker smoothing, watch progress, and download the finished MP4. No command line needed.",
        ],
    },
    {
        "version": "2.13.1",
        "date": "2026-06-17",
        "changes": [
            "Quieted the repetitive pyaarlo “failed to read modes” log noise the Go 2 emits every ~10 minutes.",
        ],
    },
    {
        "version": "2.13.0",
        "date": "2026-06-17",
        "changes": [
            "Site title is now editable as two lines from Settings — it updates the page heading, the browser tab, the link preview and the share card.",
            "Added a Logs panel to the dashboard: view, copy, and clear the app log.",
            "Saving settings now reloads the page so the header updates immediately.",
            "Project-agnostic defaults and link-preview URLs derived from the request host, so the dashboard works cleanly on any domain.",
        ],
    },
    {
        "version": "2.12.0",
        "date": "2026-06-16",
        "changes": [
            "Link-preview share card (Open Graph) for nice previews in Messages/Slack.",
            "Real, Safari-compatible favicon.ico.",
        ],
    },
    {
        "version": "2.11.0",
        "date": "2026-06-14",
        "changes": [
            "Generated neon-bike app icon (browser favicon + iPhone home-screen icon).",
        ],
    },
    {
        "version": "2.10.0",
        "date": "2026-06-13",
        "changes": [
            "In-dashboard Settings editor for every setting (passwords masked; most changes apply instantly).",
        ],
    },
    {
        "version": "2.9.0",
        "date": "2026-06-13",
        "changes": [
            "Synthwave dashboard theme: perspective grid, sun glow, chrome title; mobile-friendly.",
        ],
    },
    {
        "version": "2.5.0",
        "date": "2026-06-07",
        "changes": [
            "Optional timestamp burned into each snapshot, matching the rendered video overlay.",
        ],
    },
    {
        "version": "2.3.0",
        "date": "2026-06-07",
        "changes": [
            "Daily digest email at sunset; low-battery and consecutive-failure alerts with cooldowns.",
        ],
    },
    {
        "version": "2.0.0",
        "date": "2026-06-07",
        "changes": [
            "Local web dashboard: live battery/signal, photo counts, latest snapshot, and a Pause/Resume button.",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-06-06",
        "changes": [
            "Initial release: log-in-once Arlo session, daylight-window snapshot loop, status.csv logging, and the ffmpeg video compiler.",
        ],
    },
]
