"""TradeXVm desktop :: launcher.

Double-click ``run_tradexvm.bat`` (Windows) or ``./run_tradexvm.sh`` (macOS /
Linux), or run ``python desktop.py``. It starts the local console on a free
loopback port and opens your browser. Closing the window stops the desk.

Nothing is exposed to the network: the default host is ``127.0.0.1`` and the
sentinel only runs while this process is alive.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from app import APP_VERSION, DEFAULT_PORT, DeskState, create_app, settings_store
from config import configure_logging, get_settings

LOGGER = logging.getLogger("tradexvm.desktop")

_BANNER = """
+--------------------------------------------------------------+
|  T R A D E X V M   ·   N S E   M A R K E T   D E S K   {version}  |
+--------------------------------------------------------------+
|  Console : {url}
|  Config  : {env}
|  Stop    : close this window or press Ctrl+C
+--------------------------------------------------------------
"""


def _free_port(host: str, preferred: int, attempts: int = 12) -> int:
    for candidate in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise SystemExit(
        f"No free port between {preferred} and {preferred + attempts - 1}. "
        "Close the other TradeXVm window or pass --port."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeXVm desktop console")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("APP_PORT", str(DEFAULT_PORT))),
        help=f"preferred port (default {DEFAULT_PORT})",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        LOGGER.warning(
            "Binding to %s exposes the console beyond this machine. The panel is "
            "token-protected but the .env writer is not designed for hostile networks.",
            args.host,
        )

    settings = get_settings()
    configure_logging(settings)

    port = _free_port(args.host, args.port)
    app = create_app()
    desk: DeskState = app.config["DESK"]
    url = f"http://{args.host}:{port}/"

    print(_BANNER.format(version=APP_VERSION, url=url, env=settings_store.ENV_PATH))
    if not settings.telegram_ready and not settings.email_ready:
        print("  Note: no alert channel configured yet - the setup guide opens in the app.\n")

    # Always-on by default: sweep and alert without pressing anything.
    if desk.settings.auto_arm:
        try:
            desk.sentinel.start()
            print("  Sentinel armed automatically (AUTO_ARM=true).\n")
        except Exception as exc:  # noqa: BLE001 - never block startup
            LOGGER.exception("Auto-arm failed: %s", exc)
            print(f"  Could not arm the sentinel automatically: {exc}\n")

    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()

    try:
        app.run(host=args.host, port=port, threaded=True, use_reloader=False, debug=False)
    except KeyboardInterrupt:
        print("\nStopping TradeXVm...")
    finally:
        desk.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
