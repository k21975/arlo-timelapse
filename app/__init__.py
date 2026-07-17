"""Arlo Go 2 time-lapse capture package.

Modules:
    config         - all settings, read from environment variables
    logging_setup  - logging to stdout + a rotating file
    daylight       - sunrise/sunset window math (astral)
    arlo_client    - login, session persistence, snapshots (pyaarlo)
    notify         - email alerts + the daily digest (SMTP)
    capture        - one capture cycle (snapshot -> save -> status -> alerts)
    pause          - the pause flag file (shared by capture + the web dashboard)
    webserver      - tiny stdlib web dashboard (battery/signal/photos + Pause)
    daemon         - the long-running entrypoint (scheduler)
"""
