"""TradeVMx desktop :: local control room.

A single-user Flask app bound to ``127.0.0.1`` that the desktop launcher opens
in your browser. It is the "Jarvis console": configure keys, arm the sentinel,
watch the tape, read the feed, download research PDFs.

Security posture (localhost, single operator)
--------------------------------------------
* binds loopback by default (the launcher refuses ``0.0.0.0`` unless asked),
* every mutating request needs ``X-TradeVMx-Token`` (random per launch, injected
  into the page) plus a same-origin check, so another website cannot POST to
  ``localhost`` and rewrite your keys,
* secrets are never returned - the settings API only exposes ``is_set`` masks,
* CSP, ``X-Frame-Options: DENY`` and ``no-store`` on API responses.
"""

from __future__ import annotations

import logging
import math
import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file
from jinja2 import FileSystemLoader

import json
import logging
import math
import sys
import secrets
import threading
import settings_store
import studio
import updater
from updater import check_cached
from version import __version__ as APP_VERSION
from config import IST, configure_logging, UnknownInstrument, get_settings
from market_scanner import MarketScanner, ScanReport, SUPPORTED_INTERVALS
from notify import LocalNotifier
from report_engine import ReportEngine
from alerts import AlertDispatcher
from ai_engine import GroqBrain
from sentinel import Sentinel, SentinelEvent
from chart_engine import ChartEngine


# ---------------------------------------------------------------------------
# Bundle root
#
# When the app is run from a normal source checkout, ``__file__`` points at the
# project folder.  When PyInstaller packages it as a one-file executable the
# import path is ``sys._MEIPASS`` and the real files live under the
# extraction directory (``sys._MEIPASS``), so ``__file__`` is no longer a usable
# anchor.  Resolve the root once, at import time, and hand it to the app
# factory so the template/static folders, the .env and the logs/Reports
# directories all resolve correctly in both modes.
# ---------------------------------------------------------------------------

def _bundle_root() -> Path:
    """Return the directory that holds the app's templates/ and web/ trees."""
    if getattr(sys, "frozen", False):
        # PyInstaller one-file: content is extracted to this path
        return Path(sys._MEIPASS)
    # Normal source checkout
    return Path(__file__).resolve().parent


APP_ROOT = _bundle_root() / "web"
TEMPLATE_ROOT = APP_ROOT / "templates"
STATIC_ROOT = APP_ROOT / "static"

# Env/settings live next to the project root (or in the extraction directory).
ENV_PATH = APP_ROOT.parent / ".env"
LOGS_DIR = APP_ROOT.parent / "logs"
REPORTS_DIR = APP_ROOT.parent / "reports"

def _create_app_root(app: Flask) -> dict:
    """Return the resolved template/static roots so the frozen app resolves
    its templates, CSS and JS relative to the app's real directory."""
    return {
        "template_folder": str(TEMPLATE_ROOT),
        "static_folder": str(STATIC_ROOT),
        "root_path": str(APP_ROOT.parent),
    }


LOGGER = logging.getLogger(__name__)
LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8765


# ------------------------------------------------------------- update
# Fallback for the in-app update check when GitHub is unavailable or the
# release branch is not yet published. The desktop launcher keeps its own
# copy of the state so the update banner shows the correct status immediately.

UPDATE_STATE_FILE = Path.home() / ".tradevmx" / "update.state.json"  # project dir on failure


def _read_update_state() -> dict:
    try:
        return json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_update_state(payload: dict) -> None:
    # Fall back to the project reports/ dir when the home directory is not writable
    # (cloud sandbox), so the in-app update check never fails on I/O.
    target = UPDATE_STATE_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        fallback = Path(__file__).resolve().parent / "reports" / "update.state.json"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        tmp = fallback.with_name(fallback.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, fallback)

import os


