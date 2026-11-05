"""TradeXVm :: alert dispatch (Telegram + Gmail SMTP).

* Telegram pushes HTML alerts to every configured chat and can deliver PDF
  documents via ``send_document``.
* Email uses ``smtplib`` with an SSL context and a Gmail app password, sending
  HTML bodies with optional PDF attachments.
* Every channel is isolated: a Telegram outage never blocks email and a mail
  failure never suppresses a push.  Alerts are de-duplicated per
  symbol+setup for ``ALERT_COOLDOWN_MINUTES`` so a 15-minute scanner does not
  spam the same signal all session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
import smtplib
import ssl
import time as _time
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from config import IST, Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from ai_engine import TradePlan
    from market_scanner import MarketSnapshot, ScanReport, Signal

LOGGER = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS; 587 is blocked by many corporate networks

SEVERITY_DOTS = {1: "\u25cb", 2: "\u25cb", 3: "\u25cf", 4: "\u25cf\u25cf", 5: "\u25cf\u25cf\u25cf"}


def _e(value: Any) -> str:
    return escape(str(value), quote=False)


def _pnl_arrow(change_pct: float) -> str:
    return "\u25b2" if change_pct >= 0 else "\u25bc"


def _num(value: float | None, digits: int = 2) -> str:
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "-"
    return f"{value:,.{digits}f}"


def _clip(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n<i>(truncated)</i>"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
class TelegramChannel:
    """Thin async wrapper around ``telegram.Bot`` with retries."""

    def __init__(self, settings: Settings, bot: Any | None = None) -> None:
        self.settings = settings
        self._bot = bot

    def attach_bot(self, bot: Any) -> None:
        self._bot = bot

    @property
    def ready(self) -> bool:
        return self._bot is not None and bool(self.settings.telegram_chat_ids)

    async def _send_one(self, chat_id: int, text: str) -> bool:
        from telegram.constants import ParseMode
        from telegram.error import RetryAfter, TelegramError

        for attempt in range(1, 4):
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=_clip(text),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return True
            except RetryAfter as exc:
                wait = float(getattr(exc, "retry_after", 3)) + 1
                LOGGER.warning("Telegram flood-wait %.1fs (chat %s)", wait, chat_id)
                await asyncio.sleep(wait)
            except TelegramError as exc:
                LOGGER.warning(
                    "Telegram send failed (chat %s, attempt %s/3): %s", chat_id, attempt, exc
                )
                await asyncio.sleep(1.5 * attempt)
            except Exception as exc:  # noqa: BLE001 - never crash a job
                LOGGER.exception("Unexpected Telegram error: %s", exc)
                return False
        return False

    async def broadcast(self, text: str) -> int:
        """Send to every configured chat; returns the success count."""
        if not self.ready:
            LOGGER.debug("Telegram channel not ready; skipping broadcast.")
            return 0
        results = await asyncio.gather(
            *(self._send_one(chat_id, text) for chat_id in self.settings.telegram_chat_ids),
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def send_document(
        self,
        document: bytes | Path,
        *,
        filename: str,
        caption: str = "",
        chat_id: int | None = None,
    ) -> int:
        """Deliver a PDF (bytes or path) to one chat or all configured chats."""
        if self._bot is None:
            return 0
        from telegram.error import TelegramError

        targets = (chat_id,) if chat_id is not None else self.settings.telegram_chat_ids
        if isinstance(document, Path):
            try:
                payload: bytes | Any = document.read_bytes()
            except OSError as exc:
                LOGGER.error("Cannot read attachment %s: %s", document, exc)
                return 0
        else:
            payload = document

        sent = 0
        for target in targets:
            for attempt in range(1, 3):
                try:
                    await self._bot.send_document(
                        chat_id=target,
                        document=payload,
                        filename=filename,
                        caption=_clip(caption, TELEGRAM_CAPTION_LIMIT) if caption else None,
                        parse_mode="HTML",
                    )
                    sent += 1
                    break
                except TelegramError as exc:
                    LOGGER.warning(
                        "send_document failed (chat %s, attempt %s/2): %s", target, attempt, exc
                    )
                    await asyncio.sleep(1.5 * attempt)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("Unexpected send_document error: %s", exc)
                    break
        return sent


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
class EmailChannel:
    """Gmail SMTP dispatcher (blocking; wrap with ``send_async``)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def ready(self) -> bool:
        return self.settings.email_ready

    def _message(
        self,
        subject: str,
        html_body: str,
        text_body: str,
        attachments: Sequence[Path],
    ) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"TradeXVm Desk <{self.settings.gmail_user}>"
        message["To"] = ", ".join(self.settings.resolved_recipients)
        message["Date"] = datetime.now(IST).strftime("%a, %d %b %Y %H:%M:%S %z")

        message.set_content(text_body or "This alert requires an HTML capable client.")
        message.add_alternative(html_body, subtype="html")

        for path in attachments:
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                LOGGER.error("Attachment %s unreadable: %s", path, exc)
                continue
            mime, _ = mimetypes.guess_type(str(path))
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            message.add_attachment(
                data, maintype=maintype, subtype=subtype or "octet-stream", filename=Path(path).name
            )
        return message

    def send(
        self,
        subject: str,
        html_body: str,
        *,
        text_body: str = "",
        attachments: Sequence[Path] = (),
    ) -> bool:
        if not self.ready:
            LOGGER.debug("Email channel not ready; skipping '%s'.", subject)
            return False

        message = self._message(subject, html_body, text_body, attachments)
        context = ssl.create_default_context()
        for attempt in range(1, 4):
            try:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=context) as server:
                    server.ehlo()
                    server.login(self.settings.gmail_user, self.settings.gmail_app_password)
                    server.send_message(message)
                LOGGER.info("Email sent: %s", subject)
                return True
            except smtplib.SMTPAuthenticationError as exc:
                LOGGER.error(
                    "Gmail authentication failed - use a 16-character App Password, not your "
                    "account password (%s).", exc
                )
                return False  # retrying cannot fix bad credentials
            except (smtplib.SMTPException, OSError) as exc:
                LOGGER.warning("Email attempt %s/3 failed: %s", attempt, exc)
                if attempt < 3:
                    _time.sleep(2 * attempt)  # blocking by design: runs in a worker thread
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Unexpected email failure: %s", exc)
                return False
        return False

    async def send_async(
        self,
        subject: str,
        html_body: str,
        *,
        text_body: str = "",
        attachments: Sequence[Path] = (),
    ) -> bool:
        return await asyncio.to_thread(
            self.send, subject, html_body, text_body=text_body, attachments=tuple(attachments)
        )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_signal_alert(
    snapshot: "MarketSnapshot",
    signals: Sequence["Signal"],
    plan: "TradePlan | None" = None,
) -> str:
    change_colour = "#0F7B3F" if snapshot.change_pct >= 0 else "#B3261E"
    lines = [
        f"{_pnl_arrow(snapshot.change_pct)} <b>{_e(snapshot.display_name)}</b>  "
        f"<code>{snapshot.timeframe}</code>  {snapshot.as_of.strftime('%H:%M')} IST",
        f"<b>{snapshot.cmp:,.2f}</b>  "
        f"<font color='{change_colour}'>{snapshot.change_pct:+.2f}%</font>  |  "
        f"Bias: <b>{_e(snapshot.bias)}</b>",
        f"VWAP {snapshot.vwap:,.2f} ({snapshot.vwap_distance_pct:+.2f}%)  |  "
        f"Range {snapshot.day_low:,.2f}-{snapshot.day_high:,.2f}",
        "",
        "<b>Setups</b>",
    ]
    for signal in signals:
        dots = SEVERITY_DOTS.get(signal.severity, "")
        lines.append(f"{dots} <b>{_e(signal.label)}</b> ({signal.severity}/5) \u2014 {_e(signal.detail)}")

    if snapshot.order_blocks:
        blocks = "; ".join(
            f"{ob.direction[:4]} {ob.bottom:,.2f}-{ob.top:,.2f} ({ob.state})"
            for ob in snapshot.order_blocks[:2]
        )
        lines += ["", f"<i>Order blocks: {_e(blocks)}</i>"]

    if plan is not None:
        lines += ["", f"<b>Desk plan \u00b7 {_e(plan.verdict)}</b>"]
        if plan.verdict == "NO TRADE":
            reason = plan.rationale[0] if plan.rationale else "Risk contract not met."
            lines.append(f"Stand aside \u2014 {_e(reason)}")
        else:
            lines += [
                f"Entry <b>{plan.entry_low:,.2f} \u2013 {plan.entry_high:,.2f}</b>",
                f"Invalidation <b>{plan.stop_loss:,.2f}</b>",
                f"T1 {plan.target_1:,.2f}  |  T2 {plan.target_2:,.2f}  |  "
                f"R:R 1:{plan.risk_reward:.2f}",
            ]
            if plan.invalidation:
                lines.append(f"<i>{_e(plan.invalidation)}</i>")

    lines += [
        "",
        f"<i>Feed {_e(snapshot.ltp_source)} \u00b7 /report {_e(snapshot.display_name)} for the PDF</i>",
    ]
    return _clip("\n".join(lines))


