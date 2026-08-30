import argparse

from timed_sketching_helper.main import create_app
from timed_sketching_helper.main import main as serve

__all__ = ["create_app", "main", "serve"]

_DESCRIPTION = """\
Timed sketching helper — a local web app for timed drawing practice.

It fetches an image list from a DeviantArt URL (a gallery, a favourites
collection, ...) and drives a countdown drawing session in your browser:
pick a duration, and it cycles through a random subset of the images with
pause / previous / skip / reroll controls.
"""

_EPILOG = """\
configuration:
  Runtime settings are read from a .env file in the working directory
  (see .env.example). DeviantArt image fetching needs DEVIANTART_CLIENT_ID
  and DEVIANTART_CLIENT_SECRET from your DeviantArt app page. Sensitive /
  mature images are skipped unless you authorise a logged-in account (with
  mature content enabled) via the app's /auth/deviantart/login route.

data:
  Downloaded images and the SQLite cache live in ./data/ (safe to delete).

Once running, open the printed URL (default http://127.0.0.1:8765) in a browser.
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="timed-sketching-helper",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="port to listen on (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default="info",
        help="logging verbosity (default: %(default)s)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="shortcut for --log-level debug; logs every HTTP request the "
        "server makes to DeviantArt plus verbose uvicorn output",
    )
    args = parser.parse_args()
    log_level = "debug" if args.debug else args.log_level
    serve(host=args.host, port=args.port, log_level=log_level)
