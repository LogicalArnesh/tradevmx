"""TradeVMx desktop :: launcher.

Double-click ``run_tradevmx.bat`` (Windows) or ``./run_tradevmx.sh`` (macOS /
Linux), or run ``python desktop.py``. It starts a native desktop window that
wraps the local control-room console (the Flask app) and shows the live market
desk in a real app window: a title bar, minimise / maximise / close buttons, a
taskbar icon and a logo. Closing the window stops the desk.

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
from pathlib import Path

import webview

from app import APP_VERSION, DEFAULT_PORT, DeskState, create_app, settings_store
from config import configure_logging, get_settings

LOGGER = logging.getLogger("tradevmx.desktop")

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
        "Close the other TradeVMx window or pass --port."
    )


def _create_window(app, host: str, port: int) -> webview.Window:
    """Create the native desktop window wrapping the local Flask UI."""
    url = f"http://{host}:{port}/"
    LOGGER.info("Launching native window at %s", url)

    window = webview.create_window(
        title="TradeVMx — NSE Market Desk",
        url=url,
        width=1280,
        height=820,
        min_size=(920, 640),
        resizable=True,
        frameless=False,
        background_color="#05080e",
        js_api={
            "dock": lambda: (window.dock() if hasattr(window, "dock") else None),
            "minimize": lambda: (window.minimize() if hasattr(window, "minimize") else None),
            "maximize": lambda: (window.maximize() if hasattr(window, "maximize") else None),
            "close": lambda: (window.destroy() if hasattr(window, "destroy") else None),
            "log": lambda *args: print("[TradeVMx]", *args, flush=True),
        },
    )
    return window


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TradeVMx desktop console")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("APP_PORT", str(DEFAULT_PORT))),
        help=f"preferred port (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser",
    )
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

    # Start Flask in a background thread; the native window owns the GUI.
    server_thread = threading.Thread(
        target=app.run,
        kwargs={
            "host": args.host,
            "port": port,
            "threaded": True,
            "use_reloader": False,
            "debug": False,
        },
        daemon=True,
    )
    server_thread.start()
    LOGGER.info("Flask console listening on %s", url)

    # Give Flask a moment to bind so the first request does not race.
    deadline_tick = threading.Event()

    def wait_for_port() -> None:
        import socket as _socket

        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
            while True:
                try:
                    probe.connect((args.host, port))
                    deadline_tick.set()
                    return
                except OSError:
                    if deadline_tick.is_set():
                        return
                    threading.Event().wait(0.05)

    threading.Thread(target=wait_for_port, daemon=True).start()
    deadline_tick.wait(6.0)  # up to ~6s for the port to open

    if not args.no_browser:
        window = _create_window(app, args.host, port)
        webview.start(debug=False)
    else:
        # Headless console mode: the Flask server runs alone.
        try:
            app.run(host=args.host, port=port, threaded=True, use_reloader=False, debug=False)
        finally:
            desk.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