def format_snapshot_card(snapshot: "MarketSnapshot") -> str:
    """Compact card used by ``/live <symbol>``."""
    lines = [
        f"\U0001f4ca <b>{_e(snapshot.display_name)}</b> \u00b7 {_e(snapshot.timeframe)} \u00b7 "
        f"{snapshot.as_of.strftime('%H:%M')} IST",
        f"LTP <b>{snapshot.cmp:,.2f}</b> ({snapshot.change_pct:+.2f}%) \u00b7 "
        f"<b>{_e(snapshot.bias)}</b> \u00b7 {_e(snapshot.structure)}",
        "",
        f"VWAP {snapshot.vwap:,.2f} ({snapshot.vwap_distance_pct:+.2f}%)  "
        f"[{_e(snapshot.vwap_source)}]",
        f"Open {snapshot.day_open:,.2f}  H {snapshot.day_high:,.2f}  L {snapshot.day_low:,.2f}",
        f"Prev close {snapshot.prev_close:,.2f}  |  ATR(14) {snapshot.atr:,.2f}",
    ]
    if isinstance(snapshot.volume_ratio, float) and math.isfinite(snapshot.volume_ratio):
        lines.append(
            f"Volume {snapshot.volume:,.0f} = {snapshot.volume_ratio:.2f}x {_e('SMA')} "
            f"({snapshot.volume_sma:,.0f})"
        )
    else:
        lines.append("Volume: not published for this instrument")

    if snapshot.signals:
        lines += ["", "<b>Setups</b>"]
        for signal in sorted(snapshot.signals, key=lambda s: s.severity, reverse=True)[:6]:
            lines.append(f"\u2022 <b>{_e(signal.label)}</b> ({signal.timeframe}, {signal.severity}/5)")
    else:
        lines += ["", "<i>No qualifying setup right now.</i>"]

    if snapshot.supports or snapshot.resistances:
        lines += ["", "<b>Levels</b>"]
        if snapshot.resistances:
            lines.append(
                "R: " + ", ".join(f"{lv.price:,.2f} ({_e(lv.source)})" for lv in snapshot.resistances[:3])
            )
        if snapshot.supports:
            lines.append(
                "S: " + ", ".join(f"{lv.price:,.2f} ({_e(lv.source)})" for lv in snapshot.supports[:3])
            )

    if snapshot.warnings:
        lines += ["", "<i>" + _e(" | ".join(snapshot.warnings[:2])) + "</i>"]
    return _clip("\n".join(lines))


