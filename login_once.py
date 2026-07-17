"""
login_once.py
=============
Run this ONCE, interactively, the first time you set things up (or any time the
saved session is lost). It establishes the "trusted" session with Arlo and saves
it to /data/state so the daemon can then run fully headless.

With IMAP auto-2FA you often don't even need this -- `docker compose up -d` will
establish the session on its own. Keep this script as a fallback for when the
automatic email reading does not work and you want to paste the code yourself.

Run it like:
    docker compose run --rm -it arlo-timelapse python login_once.py
"""

from app import arlo_client, config
from app.logging_setup import setup_logging


def main() -> None:
    setup_logging()
    config.validate_required()
    config.ensure_dirs()

    print("Step 1: trying automatic 2FA by reading the code from Gmail (IMAP)...")
    try:
        arlo = arlo_client.login(interactive=False)
        print("SUCCESS via IMAP. Session saved to", config.STATE_DIR)
        print("Cameras visible:", [c.name for c in arlo.cameras])
        return
    except Exception as exc:
        print("Automatic IMAP login failed:", exc)
        print("Step 2: falling back to MANUAL entry -- watch your email for the code.")

    arlo = arlo_client.login(interactive=True)
    print("SUCCESS (manual). Session saved to", config.STATE_DIR)
    print("Cameras visible:", [c.name for c in arlo.cameras])


if __name__ == "__main__":
    main()