def _local_check(repo: str, current: str, force: bool = False) -> dict:
    """The desktop launcher's own update check.

    No HTTP dependency: it answers from the cached release state or from the
    repo default, so the banner is never blocked by a rate limit or a network
    path that has not been published yet.
    """
    if not repo:
        return {
            "configured": False,
            "available": False,
            "current": current,
            "latest": None,
            "unmanaged": True,
            "fetched_at": None,
            "error": None,
        }
    if force:
        return updater.check_cached(repo, current, force=True)
    cached = _read_update_state()
    if cached.get("current") == current:
        return cached
    return updater.check_cached(repo, current, force=False)


def _schedule_update_check(app) -> None:
    """Poll the configured repo for a new version once a day.

    Runs in the event loop background thread; the result is written to
    UPDATE_STATE_FILE and exposed to the browser as /api/update.
    """
    import threading

    def _loop():
        while True:
            try:
                settings = app.config["DESK"].settings
                payload = _local_check(
                    settings.update_repo, app.config["TRADEVMX_TOKEN"]
                )
                _write_update_state(payload)
            except Exception:
                LOGGER.exception("update check failed")
            threading.Event().wait(86400)

    threading.Thread(target=_loop, daemon=True).start()


def _build_initial_state() -> dict:
    """Return the update state the page renders before /api/update settles."""
    from . import app as _app_mod

    # Mirror the app's update check so the desktop launcher does not need a web
    # round-trip to know whether an update is available.
    return {}



# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------
class DeskState:
    """Owns the live components and the event feed for the browser."""

    def __init__(self, env_path: Path | None = None) -> None:
        self.env_path = env_path or settings_store.ENV_PATH
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.started_at = datetime.now(timezone.utc).astimezone(IST)
        self._events_lock = threading.Lock()
        self.settings: Settings
        self.scanner: MarketScanner
        self.brain: GroqBrain
        self.reports: ReportEngine
        self.alerts: AlertDispatcher
        self.notifier: LocalNotifier
        self.sentinel: Sentinel
        self._build()

    def _build(self) -> None:
        if self.env_path.exists():
            load_dotenv(self.env_path, override=True)
        get_settings.cache_clear()
        self.settings = get_settings()
        self.scanner = MarketScanner(self.settings)
        self.chart_engine = ChartEngine(self.settings)
        self.chart_engine.attach_scanner(self.scanner)
        self.brain = GroqBrain(self.settings)
        self.reports = ReportEngine(self.settings)
        self.alerts = AlertDispatcher(self.settings)
        self.notifier = LocalNotifier(
            toast=self.settings.desktop_toast, speak=self.settings.voice_alerts
        )
        self.sentinel = Sentinel(self.settings, dispatcher=self.alerts, notifier=self.notifier)
        self.sentinel.subscribe(self._on_event)

    # ------------------------------------------------------------- events
    def _on_event(self, event: SentinelEvent) -> None:
        with self._events_lock:
            self.events.appendleft(event.as_dict())

    def recent_events(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._events_lock:
            return list(self.events)[:limit]

    # ------------------------------------------------------------- reload
    def reload(self) -> None:
        """Re-read ``.env`` and rebuild every component (keys may have changed)."""
        was_running = self.sentinel.running
        self.sentinel.stop()
        self.sentinel.close()
        self._build()
        if was_running:
            self.sentinel.start()
        self._on_event(
            SentinelEvent(
                datetime.now(IST),
                "info",
                "Configuration applied",
                "Components reloaded from .env",
                kind="system",
            )
        )

    def shutdown(self) -> None:
        try:
            self.sentinel.close()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            LOGGER.exception("Sentinel shutdown failed")


# ---------------------------------------------------------------------------
# JSON builders
# ---------------------------------------------------------------------------
def _finite(value: float | None) -> float | None:
    if value is None or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def build_state(desk: DeskState) -> dict[str, Any]:
    now = datetime.now(IST)
    report: ScanReport | None = desk.sentinel.latest_report()
    plans = desk.sentinel.latest_plans()
    interval = desk.settings.scan_interval_minutes

    rows: list[dict[str, Any]] = []
    if report is not None:
        for snapshot in report.snapshots:
            plan = plans.get(snapshot.symbol)
            rows.append(
                {
                    "symbol": snapshot.symbol,
                    "name": snapshot.display_name,
                    "cmp": snapshot.cmp,
                    "change_pct": snapshot.change_pct,
                    "bias": snapshot.bias,
                    "structure": snapshot.structure,
                    "vwap": snapshot.vwap,
                    "vwap_distance_pct": round(snapshot.vwap_distance_pct, 2),
                    "atr": snapshot.atr,
                    "volume_ratio": _finite(snapshot.volume_ratio),
                    "stale": snapshot.is_stale,
                    "ltp_source": snapshot.ltp_source,
                    "signals": [
                        {
                            "label": signal.label,
                            "direction": signal.direction,
                            "severity": signal.severity,
                            "timeframe": signal.timeframe,
                            "detail": signal.detail,
                        }
                        for signal in sorted(snapshot.signals, key=lambda s: -s.severity)[:4]
                    ],
                    "verdict": plan.verdict if plan else None,
                    "risk_reward": plan.risk_reward if plan else None,
                }
            )

    actionable = len(report.actionable(desk.settings.alert_min_severity)) if report else 0
    last_scan = desk.sentinel.last_scan_at

    return {
        "app": {
            "version": APP_VERSION,
            "uptime_seconds": int((now - desk.started_at).total_seconds()),
            "env_path": str(desk.env_path),
        },
        "clock": {
            "ist": now.isoformat(),
            "date_label": now.strftime("%a %d %b %Y"),
            "time_label": now.strftime("%H:%M:%S"),
            "phase": desk.settings.market_phase(now),
            "open": desk.settings.is_market_open(now),
            "trading_day": desk.settings.is_trading_day(now.date()),
        },
        "sentinel": {
            "running": desk.sentinel.running,
            "interval_minutes": interval,
            "timeframe": desk.settings.scan_timeframe,
            "schedule": desk.sentinel.schedule_table(),
            "last_scan": last_scan.isoformat() if last_scan else None,
            "last_scan_label": last_scan.strftime("%H:%M:%S") if last_scan else None,
            "last_alert_count": desk.sentinel.last_alert_count,
            "alert_floor": desk.settings.alert_min_severity,
        },
        "setup": {
            "needs_setup": not desk.settings.setup_done,
            "done": desk.settings.setup_done,
        },
        "readiness": {
            "telegram": desk.settings.telegram_ready,
            "email": desk.settings.email_ready,
            "groq": bool(desk.settings.groq_api_key),
            "toast": desk.settings.desktop_toast,
            "voice": desk.settings.voice_alerts,
        },
        "market": {
            "as_of": report.created_at.isoformat() if report else None,
            "scanned": report.scanned if report else 0,
            "actionable": actionable,
            "errors": report.errors if report else {},
            "rows": rows,
        },
        "events": desk.recent_events(),
        "settings_summary": desk.settings.public_summary(),
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def _resolve_web_roots() -> tuple[str, str]:
    """Return (template_folder, static_folder) for the web UI.

    From a source checkout the web/ folder sits next to this file.  When the
    app runs from a PyInstaller onefile/onedir bundle the web/ tree is bundled
    next to the executable, so we first look for it next to the executable and
    then fall back to the source-tree layout.
    """
    import logging
    LOGGER = logging.getLogger(__name__)
    exe_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    # In onedir, the web/ tree lives in the same folder as the executable,
    # alongside _internal/ with templates/ and static/ as sub-folders.
    exe_parent = exe_dir.parent if exe_dir != Path(__file__).resolve().parent else exe_dir
    # The project root is the parent of the source web/ folder.
    project_root = APP_ROOT.parent if APP_ROOT.parent.exists() and (APP_ROOT.parent / "web" / "templates" / "index.html").exists() else APP_ROOT.parent
    candidates = [exe_parent.parent, exe_parent, Path.cwd(), project_root]
    for candidate in candidates:
        # In onedir the web/ tree may sit at <candidate>/web/ (source layout).
        web_tmpl = candidate / "web" / "templates" / "index.html"
        web_static = candidate / "web" / "static"
        if web_tmpl.exists() and web_static.exists():
            LOGGER.info("Resolved web template root: %s", web_tmpl.parent)
            # web/ contains templates/ and static/ as sub-folders.
            return str(web_tmpl.parent), str(web_static)
        # Inside PyInstaller extraction at <candidate>/_internal with
        # templates/ and static/ as sub-folders.
        _tmpl = candidate / "_internal" / "templates" / "index.html"
        _static = candidate / "_internal" / "static"
        if _tmpl.exists() and _static.exists():
            LOGGER.info("Resolved web template root: %s", _tmpl.parent)
            # _internal contains templates/ and static/ as sub-folders.
            return str(_tmpl.parent), str(_static)
    LOGGER.warning("Resolved web template root not found, falling back to source")
    # Fall back to <project>/web (contains templates/ + static/ subfolders)
    return str(TEMPLATE_ROOT), str(STATIC_ROOT)


def create_app(
    env_path: Path | None = None,
    *,
    state: DeskState | None = None,
    token: str | None = None,
) -> Flask:
    # Resolve the template/static roots (works for source checkout AND frozen
    # onefile/onedir bundles).  Flask resolves static_folder/template_folder
    # relative to the CWD for a frozen app, so we pass explicit absolute paths
    # and a dedicated FileSystemLoader so templates resolve correctly
    # regardless of the process working directory.
    template_root, static_root = _resolve_web_roots()
    # static_url_path='/static' so the browser always fetches /static/app.css
    # and /static/app.js.  The static folder is the bundled web/static tree
    # (source checkout or PyInstaller _internal/static), never the CWD.
    app = Flask(
        __name__,
        static_folder=static_root,
        static_url_path="/static",
        root_path=str(APP_ROOT.parent),
    )
    # Register the template loader explicitly so the dashboard does not 500
    # when the app is started from a packaged (frozen) bundle.
    app.jinja_loader = FileSystemLoader(template_root)
    app.config["JSON_SORT_KEYS"] = False
    app.config["TRADEVMX_TOKEN"] = token or secrets.token_urlsafe(24)
    desk = state or DeskState(env_path)
    app.config["DESK"] = desk
    configure_logging(desk.settings)
    _schedule_update_check(app)
    ChartEngine.register(desk.chart_engine)
    _schedule_update_check(app)

    # -------------------------------------------------------------- guards
    def _same_origin() -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True  # curl, tests, same-origin navigations
        parsed = urlparse(origin)
        return parsed.netloc == request.host

    def require_token(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _same_origin():
                return jsonify({"error": "cross-origin request rejected"}), 403
            supplied = request.headers.get("X-TradeVMx-Token", "")
            if not secrets.compare_digest(supplied, app.config["TRADEVMX_TOKEN"]):
                return jsonify({"error": "not authorised"}), 401
            return view(*args, **kwargs)

        return wrapper

    # -------------------------------------------------------------- pages
    @app.get("/")
    def index() -> str:
        try:
            return render_template(
                "index.html", token=app.config["TRADEVMX_TOKEN"], version=APP_VERSION
            )
        except Exception as _e:
            # Debug: re-raise with the loader path so we can see what went wrong
            import traceback
            traceback.print_exc()
            return f"Template error: {repr(_e)}"

    @app.after_request
    def _headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # --------------------------------------------------------------- api
    @app.get("/api/state")
    def api_state() -> Any:
        return jsonify(build_state(desk))

    @app.post("/api/scan")
    @require_token
    def api_scan() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            report = desk.sentinel.scan(force=bool(payload.get("force", True)))
        except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
            LOGGER.exception("Manual scan failed: %s", exc)
            return jsonify({"error": f"scan failed: {exc}"}), 502
        return jsonify(
            {
                "ok": True,
                "scanned": report.scanned,
                "actionable": len(report.actionable(desk.settings.alert_min_severity)),
                "errors": report.errors,
                "state": build_state(desk),
            }
        )

    @app.post("/api/sentinel")
    @require_token
    def api_sentinel() -> Any:
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", "")).lower()
        if action == "start":
            desk.sentinel.start()
        elif action == "stop":
            desk.sentinel.stop()
        else:
            return jsonify({"error": "action must be start or stop"}), 400
        return jsonify({"ok": True, "running": desk.sentinel.running, "state": build_state(desk)})

    @app.get("/api/report")
    def api_report() -> Any:
        symbol = request.args.get("symbol", "NIFTY 50").strip() or "NIFTY 50"
        try:
            artifact, _plan, _snapshot = desk.sentinel.build_report(symbol)
        except UnknownInstrument as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Report build failed for %s: %s", symbol, exc)
            return jsonify({"error": f"report failed: {exc}"}), 502
        artifact.buffer.seek(0)
        return send_file(
            artifact.buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=artifact.filename,
        )

    @app.get("/api/settings")
    def api_settings() -> Any:
        return jsonify(settings_store.snapshot(desk.env_path))

    @app.post("/api/settings")
    @require_token
    def api_save_settings() -> Any:
        payload = request.get_json(silent=True) or {}
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            return jsonify({"error": "values must be an object"}), 400
        try:
            settings_store.save(values, desk.env_path)
        except settings_store.SettingsValidationError as exc:
            return jsonify({"error": "validation failed", "fields": exc.errors}), 400
        except OSError as exc:
            LOGGER.exception("Cannot write .env: %s", exc)
            return jsonify({"error": f"could not write .env: {exc}"}), 500

        desk.reload()
        return jsonify(
            {
                "ok": True,
                "settings": settings_store.snapshot(desk.env_path),
                "state": build_state(desk),
            }
        )

    @app.post("/api/test")
    @require_token
    def api_test() -> Any:
        payload = request.get_json(silent=True) or {}
        channel = str(payload.get("channel", "")).lower()

        if channel == "telegram":
            ok = asyncio.run(
                desk.alerts.telegram.broadcast(
                    "\u2705 <b>TradeVMx</b> test message \u2014 Telegram is wired up."
                )
            )
            return jsonify(
                {
                    "ok": ok > 0,
                    "detail": "Message delivered to Telegram."
                    if ok
                    else "Nothing delivered. Check the token and chat ids, and press /start in the bot.",
                }
            )

        if channel == "email":
            ok = asyncio.run(
                desk.alerts.email.send_async(
                    "TradeVMx | Test message",
                    "<p>TradeVMx test message.</p>"
                    "<p>If you can read this, Gmail SMTP and the app password are working.</p>",
                    text_body="TradeVMx test message. Gmail SMTP is working.",
                )
            )
            return jsonify(
                {
                    "ok": ok,
                    "detail": "Test email sent."
                    if ok
                    else "Send failed. Use a 16-character Gmail App Password and fill the recipients.",
                }
            )

        if channel == "groq":
            report = desk.sentinel.latest_report()
            if report is None or not report.snapshots:
                return jsonify({"ok": False, "detail": "Run a scan first, then test Groq."})
            plan = asyncio.run(desk.brain.plan(report.snapshots[0]))
            return jsonify(
                {
                    "ok": not plan.is_fallback,
                    "detail": (
                        f"{plan.model} \u00b7 {plan.verdict} \u00b7 RR 1:{plan.risk_reward:.2f}"
                        if not plan.is_fallback
                        else "Groq did not answer - the rule engine produced this plan instead."
                    ),
                }
            )

        return jsonify({"error": "unknown channel"}), 400

    @app.get("/api/update")
    def api_update() -> Any:
        """Cached GitHub release check; safe to poll, never raises."""
        force = request.args.get("force", "").lower() in {"1", "true", "yes"}
        payload = _local_check(
            desk.settings.update_repo, app.config["TRADEVMX_TOKEN"], force=force
        )
        return jsonify(payload)

    @app.get("/api/symbols")
    def api_symbols():
        """List the watched instruments plus indices, with labels for the search dropdown."""
        from config import get_settings

        entries: list[dict[str, object]] = []
        for inst in desk.settings.watchlist:
            entries.append(
                {
                    "symbol": inst.symbol,
                    "name": inst.name,
                    "kind": inst.kind,
                    "description": f"{inst.name} ({inst.symbol})",
                }
            )
        # Indices always available for charting
        for inst in get_settings().instruments.values():
            if inst.kind == "index":
                entries.append(
                    {
                        "symbol": inst.symbol,
                        "name": inst.name,
                        "kind": inst.kind,
                        "description": f"{inst.name} ({inst.symbol})",
                    }
                )
        return jsonify({
            "symbols": entries,
            "current": desk.settings.watchlist[0].symbol if desk.settings.watchlist else None,
        })

    @app.get("/api/chart")
    def api_chart():
        """Current live chart for a symbol (bars + levels + snapshot)."""
        symbol = request.args.get("symbol", "").strip().upper()
        timeframe = request.args.get("timeframe", "15m")
        if not symbol:
            return jsonify({"error": "symbol is required"}), 400
        if timeframe not in SUPPORTED_INTERVALS:
            return jsonify({"error": f"unsupported timeframe; use {', '.join(sorted(SUPPORTED_INTERVALS))}"}), 400
        try:
            state = desk.chart_engine.state(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 502
        return jsonify({
            "symbol": state.symbol,
            "name": state.name,
            "timeframe": state.timeframe,
            "current": state.snapshot.cmp if state.snapshot else None,
            "change_pct": state.snapshot.change_pct if state.snapshot else None,
            "is_stale": state.is_stale,
            "bars": [b.as_dict() for b in state.bars],
            "levels": state.levels,
            "scanned_at": state.scanned_at,
            "error": state.error,
        })

    # ------------------------------------------------------------- studio
    def _desk_context() -> dict[str, Any]:
        state = build_state(desk)
        market = state.get("market") or {}
        clock_info = state.get("clock") or {}
        return {
            "time": clock_info.get("time_label"),
            "phase": clock_info.get("phase"),
            "scanned": market.get("scanned") or 0,
            "actionable": market.get("actionable") or 0,
            "rows": market.get("rows") or [],
            "news": [],
        }

    def _env_value(key: str, default: str = "") -> str:
        try:
            raw = settings_store.read_env(desk.env_path).get(key) or ""
        except Exception:  # noqa: BLE001 - settings must never break a route
            return default
        return raw.strip() or default

    paper_book = studio.PaperPortfolio(desk.env_path.parent / "paper_book.json")

    @app.get("/api/search")
    def api_search() -> Any:
        query = request.args.get("q", "")
        try:
            limit = int(request.args.get("limit", 12))
        except ValueError:
            limit = 12
        return jsonify(
            {
                "query": query,
                "matches": studio.search_instruments(
                    query, limit=limit, watchlist=desk.settings.watchlist
                ),
            }
        )

    @app.get("/api/history")
    def api_history() -> Any:
        symbol = request.args.get("symbol", "").strip().upper()
        timeframe = request.args.get("timeframe", "15m")
        if not symbol:
            return jsonify({"error": "symbol is required"}), 400
        if timeframe not in SUPPORTED_INTERVALS:
            timeframe = "15m"
        try:
            limit = int(request.args.get("limit", 220))
        except ValueError:
            limit = 220
        payload = studio.history_bars(symbol, timeframe, limit)
        try:
            payload["name"] = desk.settings.resolve_instrument(symbol).name
        except Exception:  # noqa: BLE001 - unknown tickers still chart
            payload["name"] = symbol
        return jsonify(payload)

    @app.post("/api/assistant")
    @require_token
    def api_assistant() -> Any:
        payload = request.get_json(silent=True) or {}
        question = str(payload.get("question", "")).strip()
        result = studio.assistant_answer(
            question,
            _desk_context(),
            api_key=desk.settings.groq_api_key,
            model=desk.settings.groq_model,
            timeout=desk.settings.groq_timeout,
        )
        result["question"] = question
        return jsonify(result)

    @app.get("/api/portfolio")
    def api_portfolio() -> Any:
        book = paper_book.snapshot()
        symbols = [position["symbol"] for position in book.get("positions", [])]
        if symbols:
            book = paper_book.snapshot(studio.last_prices(symbols))
        return jsonify(book)

    @app.post("/api/portfolio")
    @require_token
    def api_portfolio_trade() -> Any:
        payload = request.get_json(silent=True) or {}
        symbol = str(payload.get("symbol", "")).strip().upper()
        action = str(payload.get("action", "")).strip().upper()
        name = str(payload.get("name", "") or symbol)
        try:
            qty = float(payload.get("qty", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "quantity must be a number"}), 400
        price = payload.get("price")
        try:
            if price in (None, "", 0):
                live = studio.last_prices([symbol])
                price = live.get(symbol.upper())
                if not price:
                    return jsonify(
                        {"error": "no live price for that symbol; type a price to fill"}
                    ), 400
            book = paper_book.trade(action, symbol, qty, float(price), name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        desk._on_event(
            SentinelEvent(
                datetime.now(IST),
                "info",
                f"Paper {action.lower()} {qty:g} {symbol}",
                f"Filled at {float(price):,.2f}",
                kind="trade",
            )
        )
        return jsonify({"ok": True, "book": book})

    def _render_daily_brief() -> Any:
        report = desk.sentinel.latest_report()
        commentary = ""
        if report is not None and report.snapshots:
            try:
                commentary = asyncio.run(desk.brain.desk_note(report.snapshots))
            except Exception:  # noqa: BLE001 - a brief without commentary is still valid
                LOGGER.exception("daily brief commentary failed")
        out_dir = studio.resolve_export_dir(desk.env_path, desk.settings.report_dir)
        try:
            artifact = studio.build_daily_brief(
                _desk_context(),
                out_dir=out_dir,
                commentary=commentary,
                owner=_env_value("OWNER_NAME"),
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("daily brief failed: %s", exc)
            return jsonify({"error": f"brief failed: {exc}"}), 502
        return send_file(
            artifact["path"],
            mimetype="application/pdf",
            as_attachment=True,
            download_name=artifact["filename"],
        )

    @app.get("/api/report/daily")
    def api_daily_brief() -> Any:
        return _render_daily_brief()

    @app.post("/api/report/daily")
    @require_token
    def api_daily_brief_post() -> Any:
        return _render_daily_brief()

    @app.post("/api/shortcut")
    @require_token
    def api_shortcut() -> Any:
        return jsonify(studio.ensure_shortcut())

    @app.get("/api/license")
    def api_license() -> Any:
        payload = studio.licence_text()
        payload["accepted"] = _env_value("LICENSE_ACCEPTED", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return jsonify(payload)

    @app.post("/api/license")
    @require_token
    def api_license_accept() -> Any:
        payload = request.get_json(silent=True) or {}
        accepted = bool(payload.get("accepted", True))
        try:
            settings_store.save({"LICENSE_ACCEPTED": accepted}, desk.env_path)
        except settings_store.SettingsValidationError as exc:
            return jsonify({"error": "validation failed", "fields": exc.errors}), 400
        return jsonify({"ok": True, "accepted": accepted})

    @app.get("/api/logs")
    def api_logs() -> Any:
        try:
            lines = max(20, min(int(request.args.get("lines", 200)), 2000))
        except ValueError:
            lines = 200
        path = desk.settings.log_dir / "tradevmx.log"
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            content = ["No log file yet. Start the sentinel or run a scan."]
        return Response("\n".join(content), mimetype="text/plain")

    @app.errorhandler(404)
    def _not_found(error: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return render_template("index.html", token=app.config["TRADEVMX_TOKEN"], version=APP_VERSION), 404

    return app