def format_market_pulse(report: "ScanReport") -> str:
    lines = [f"\U0001f1ee\U0001f1f3 <b>TradeXVm Market Pulse</b> \u00b7 {report.created_at.strftime('%H:%M')} IST"]
    for snapshot in report.snapshots:
        arrow = _pnl_arrow(snapshot.change_pct)
        top = max(snapshot.signals, key=lambda s: s.severity, default=None)
        tail = f" \u2014 {_e(top.label)}" if top else ""
        lines.append(
            f"{arrow} <b>{_e(snapshot.display_name)}</b> {snapshot.cmp:,.2f} "
            f"({snapshot.change_pct:+.2f}%) \u00b7 {_e(snapshot.bias)}{tail}"
        )
    if report.errors:
        lines += ["", "<i>Unavailable: " + _e(", ".join(report.errors)) + "</i>"]
    return _clip("\n".join(lines))


def format_scan_summary(report: "ScanReport", min_severity: int = 3) -> str:
    actionable = report.actionable(min_severity)
    lines = [
        f"\U0001f50e <b>Scan complete</b> \u00b7 {report.created_at.strftime('%H:%M')} IST",
        f"{report.scanned} instruments \u00b7 {len(actionable)} actionable "
        f"(severity \u2265 {min_severity}) \u00b7 {len(report.errors)} errors",
    ]
    if actionable:
        lines.append("")
        for snapshot in actionable:
            codes = ", ".join(dict.fromkeys(s.label for s in snapshot.actionable_signals(min_severity)))
            lines.append(
                f"\u2022 <b>{_e(snapshot.display_name)}</b> {snapshot.cmp:,.2f} "
                f"({snapshot.change_pct:+.2f}%) {_e(snapshot.bias)} \u2014 {_e(codes)}"
            )
    quiet = [s.display_name for s in report.snapshots if s.max_severity < min_severity]
    if quiet:
        lines += ["", f"<i>Quiet: {_e(', '.join(quiet))}</i>"]
    if report.errors:
        lines += ["", "<b>Errors</b>"]
        for symbol, error in report.errors.items():
            lines.append(f"\u2022 {_e(symbol)}: {_e(error[:160])}")
    return _clip("\n".join(lines))


