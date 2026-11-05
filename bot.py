"""TradeXVm :: Telegram desk entry point.

Wires together the scanner, Groq brain, PDF engine and alert dispatcher behind a
``python-telegram-bot`` v20+ application, and drives autonomy with an
APScheduler :class:`AsyncIOScheduler` (Asia/Kolkata):

===============  =========================================================
09:16 Mon-Fri    opening bell brief  -> Telegram + email
every 15 min     intraday sweep      -> alerts for qualifying setups
15:40 Mon-Fri    end-of-day wrap     -> PDF report + email attachment
===============  =========================================================

Every job is additionally gated by ``Settings.is_market_open`` so a cron tick at
09:00 or 15:45 can never touch the tape outside the session.

Run:  ``python bot.py``  (requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ai_engine import GroqBrain, TradePlan
from alerts import (
    AlertDispatcher,
    format_market_pulse,
    format_scan_summary,
    format_snapshot_card,
)
from config import (
    IST,
    ConfigError,
    Instrument,
    Settings,
    UnknownInstrument,
    configure_logging,
    get_settings,
)
from market_scanner import MarketScanner, ScanReport
from report_engine import ReportEngine

LOGGER = logging.getLogger(__name__)

COMMANDS: tuple[tuple[str, str], ...] = (
    ("live", "Live pulse: /live or /live RELIANCE"),
    ("report", "AI research PDF: /report BANKNIFTY"),
    ("scan", "Full watchlist sweep with signals"),
    ("status", "Desk, scheduler and data-status"),
    ("watchlist", "Instruments under surveillance"),
    ("wrap", "Send the end-of-day wrap now"),
    ("help", "Command reference"),
)


class TradeXVmBot:
    """Owns the Telegram application and every background job."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scanner = MarketScanner(settings)
        self.brain = GroqBrain(settings)
        self.reports = ReportEngine(settings)
        self.alerts = AlertDispatcher(settings)
        self.scheduler = AsyncIOScheduler(
            timezone=IST,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
        )
        self._scan_lock = asyncio.Lock()
        self._app: Application | None = None

    # ------------------------------------------------------------ lifecycle
    def build_application(self) -> Application:
        application = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self._app = application
        self.alerts.attach_bot(application.bot)

        application.add_handler(CommandHandler("start", self.cmd_start))
        application.add_handler(CommandHandler("help", self.cmd_help))
        application.add_handler(CommandHandler("live", self.cmd_live))
        application.add_handler(CommandHandler("report", self.cmd_report))
        application.add_handler(CommandHandler("scan", self.cmd_scan))
        application.add_handler(CommandHandler("status", self.cmd_status))
        application.add_handler(CommandHandler("watchlist", self.cmd_watchlist))
        application.add_handler(CommandHandler("wrap", self.cmd_wrap))
        application.add_handler(MessageHandler(filters.COMMAND, self.cmd_unknown))
        application.add_error_handler(self.on_error)
        return application

    async def _post_init(self, application: Application) -> None:
        with contextlib.suppress(TelegramError):
            await application.bot.set_my_commands(
                [BotCommand(command, description) for command, description in COMMANDS]
            )
        self._schedule_jobs()
        LOGGER.info(
            "TradeXVm online | chats=%s | universe=%s | scan=%smin | groq=%s | email=%s",
            len(self.settings.telegram_chat_ids),
            len(self.settings.watchlist),
            self.settings.scan_interval_minutes,
            "on" if self.settings.groq_api_key else "fallback",
            "on" if self.settings.email_ready else "off",
        )

    async def _post_shutdown(self, application: Application) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.scanner.close()
        LOGGER.info("TradeXVm stopped.")

    def _schedule_jobs(self) -> None:
        interval = max(1, min(self.settings.scan_interval_minutes, 59))
        brief = self.settings.opening_brief_time
        eod = self.settings.eod_wrap_time

        self.scheduler.add_job(
            self.job_intraday_scan,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute=f"*/{interval}", timezone=IST),
            id="intraday_scan",
            name="Intraday sweep",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.job_opening_brief,
            CronTrigger(day_of_week="mon-fri", hour=brief.hour, minute=brief.minute, timezone=IST),
            id="opening_brief",
            name="Opening bell brief",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.job_eod_wrap,
            CronTrigger(day_of_week="mon-fri", hour=eod.hour, minute=eod.minute, timezone=IST),
            id="eod_wrap",
            name="End of day wrap",
            replace_existing=True,
        )
        if not self.scheduler.running:
            self.scheduler.start()
        LOGGER.info(
            "Scheduler armed: sweep */%s min (09:15-15:30 IST), brief %s, EOD %s",
            interval, brief.strftime("%H:%M"), eod.strftime("%H:%M"),
        )

    def run(self) -> None:
        application = self.build_application()
        LOGGER.info("Starting Telegram polling...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,
        )

    # -------------------------------------------------------------- helpers
    @property
    def app(self) -> Application:
        if self._app is None:
            raise RuntimeError("Application has not been built")
        return self._app

    def _authorised(self, update: Update) -> bool:
        chat = update.effective_chat
        user = update.effective_user
        allowed = set(self.settings.telegram_chat_ids)
        return bool(
            (chat is not None and chat.id in allowed)
            or (user is not None and user.id in allowed)
        )

    async def _guard(self, update: Update) -> bool:
        if self._authorised(update):
            return True
        chat_id = update.effective_chat.id if update.effective_chat else "?"
        LOGGER.warning("Rejected unauthorised command from chat %s", chat_id)
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "\u26d4 Not authorised. Add this chat id to TELEGRAM_CHAT_IDS to enable the desk."
            )
        return False

    async def _typing(self, update: Update, action: str = ChatAction.TYPING) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        with contextlib.suppress(TelegramError, Exception):
            await self.app.bot.send_chat_action(chat_id=chat.id, action=action)

    async def _reply(self, update: Update, text: str) -> None:
        message = update.effective_message
        if message is None:
            return
        for chunk_start in range(0, len(text), 4000):
            await message.reply_text(
                text[chunk_start : chunk_start + 4000],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    def _resolve(self, query: str) -> Instrument:
        return self.settings.resolve_instrument(query)

    async def _scan(self, *, force: bool) -> ScanReport:
        if self._scan_lock.locked():
            LOGGER.info("Scan already running; returning cached sweep.")
        async with self._scan_lock:
            return await asyncio.to_thread(self.scanner.scan, None, force=force)

    async def _plans_for(self, report: ScanReport) -> dict[str, TradePlan]:
        plans: dict[str, TradePlan] = {}
        for snapshot in report.snapshots:
            try:
                plans[snapshot.symbol] = await self.brain.plan(snapshot)
            except Exception as exc:  # noqa: BLE001 - plan failures are non-fatal
                LOGGER.exception("Plan generation failed for %s: %s", snapshot.symbol, exc)
        return plans

    # ---------------------------------------------------------------- jobs
    async def job_intraday_scan(self) -> None:
        if not self.settings.is_market_open():
            LOGGER.debug("Sweep skipped: market closed (%s IST).", datetime.now(IST).strftime("%H:%M"))
            return
        started = datetime.now(IST)
        try:
            report = await self._scan(force=True)
            actionable = report.actionable(self.settings.alert_min_severity)
            plans: dict[str, TradePlan] = {}
            for snapshot in actionable:
                plans[snapshot.symbol] = await self.brain.plan(snapshot)

            delivered = await self.alerts.dispatch_report(report, plans)
            LOGGER.info(
                "Sweep %s: %s scanned, %s actionable, %s alerts delivered, %s errors (%.1fs)",
                started.strftime("%H:%M"),
                report.scanned,
                len(actionable),
                delivered,
                len(report.errors),
                (datetime.now(IST) - started).total_seconds(),
            )
            if not report.snapshots:
                await self.alerts.telegram.broadcast(
                    "\u26a0\ufe0f <b>TradeXVm</b>: the sweep returned no data. "
                    "Check provider connectivity."
                )
        except Exception as exc:  # noqa: BLE001 - the scheduler must survive
            LOGGER.exception("Intraday sweep failed: %s", exc)
            await self.alerts.telegram.broadcast(
                f"\u26a0\ufe0f <b>TradeXVm sweep error</b>: {str(exc)[:180]}"
            )

    async def job_opening_brief(self) -> None:
        if not self.settings.is_market_open():
            return
        try:
            report = await self._scan(force=True)
            await self.alerts.send_opening_brief(report)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Opening brief failed: %s", exc)

    async def job_eod_wrap(self) -> None:
        if not self.settings.is_trading_day():
            LOGGER.debug("EOD wrap skipped: not a trading day.")
            return
        try:
            report = await self._scan(force=True)
            if not report.snapshots:
                LOGGER.error("EOD wrap aborted: no data.")
                return
            plans = await self._plans_for(report)
            artifact = await asyncio.to_thread(
                self.reports.render_portfolio, report.snapshots, plans, title="End of Day Wrap"
            )
            note = await self.brain.desk_note(report.snapshots)
            await self.alerts.send_daily_wrap(report, note, artifact.path)
            LOGGER.info("EOD wrap delivered (%s, %.1f KB).", artifact.filename, artifact.size_kb)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("EOD wrap failed: %s", exc)
            await self.alerts.telegram.broadcast(
                f"\u26a0\ufe0f <b>TradeXVm EOD error</b>: {str(exc)[:180]}"
            )

    # ------------------------------------------------------------ commands
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._reply(
            update,
            "\U0001f5e1 <b>TradeXVm online</b>\n"
            "Autonomous NSE desk: VWAP, volume shocks, Open=High/Low and SMC order-block "
            "retests on 5m/15m, with Groq trade plans.\n\n"
            f"\u2022 Scans every {self.settings.scan_interval_minutes} min, 09:15-15:30 IST\n"
            "\u2022 <code>/live</code> index pulse, <code>/live RELIANCE</code> full card\n"
            "\u2022 <code>/report BANKNIFTY</code> AI research PDF\n"
            "\u2022 <code>/scan</code> full watchlist sweep\n"
            "\u2022 <code>/status</code> desk health\n\n"
            "<i>Educational research, not investment advice.</i>",
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        lines = ["<b>TradeXVm commands</b>"]
        lines.extend(f"/{command} \u2014 {description}" for command, description in COMMANDS)
        lines.append("\nAny NSE ticker works with /live and /report (e.g. <code>TATAMOTORS</code>).")
        await self._reply(update, "\n".join(lines))

    async def cmd_live(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._typing(update)
        query = " ".join(context.args).strip() if context.args else ""
        try:
            if query:
                instrument = self._resolve(query)
                snapshot = await asyncio.to_thread(self.scanner.snapshot, instrument, force=True)
                await self._reply(update, format_snapshot_card(snapshot))
                return
            indices = [inst for inst in self.settings.watchlist if inst.is_index][:2]
            report = await asyncio.to_thread(self.scanner.scan, indices, force=True)
            text = format_market_pulse(report)
            if self.settings.is_market_open():
                text += "\n\n<i>Full tape: /scan \u00b7 PDF: /report NIFTY</i>"
            else:
                text += "\n\n<i>Market closed \u2014 levels are the last available session.</i>"
            await self._reply(update, text)
        except UnknownInstrument as exc:
            await self._reply(update, f"\u26a0\ufe0f {exc}. Try <code>/live NIFTY</code>.")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("live failed: %s", exc)
            await self._reply(update, f"\u26a0\ufe0f Live data unavailable: {str(exc)[:180]}")

    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        chat = update.effective_chat
        if chat is None:
            return
        query = " ".join(context.args).strip() if context.args else "NIFTY 50"
        await self._typing(update, ChatAction.UPLOAD_DOCUMENT)
        status = await update.effective_message.reply_text(
            f"\U0001f4c4 Building research note for <b>{query}</b>...",
            parse_mode=ParseMode.HTML,
        ) if update.effective_message else None
        try:
            instrument = self._resolve(query)
            snapshot = await asyncio.to_thread(self.scanner.snapshot, instrument, force=True)
            plan = await self.brain.plan(snapshot)
            artifact = await asyncio.to_thread(self.reports.render, snapshot, plan)

            caption = (
                f"<b>{snapshot.display_name}</b> \u00b7 {snapshot.timeframe} \u00b7 "
                f"{snapshot.as_of.strftime('%d %b %H:%M')} IST\n"
                f"CMP {snapshot.cmp:,.2f} ({snapshot.change_pct:+.2f}%) \u00b7 Bias {snapshot.bias}\n"
                f"Verdict <b>{plan.verdict}</b>"
                + (f" \u00b7 R:R 1:{plan.risk_reward:.2f}" if plan.actionable else "")
            )
            await self.app.bot.send_document(
                chat_id=chat.id,
                document=artifact.buffer,
                filename=artifact.filename,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            LOGGER.info("Report delivered: %s (%.1f KB)", artifact.filename, artifact.size_kb)
        except UnknownInstrument as exc:
            await self._reply(update, f"\u26a0\ufe0f {exc}. Example: <code>/report RELIANCE</code>.")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("report failed: %s", exc)
            await self._reply(update, f"\u26a0\ufe0f Report generation failed: {str(exc)[:180]}")
        finally:
            if status is not None:
                with contextlib.suppress(TelegramError):
                    await status.delete()

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._typing(update)
        try:
            report = await self._scan(force=True)
            await self._reply(
                update, format_scan_summary(report, self.settings.alert_min_severity)
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("scan failed: %s", exc)
            await self._reply(update, f"\u26a0\ufe0f Scan failed: {str(exc)[:180]}")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        now = datetime.now(IST)
        jobs = [job for job in self.scheduler.get_jobs()] if self.scheduler.running else []
        lines = [
            "<b>TradeXVm status</b>",
            f"IST clock: {now.strftime('%a %d %b %Y, %H:%M:%S')}",
            f"Market: <b>{self.settings.market_phase(now)}</b> "
            f"({'open' if self.settings.is_market_open(now) else 'closed'})",
            "",
            f"Universe: {len(self.settings.watchlist)} instruments "
            f"({sum(1 for i in self.settings.watchlist if i.is_index)} indices)",
            f"Sweep: every {self.settings.scan_interval_minutes} min on "
            f"{self.settings.scan_timeframe} (+{self.settings.confirm_timeframe} confirmation)",
            f"Risk floor: 1:{self.settings.min_risk_reward:.1f}  |  alert severity \u2265 "
            f"{self.settings.alert_min_severity}",
            "",
            f"Groq: {'configured' if self.settings.groq_api_key else 'fallback engine'} "
            f"({self.settings.groq_model})",
            f"Email: {'ready' if self.settings.email_ready else 'disabled'} "
            f"({len(self.settings.resolved_recipients)} recipients)",
            f"Telegram: {len(self.settings.telegram_chat_ids)} chat(s)",
        ]
        if jobs:
            lines += ["", "<b>Scheduled jobs</b>"]
            for job in jobs:
                next_run = getattr(job, "next_run_time", None)
                stamp = next_run.astimezone(IST).strftime("%a %H:%M") if next_run else "not armed"
                lines.append(f"\u2022 {job.name}: next {stamp}")
        await self._reply(update, "\n".join(lines))

    async def cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        lines = ["<b>Watchlist</b>"]
        for instrument in self.settings.watchlist:
            tag = "index" if instrument.is_index else "equity"
            lines.append(f"\u2022 <code>{instrument.symbol}</code> \u2014 {instrument.name} ({tag})")
        lines.append("\nOverride with <code>WATCHLIST</code> in <code>.env</code>.")
        await self._reply(update, "\n".join(lines))

    async def cmd_wrap(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._typing(update, ChatAction.UPLOAD_DOCUMENT)
        await self._reply(update, "\U0001f4d8 Compiling the wrap and mailing the PDF...")
        await self.job_eod_wrap()

    async def cmd_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._reply(update, "Unknown command. Use /help.")

    # --------------------------------------------------------------- error
    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.error("Handler error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message is not None:
            with contextlib.suppress(TelegramError):
                await update.effective_message.reply_text(
                    "\u26a0\ufe0f Something went wrong handling that command. It has been logged."
                )


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    try:
        settings.require_runnable()
    except ConfigError as exc:
        LOGGER.error("TradeXVm cannot start: %s", exc)
        LOGGER.error(
            "Create .env from .env.example and set TELEGRAM_BOT_TOKEN plus at least one "
            "TELEGRAM_CHAT_ID, then restart."
        )
        return 2

    bot = TradeXVmBot(settings)
    try:
        bot.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        LOGGER.info("Interrupted by operator.")
    finally:
        bot.scanner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
