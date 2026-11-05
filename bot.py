"""TradeVMx :: Telegram front-end for the shared sentinel engine.

The autonomous work (sweeps, brief, wrap) lives in ``sentinel.py``; this module
is only the chat surface: commands, authorization, and formatting. That keeps
Telegram and the desktop console on exactly the same engine.

Run:  ``python bot.py``   (requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from alerts import (
    AlertDispatcher,
    format_market_pulse,
    format_snapshot_card,
    format_scan_summary,
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
from notify import LocalNotifier
from sentinel import Sentinel

LOGGER = logging.getLogger(__name__)

COMMANDS: tuple[tuple[str, str], ...] = (
    ("live", "Live pulse: /live or /live RELIANCE"),
    ("report", "AI research PDF: /report BANKNIFTY"),
    ("scan", "Full watchlist sweep with signals"),
    ("status", "Desk, scheduler and data status"),
    ("watchlist", "Instruments under surveillance"),
    ("wrap", "Send the end-of-day wrap now"),
    ("help", "Command reference"),
)


class TradeVMxBot:
    """Telegram surface over :class:`sentinel.Sentinel`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.alerts = AlertDispatcher(settings)
        self.sentinel = Sentinel(
            settings,
            dispatcher=self.alerts,
            notifier=LocalNotifier(toast=False, speak=False),
        )
        self._app: Application | None = None

    # ------------------------------------------------------------ shortcuts
    @property
    def scanner(self) -> object:
        return self.sentinel.scanner

    @property
    def brain(self) -> object:
        return self.sentinel.brain

    @property
    def reports(self) -> object:
        return self.sentinel.reports

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
        self.sentinel.start()
        LOGGER.info(
            "TradeVMx bot online | chats=%s | universe=%s | sweep=%smin | groq=%s | email=%s",
            len(self.settings.telegram_chat_ids),
            len(self.settings.watchlist),
            self.settings.scan_interval_minutes,
            "on" if self.settings.groq_api_key else "fallback",
            "on" if self.settings.email_ready else "off",
        )

    async def _post_shutdown(self, application: Application) -> None:
        self.sentinel.stop()
        self.sentinel.close()
        LOGGER.info("TradeVMx bot stopped.")

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
        for start in range(0, len(text), 4000):
            await message.reply_text(
                text[start : start + 4000],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    def _resolve(self, query: str) -> Instrument:
        return self.settings.resolve_instrument(query)

    # ------------------------------------------------------------ commands
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self._reply(
            update,
            "\U0001f5e1 <b>TradeVMx online</b>\n"
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
        lines = ["<b>TradeVMx commands</b>"]
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
                snapshot = await asyncio.to_thread(self.sentinel.scanner.snapshot, instrument, force=True)
                await self._reply(update, format_snapshot_card(snapshot))
                return
            indices = [inst for inst in self.settings.watchlist if inst.is_index][:2]
            report = await asyncio.to_thread(self.sentinel.scanner.scan, indices, force=True)
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
        status = (
            await update.effective_message.reply_text(
                f"\U0001f4c4 Building research note for <b>{query}</b>...",
                parse_mode=ParseMode.HTML,
            )
            if update.effective_message
            else None
        )
        try:
            artifact, plan, snapshot = await asyncio.to_thread(self.sentinel.build_report, query)
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
            report = await asyncio.to_thread(self.sentinel.scan, True)
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
        schedule = self.sentinel.schedule_table()
        lines = [
            "<b>TradeVMx status</b>",
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
            f"Sentinel: {'armed' if self.sentinel.running else 'idle'}",
        ]
        if schedule:
            lines += ["", "<b>Scheduled jobs</b>"]
            for job in schedule:
                lines.append(f"\u2022 {job['name']}: next {job['next_label'] or 'not armed'}")
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
        try:
            artifact = await asyncio.to_thread(self.sentinel.cycle_eod)
            if artifact is None:
                await self._reply(update, "\u26a0\ufe0f Wrap skipped: no market data available.")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("wrap failed: %s", exc)
            await self._reply(update, f"\u26a0\ufe0f Wrap failed: {str(exc)[:180]}")

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
        LOGGER.error("TradeVMx cannot start: %s", exc)
        LOGGER.error(
            "Create .env from .env.example (or open the desktop console) and set "
            "TELEGRAM_BOT_TOKEN plus at least one TELEGRAM_CHAT_ID, then restart."
        )
        return 2

    bot = TradeVMxBot(settings)
    try:
        bot.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        LOGGER.info("Interrupted by operator.")
    finally:
        bot.sentinel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