def format_daily_wrap(report: "ScanReport", desk_note: str) -> str:
    lines = [
        f"\U0001f4d8 <b>TradeXVm End of Day</b> \u00b7 "
        f"{report.created_at.strftime('%d %b %Y')}",
        "",
        _e(desk_note),
        "",
        f"<b>Close snapshot</b> ({report.scanned} instruments)",
    ]
    for snapshot in report.snapshots:
        lines.append(
            f"\u2022 {_e(snapshot.display_name)} {snapshot.cmp:,.2f} "
            f"({snapshot.change_pct:+.2f}%) {_e(snapshot.bias)}"
        )
    lines += ["", "<i>Full PDF research report attached.</i>"]
    return _clip("\n".join(lines))


def _email_shell(title: str, body_html: str, subtitle: str = "") -> str:
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#F3F6F9;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F6F9;padding:18px 0;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="width:640px;max-width:94%;background:#FFFFFF;border:1px solid #D8E0E8;">
        <tr><td style="background:#0B1E33;padding:14px 20px;">
          <div style="color:#FFFFFF;font-size:17px;font-weight:bold;letter-spacing:1px;">TRADEXVM</div>
          <div style="color:#8FA6BD;font-size:10px;letter-spacing:2px;margin-top:3px;">
            INSTITUTIONAL MARKET DESK &nbsp;|&nbsp; NSE / BSE &nbsp;|&nbsp; INR
          </div>
        </td></tr>
        <tr><td style="background:#0E8C8A;height:3px;line-height:3px;font-size:0;">&nbsp;</td></tr>
        <tr><td style="padding:20px 22px 6px 22px;">
          <div style="color:#0B1E33;font-size:16px;font-weight:bold;">{_e(title)}</div>
          <div style="color:#6B7785;font-size:11px;margin-top:4px;">{_e(subtitle)}</div>
        </td></tr>
        <tr><td style="padding:6px 22px 22px 22px;color:#16202B;font-size:13px;line-height:1.55;">
          {body_html}
        </td></tr>
        <tr><td style="background:#F3F6F9;border-top:1px solid #D8E0E8;padding:12px 22px;
                       color:#6B7785;font-size:10px;line-height:1.5;">
          Generated automatically by TradeXVm from public market data. Educational research,
          not investment advice. Prices in INR.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class AlertDispatcher:
    """Fans out signals to Telegram and email with de-duplication."""

    def __init__(self, settings: Settings | None = None, bot: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.telegram = TelegramChannel(self.settings, bot)
        self.email = EmailChannel(self.settings)
        self._last_sent: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    def attach_bot(self, bot: Any) -> None:
        self.telegram.attach_bot(bot)

    # -------------------------------------------------------------- dedupe
    async def _claim(self, key: str) -> bool:
        """True when this key is outside its cooldown window."""
        cooldown = timedelta(minutes=self.settings.alert_cooldown_minutes)
        now = datetime.now(IST)
        async with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < cooldown:
                return False
            self._last_sent[key] = now
            # opportunistic pruning
            for stale in [k for k, ts in self._last_sent.items() if now - ts > timedelta(days=1)]:
                self._last_sent.pop(stale, None)
            return True

    async def _release(self, key: str) -> None:
        """Allow an immediate retry when no channel accepted the alert."""
        async with self._lock:
            self._last_sent.pop(key, None)

    # ------------------------------------------------------------ dispatch
    async def dispatch_snapshot(
        self, snapshot: "MarketSnapshot", plan: "TradePlan | None" = None
    ) -> bool:
        signals = snapshot.actionable_signals(self.settings.alert_min_severity)
        if not signals:
            return False

        key = f"{snapshot.symbol}:{snapshot.timeframe}:{','.join(s.code for s in signals)}"
        if not await self._claim(key):
            LOGGER.debug("Alert suppressed by cooldown for %s", key)
            return False

        text = format_signal_alert(snapshot, signals, plan)
        delivered = await self.telegram.broadcast(text)

        if max(s.severity for s in signals) >= self.settings.email_min_severity:
            subject = (
                f"TradeXVm Alert | {snapshot.display_name} | {signals[0].label} | "
                f"{snapshot.bias} | {snapshot.cmp:,.2f}"
            )
            html = _email_shell(
                f"{snapshot.display_name} \u00b7 {signals[0].label}",
                text.replace("\n", "<br/>"),
                subtitle=f"{snapshot.timeframe} \u00b7 {snapshot.as_of.strftime('%d %b %Y %H:%M')} IST",
            )
            if await self.email.send_async(subject, html, text_body=text):
                delivered += 1

        if delivered == 0:
            await self._release(key)
            LOGGER.error("Alert for %s reached no channel.", snapshot.symbol)
            return False
        return True

    async def dispatch_report(
        self, report: "ScanReport", plans: Mapping[str, "TradePlan"] | None = None
    ) -> int:
        """Route every actionable snapshot in a scan report."""
        plans = plans or {}
        sent = 0
        for snapshot in report.actionable(self.settings.alert_min_severity):
            try:
                if await self.dispatch_snapshot(snapshot, plans.get(snapshot.symbol)):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 - one bad alert must not stop the rest
                LOGGER.exception("Alert routing failed for %s: %s", snapshot.symbol, exc)
        return sent

    async def send_opening_brief(self, report: "ScanReport") -> None:
        text = (
            f"\U0001f7e2 <b>TradeXVm is live</b> \u00b7 {report.created_at.strftime('%d %b %Y')}\n"
            f"Session scan across {report.scanned} instruments.\n\n"
            + format_scan_summary(report, self.settings.alert_min_severity)
        )
        await self.telegram.broadcast(_clip(text))
        html = _email_shell(
            "Opening Bell Brief",
            text.replace("\n", "<br/>"),
            subtitle=report.created_at.strftime("%d %b %Y %H:%M IST"),
        )
        await self.email.send_async(
            f"TradeXVm Morning Brief | {report.created_at.strftime('%d %b %Y')}",
            html,
            text_body=text,
        )

    async def send_daily_wrap(
        self,
        report: "ScanReport",
        desk_note: str,
        pdf_path: Path | None,
    ) -> None:
        text = format_daily_wrap(report, desk_note)
        await self.telegram.broadcast(text)
        if pdf_path is not None:
            await self.telegram.send_document(
                pdf_path,
                filename=pdf_path.name,
                caption=f"\U0001f4c4 EOD research report \u00b7 {report.created_at.strftime('%d %b %Y')}",
            )

        attachments: list[Path] = [pdf_path] if pdf_path and pdf_path.exists() else []
        html = _email_shell(
            "End of Day Wrap",
            text.replace("\n", "<br/>"),
            subtitle=report.created_at.strftime("%d %b %Y") + " \u00b7 NSE close",
        )
        await self.email.send_async(
            f"TradeXVm EOD Wrap | {report.created_at.strftime('%d %b %Y')}",
            html,
            text_body=text,
            attachments=attachments,
        )
