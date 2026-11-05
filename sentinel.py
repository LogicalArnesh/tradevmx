"""TradeXVm :: autonomous sentinel engine.

One engine, two front-ends: the desktop console (``app.py``) and the Telegram
bot (``bot.py``) both drive this class, so an alert can never look different
depending on where you are watching from.

Owns
----
* an APScheduler :class:`BackgroundScheduler` in IST with the three jobs
  (opening brief, 15-minute sweep, end-of-day wrap), each market-hours gated,
* scan -> plan -> alert -> report orchestration,
* an event bus (subscribers) feeding the desktop feed and local notifier,
* a scan lock so a manual sweep and a scheduled sweep never interleave.

All public cycle methods are synchronous and must be called from a worker
thread (the scheduler pool, a Flask request thread, or ``asyncio.to_thread``);
they bridge into the async AI/alert layers with their own event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ai_engine import GroqBrain, TradePlan
from alerts import AlertDispatcher, format_scan_summary
from config import IST, Settings, get_settings
from market_scanner import MarketScanner, MarketSnapshot, ScanReport
from notify import LocalNotifier
from report_engine import ReportArtifact, ReportEngine

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SentinelEvent:
    """A single line in the desk feed."""

    at: datetime
    level: str  # "info" | "signal" | "error"
    title: str
    detail: str = ""
    symbol: str | None = None
    kind: str = "scan"  # scan | alert | brief | eod | system | error

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "time": self.at.strftime("%H:%M:%S"),
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "symbol": self.symbol,
            "kind": self.kind,
        }


def _run(coro: Any) -> Any:
    """Run a coroutine from a worker thread that owns no event loop."""
    return asyncio.run(coro)


class Sentinel:
    """Scheduler + orchestration for the autonomous desk."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        dispatcher: AlertDispatcher | None = None,
        notifier: LocalNotifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.dispatcher = dispatcher or AlertDispatcher(self.settings)
        self.notifier = notifier or LocalNotifier(
            toast=self.settings.desktop_toast, speak=self.settings.voice_alerts
        )

        self._scanner: MarketScanner | None = None
        self._brain: GroqBrain | None = None
        self._reports: ReportEngine | None = None

        self._scan_lock = threading.Lock()
        self._subscribers: list[Callable[[SentinelEvent], None]] = []
        self._scheduler = BackgroundScheduler(
            timezone=IST,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
        )

        self._report: ScanReport | None = None
        self._plans: dict[str, TradePlan] = {}
        self._last_scan_at: datetime | None = None
        self._last_alert_count = 0

    # --------------------------------------------------------- components
    @property
    def scanner(self) -> MarketScanner:
        if self._scanner is None:
            self._scanner = MarketScanner(self.settings)
        return self._scanner

    @property
    def brain(self) -> GroqBrain:
        if self._brain is None:
            self._brain = GroqBrain(self.settings)
        return self._brain

    @property
    def reports(self) -> ReportEngine:
        if self._reports is None:
            self._reports = ReportEngine(self.settings)
        return self._reports

    # ------------------------------------------------------------- events
    def subscribe(self, callback: Callable[[SentinelEvent], None]) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def _emit(self, event: SentinelEvent) -> None:
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a UI bug must not stop the desk
                LOGGER.exception("Event subscriber failed")
        if event.level in {"signal", "error"}:
            self.notifier.announce(
                f"{event.title}" + (f" \u00b7 {event.symbol}" if event.symbol else ""),
                event.detail,
            )

    # ----------------------------------------------------------- schedule
    def start(self) -> bool:
        if self._scheduler.running:
            return False
        self._register_jobs()
        self._scheduler.start()
        self._emit(
            SentinelEvent(
                datetime.now(IST),
                "info",
                "Sentinel armed",
                f"Sweep every {self.settings.scan_interval_minutes} min inside 09:15-15:30 IST",
                kind="system",
            )
        )
        LOGGER.info("Sentinel armed (jobs=%s)", len(self._scheduler.get_jobs()))
        if self.settings.is_market_open():
            threading.Thread(
                target=self._guarded, args=(self.cycle_intraday,), name="first-sweep", daemon=True
            ).start()
        return True

    def stop(self) -> bool:
        if not self._scheduler.running:
            return False
        self._scheduler.shutdown(wait=False)
        self._emit(
            SentinelEvent(datetime.now(IST), "info", "Sentinel stopped", "No autonomous sweeps", kind="system")
        )
        LOGGER.info("Sentinel stopped.")
        return True

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def _register_jobs(self) -> None:
        interval = max(1, min(self.settings.scan_interval_minutes, 59))
        brief = self.settings.opening_brief_time
        eod = self.settings.eod_wrap_time

        self._scheduler.add_job(
            self._guarded_intraday,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute=f"*/{interval}", timezone=IST),
            id="intraday_scan",
            name="Intraday sweep",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._guarded_brief,
            CronTrigger(day_of_week="mon-fri", hour=brief.hour, minute=brief.minute, timezone=IST),
            id="opening_brief",
            name="Opening bell brief",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._guarded_eod,
            CronTrigger(day_of_week="mon-fri", hour=eod.hour, minute=eod.minute, timezone=IST),
            id="eod_wrap",
            name="End of day wrap",
            replace_existing=True,
        )

    def _next_intraday_run(self, now: datetime) -> datetime | None:
        """First cron slot at or after ``now`` that actually falls in session.

        The cron fires at 09:00 and 15:45 too; those ticks are rejected by the
        market-hours gate, so showing the raw cron time in the UI would lie.
        """
        step = max(1, min(self.settings.scan_interval_minutes, 59))
        base = now.astimezone(IST).replace(second=0, microsecond=0)
        offset = (base.minute // step + 1) * step
        candidate = base.replace(minute=0) + timedelta(minutes=offset)
        for _ in range(4 * 24 * 60 // step + 8):
            if self.settings.is_market_open(candidate):
                return candidate
            candidate += timedelta(minutes=step)
        return None

    def schedule_table(self) -> list[dict[str, Any]]:
        if not self._scheduler.running:
            return []
        rows: list[dict[str, Any]] = []
        now = datetime.now(IST)
        for job in self._scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            cron_next = next_run.astimezone(IST) if next_run else None
            effective = cron_next
            if job.id == "intraday_scan":
                effective = self._next_intraday_run(now) or cron_next
            rows.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": effective.isoformat() if effective else None,
                    "next_label": effective.strftime("%a %H:%M") if effective else None,
                    "cron_next": cron_next.strftime("%a %H:%M") if cron_next else None,
                }
            )
        return rows

    # ------------------------------------------------------------- access
    def latest_report(self) -> ScanReport | None:
        return self._report

    def latest_plans(self) -> dict[str, TradePlan]:
        return dict(self._plans)

    @property
    def last_scan_at(self) -> datetime | None:
        return self._last_scan_at

    @property
    def last_alert_count(self) -> int:
        return self._last_alert_count

    # -------------------------------------------------------------- scan
    def scan(self, force: bool = True) -> ScanReport:
        with self._scan_lock:
            report = self.scanner.scan(force=force)
            self._report = report
            self._last_scan_at = datetime.now(IST)

        actionable = report.actionable(self.settings.alert_min_severity)
        LOGGER.info(
            "Sentinel sweep: %s instruments, %s actionable, %s errors",
            report.scanned,
            len(actionable),
            len(report.errors),
        )
        if report.errors:
            self._emit(
                SentinelEvent(
                    datetime.now(IST),
                    "error",
                    f"{len(report.errors)} instrument(s) unavailable",
                    ", ".join(f"{k}: {v}" for k, v in list(report.errors.items())[:3])[:220],
                    kind="error",
                )
            )
        return report

    def plans_for(self, snapshots: Sequence[MarketSnapshot]) -> dict[str, TradePlan]:
        plans: dict[str, TradePlan] = {}
        for snapshot in snapshots:
            try:
                plans[snapshot.symbol] = _run(self.brain.plan(snapshot))
            except Exception as exc:  # noqa: BLE001 - fallback lives inside the brain
                LOGGER.exception("Plan generation failed for %s: %s", snapshot.symbol, exc)
        return plans

    # ------------------------------------------------------------- cycles
    def cycle_intraday(self) -> int:
        """One sweep: scan, plan actionable names, push alerts, feed the UI."""
        if not self.settings.is_market_open():
            LOGGER.debug("Sweep skipped: market closed.")
            return 0

        report = self.scan(force=True)
        actionable = report.actionable(self.settings.alert_min_severity)
        plans = self.plans_for(actionable)
        self._plans.update(plans)
        delivered = _run(self.dispatcher.dispatch_report(report, plans))
        self._last_alert_count = delivered

        for snapshot in actionable:
            top = max(snapshot.signals, key=lambda s: s.severity, default=None)
            self._emit(
                SentinelEvent(
                    datetime.now(IST),
                    "signal",
                    f"{snapshot.display_name} \u00b7 {snapshot.bias}",
                    (
                        f"{top.label if top else 'setup'} \u00b7 CMP {snapshot.cmp:,.2f} "
                        f"({snapshot.change_pct:+.2f}%) \u00b7 VWAP {snapshot.vwap:,.2f}"
                    ),
                    symbol=snapshot.display_name,
                    kind="alert",
                )
            )
        return delivered

    def cycle_opening_brief(self) -> None:
        if not self.settings.is_market_open():
            return
        report = self.scan(force=True)
        _run(self.dispatcher.send_opening_brief(report))
        self._emit(
            SentinelEvent(
                datetime.now(IST),
                "info",
                "Opening brief sent",
                f"{report.scanned} instruments scanned",
                kind="brief",
            )
        )

    def cycle_eod(self) -> ReportArtifact | None:
        if not self.settings.is_trading_day():
            LOGGER.debug("EOD wrap skipped: not a trading day.")
            return None
        report = self.scan(force=True)
        if not report.snapshots:
            self._emit(
                SentinelEvent(datetime.now(IST), "error", "EOD wrap aborted", "No market data", kind="error")
            )
            return None

        plans = self.plans_for(report.snapshots)
        self._plans.update(plans)
        artifact = self.reports.render_portfolio(report.snapshots, plans, title="End of Day Wrap")
        note = _run(self.brain.desk_note(report.snapshots))
        _run(self.dispatcher.send_daily_wrap(report, note, artifact.path))
        self._emit(
            SentinelEvent(
                datetime.now(IST),
                "info",
                "End of day wrap delivered",
                f"{artifact.filename} \u00b7 {artifact.size_kb} KB",
                kind="eod",
            )
        )
        return artifact

    # ------------------------------------------------------------- report
    def build_report(self, symbol: str) -> tuple[ReportArtifact, TradePlan, MarketSnapshot]:
        instrument = self.settings.resolve_instrument(symbol)
        snapshot = self.scanner.snapshot(instrument, force=True)
        plan = _run(self.brain.plan(snapshot))
        artifact = self.reports.render(snapshot, plan)
        self._emit(
            SentinelEvent(
                datetime.now(IST),
                "info",
                f"Report generated \u00b7 {snapshot.display_name}",
                f"{plan.verdict} \u00b7 {artifact.filename}",
                symbol=snapshot.display_name,
                kind="system",
            )
        )
        return artifact, plan, snapshot

    # -------------------------------------------------------------- teardown
    def close(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        if self._scanner is not None:
            self._scanner.close()
            self._scanner = None

    # ------------------------------------------------------------ wrappers
    def _guarded(self, cycle: Callable[[], Any]) -> None:
        try:
            cycle()
        except Exception as exc:  # noqa: BLE001 - the scheduler must survive
            LOGGER.exception("Sentinel cycle failed: %s", exc)
            self._emit(
                SentinelEvent(
                    datetime.now(IST),
                    "error",
                    "Sentinel error",
                    str(exc)[:220],
                    kind="error",
                )
            )

    def _guarded_intraday(self) -> None:
        self._guarded(self.cycle_intraday)

    def _guarded_brief(self) -> None:
        self._guarded(self.cycle_opening_brief)

    def _guarded_eod(self) -> None:
        self._guarded(self.cycle_eod)
