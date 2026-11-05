"""TradeXVm :: configuration layer.

Single source of truth for credentials, watchlist, detector thresholds and
market-session rules.  Everything is resolved once from the environment and
exposed through an immutable :class:`Settings` object.

Design notes
------------
* No module imports credentials directly - always go through ``get_settings``.
* Missing optional secrets degrade gracefully (Groq -> deterministic plans,
  email -> Telegram only).  Only Telegram is a hard requirement, because the
  product *is* a Telegram desk.
* All timestamps are handled in ``Asia/Kolkata`` (IST).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final, Iterable, Sequence
from zoneinfo import ZoneInfo

BASE_DIR: Final[Path] = Path(__file__).resolve().parent

try:  # python-dotenv is optional at runtime for air-gapped deployments
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=False)
except Exception:  # pragma: no cover - dotenv is a soft dependency
    pass

LOGGER = logging.getLogger(__name__)

IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: Cash-market timing (naive times compared against ``datetime.time()`` in IST).
MARKET_OPEN_TIME: Final[time] = time(9, 15)
MARKET_CLOSE_TIME: Final[time] = time(15, 30)
PRE_OPEN_TIME: Final[time] = time(9, 0)


class ConfigError(RuntimeError):
    """Raised when the environment cannot satisfy a hard runtime requirement."""


class UnknownInstrument(ValueError):
    """Raised when a requested symbol cannot be resolved to an instrument."""


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable/inspectable symbol.

    ``symbol``     - provider ticker (yfinance compatible)
    ``nse_symbol`` - NSE identifier used by nsepython
    """

    symbol: str
    name: str
    kind: str = "equity"  # "index" | "equity"
    nse_symbol: str | None = None

    @property
    def is_index(self) -> bool:
        return self.kind == "index"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.name} ({self.symbol})"


NIFTY_50: Final[Instrument] = Instrument(
    symbol="^NSEI", name="NIFTY 50", kind="index", nse_symbol="NIFTY 50"
)
BANK_NIFTY: Final[Instrument] = Instrument(
    symbol="^NSEBANK", name="BANK NIFTY", kind="index", nse_symbol="NIFTY BANK"
)

_INDEX_INSTRUMENTS: Final[dict[str, Instrument]] = {
    "^NSEI": NIFTY_50,
    "^NSEBANK": BANK_NIFTY,
}

#: Upper-case, alphanumerics only, used for tolerant symbol matching.
_ALIASES: Final[dict[str, str]] = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "NIFTYINDEX": "^NSEI",
    "NSEI": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "BANKNIFTY50": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
    "NSEBANK": "^NSEBANK",
    "BNF": "^NSEBANK",
}

#: Default large-cap liquidity universe (NSE).
DEFAULT_EQUITIES: Final[tuple[Instrument, ...]] = tuple(
    Instrument(symbol=f"{nse}.NS", name=nse, kind="equity", nse_symbol=nse)
    for nse in (
        "RELIANCE",
        "HDFCBANK",
        "ICICIBANK",
        "TCS",
        "INFY",
        "SBIN",
        "LT",
        "AXISBANK",
        "BHARTIARTL",
        "MARUTI",
    )
)


def _normalise_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def equity_instrument(nse_symbol: str) -> Instrument:
    """Build a yfinance equity instrument from an NSE trading symbol."""
    raw = nse_symbol.strip().upper()
    if raw.endswith(".NS"):
        raw = raw[:-3]
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", raw):
        raise UnknownInstrument(f"'{nse_symbol}' is not a valid NSE symbol")
    return Instrument(symbol=f"{raw}.NS", name=raw, kind="equity", nse_symbol=raw)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def _raw(key: str) -> str:
    return (os.getenv(key) or "").strip()


def _env_str(key: str, default: str = "") -> str:
    return _raw(key) or default


def _env_int(key: str, default: int) -> int:
    value = _raw(key)
    if not value:
        return default
    try:
        return int(float(value))
    except ValueError:
        LOGGER.warning("Invalid integer for %s=%r; using %s", key, value, default)
        return default


def _env_float(key: str, default: float) -> float:
    value = _raw(key)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        LOGGER.warning("Invalid float for %s=%r; using %s", key, value, default)
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    value = _raw(key).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _env_int_tuple(key: str, *fallback_keys: str) -> tuple[int, ...]:
    raw = next((_raw(k) for k in (key, *fallback_keys) if _raw(k)), "")
    if not raw:
        return ()
    parsed: list[int] = []
    for part in re.split(r"[,\s;]+", raw):
        if not part:
            continue
        try:
            parsed.append(int(float(part)))
        except ValueError:
            LOGGER.warning("Ignoring non-numeric chat id %r in %s", part, key)
    return tuple(dict.fromkeys(parsed))


def _env_str_tuple(key: str) -> tuple[str, ...]:
    raw = _raw(key)
    if not raw:
        return ()
    return tuple(
        dict.fromkeys(part.strip() for part in re.split(r"[,\s;]+", raw) if part.strip())
    )


def _env_time(key: str, default: time) -> time:
    raw = _raw(key)
    if not raw:
        return default
    for fmt in ("%H:%M", "%H%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    LOGGER.warning("Invalid time for %s=%r; using %s", key, raw, default.isoformat())
    return default


def _env_dates(key: str) -> frozenset[date]:
    holidays: set[date] = set()
    for token in re.split(r"[,\s;]+", _raw(key)):
        if not token:
            continue
        try:
            holidays.add(datetime.strptime(token, "%Y-%m-%d").date())
        except ValueError:
            LOGGER.warning("Ignoring malformed holiday %r in %s", token, key)
    return frozenset(holidays)


def _watchlist_from_env() -> tuple[Instrument, ...]:
    requested = _env_str_tuple("WATCHLIST")
    equities: list[Instrument] = []
    for symbol in requested:
        try:
            if _normalise_token(symbol) in _ALIASES:
                continue  # indices are always appended below
            equities.append(equity_instrument(symbol))
        except UnknownInstrument as exc:
            LOGGER.warning("Skipping watchlist entry: %s", exc)
    if not equities:
        equities = list(DEFAULT_EQUITIES)
    # Indices first, then equities, de-duplicated by provider symbol.
    ordered: dict[str, Instrument] = {i.symbol: i for i in (NIFTY_50, BANK_NIFTY)}
    for inst in equities:
        ordered.setdefault(inst.symbol, inst)
    return tuple(ordered.values())


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime configuration."""

    # credentials
    telegram_bot_token: str = ""
    telegram_chat_ids: tuple[int, ...] = ()
    gmail_user: str = ""
    gmail_app_password: str = ""
    email_recipients: tuple[str, ...] = ()
    enable_email: bool = True

    # intelligence
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_timeout: float = 45.0
    min_risk_reward: float = 2.0

    # universe / engine
    watchlist: tuple[Instrument, ...] = DEFAULT_EQUITIES
    scan_interval_minutes: int = 15
    scan_timeframe: str = "15m"
    confirm_timeframe: str = "5m"
    scan_workers: int = 4
    snapshot_ttl_seconds: int = 60
    enable_nse_live: bool = True
    nse_timeout_seconds: float = 6.0

    # detectors
    volume_sma_period: int = 10
    volume_surge_multiple: float = 1.5
    ob_displacement_atr: float = 1.4
    ob_lookback_bars: int = 160
    ob_retest_tolerance_atr: float = 0.35
    swing_fractal_bars: int = 2

    # alerting
    alert_min_severity: int = 3
    email_min_severity: int = 4
    alert_cooldown_minutes: int = 45

    # desktop experience
    desktop_toast: bool = True
    voice_alerts: bool = False
    auto_arm: bool = True
    update_repo: str = ""
    setup_done: bool = False

    # schedule
    eod_wrap_time: time = time(15, 40)
    opening_brief_time: time = time(9, 16)
    market_holidays: frozenset[date] = frozenset()

    # output
    report_dir: Path = BASE_DIR / "reports"
    log_dir: Path = BASE_DIR / "logs"
    log_level: str = "INFO"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5

    # ---------------------------------------------------------------- load
    @classmethod
    def load(cls) -> "Settings":
        settings = cls(
            telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_ids=_env_int_tuple("TELEGRAM_CHAT_IDS", "TELEGRAM_CHAT_ID"),
            gmail_user=_env_str("GMAIL_USER"),
            gmail_app_password=_env_str("GMAIL_APP_PASSWORD").replace(" ", ""),
            email_recipients=_env_str_tuple("EMAIL_RECIPIENTS"),
            enable_email=_env_bool("ENABLE_EMAIL", True),
            groq_api_key=_env_str("GROQ_API_KEY"),
            groq_model=_env_str("GROQ_MODEL", "llama-3.3-70b-versatile"),
            groq_timeout=_env_float("GROQ_TIMEOUT_SECONDS", 45.0),
            min_risk_reward=_env_float("MIN_RISK_REWARD", 2.0),
            watchlist=_watchlist_from_env(),
            scan_interval_minutes=max(1, _env_int("SCAN_INTERVAL_MINUTES", 15)),
            scan_timeframe=_env_str("SCAN_TIMEFRAME", "15m"),
            confirm_timeframe=_env_str("CONFIRM_TIMEFRAME", "5m"),
            scan_workers=max(1, _env_int("SCAN_WORKERS", 4)),
            snapshot_ttl_seconds=max(0, _env_int("SNAPSHOT_TTL_SECONDS", 60)),
            enable_nse_live=_env_bool("ENABLE_NSE_LIVE", True),
            nse_timeout_seconds=_env_float("NSE_TIMEOUT_SECONDS", 6.0),
            volume_sma_period=max(3, _env_int("VOLUME_SMA_PERIOD", 10)),
            volume_surge_multiple=_env_float("VOLUME_SURGE_MULTIPLE", 1.5),
            ob_displacement_atr=_env_float("OB_DISPLACEMENT_ATR", 1.4),
            ob_lookback_bars=max(40, _env_int("OB_LOOKBACK_BARS", 160)),
            ob_retest_tolerance_atr=_env_float("OB_RETEST_TOLERANCE_ATR", 0.35),
            swing_fractal_bars=max(1, _env_int("SWING_FRACTAL_BARS", 2)),
            alert_min_severity=min(5, max(1, _env_int("ALERT_MIN_SEVERITY", 3))),
            email_min_severity=min(5, max(1, _env_int("EMAIL_MIN_SEVERITY", 4))),
            alert_cooldown_minutes=max(0, _env_int("ALERT_COOLDOWN_MINUTES", 45)),
            desktop_toast=_env_bool("DESKTOP_TOAST", True),
            voice_alerts=_env_bool("VOICE_ALERTS", False),
            auto_arm=_env_bool("AUTO_ARM", True),
            update_repo=_env_str("UPDATE_REPO"),
            setup_done=_env_bool("SETUP_DONE", False),
            eod_wrap_time=_env_time("EOD_WRAP_TIME", time(15, 40)),
            opening_brief_time=_env_time("OPENING_BRIEF_TIME", time(9, 16)),
            market_holidays=_env_dates("MARKET_HOLIDAYS"),
            report_dir=Path(_env_str("REPORT_DIR", "reports")),
            log_dir=Path(_env_str("LOG_DIR", "logs")),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        )
        settings._ensure_directories()
        settings._log_soft_warnings()
        return settings

    # ------------------------------------------------------------ helpers
    def _ensure_directories(self) -> None:
        for directory in (self.report_dir, self.log_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem edge case
                LOGGER.error("Cannot create directory %s: %s", directory, exc)

    def _log_soft_warnings(self) -> None:
        if not self.groq_api_key:
            LOGGER.warning(
                "GROQ_API_KEY missing - AI verdicts fall back to deterministic ATR plans."
            )
        if self.enable_email and not (self.gmail_user and self.gmail_app_password):
            LOGGER.warning("Gmail credentials missing - email alerts are disabled.")
        if self.enable_email and not self.email_recipients and self.gmail_user:
            LOGGER.info("EMAIL_RECIPIENTS empty - defaulting to GMAIL_USER.")

    # -------------------------------------------------------------- email
    @property
    def resolved_recipients(self) -> tuple[str, ...]:
        recipients = self.email_recipients or ((self.gmail_user,) if self.gmail_user else ())
        return recipients

    @property
    def email_ready(self) -> bool:
        return bool(
            self.enable_email
            and self.gmail_user
            and self.gmail_app_password
            and self.resolved_recipients
        )

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_ids)

    # ----------------------------------------------------------- sessions
    @staticmethod
    def _as_ist(now: datetime | None) -> datetime:
        current = now or datetime.now(IST)
        if current.tzinfo is None:
            return current.replace(tzinfo=IST)
        return current.astimezone(IST)

    def is_trading_day(self, day: date | None = None) -> bool:
        target = day or datetime.now(IST).date()
        return target.weekday() < 5 and target not in self.market_holidays

    def is_market_open(self, now: datetime | None = None) -> bool:
        """True only inside the continuous NSE session (09:15-15:30 IST)."""
        current = self._as_ist(now)
        if not self.is_trading_day(current.date()):
            return False
        return MARKET_OPEN_TIME <= current.time() <= MARKET_CLOSE_TIME

    def market_phase(self, now: datetime | None = None) -> str:
        """Coarse session label used by ``/status``."""
        current = self._as_ist(now)
        if not self.is_trading_day(current.date()):
            return "HOLIDAY/WEEKEND"
        clock = current.time()
        if clock < PRE_OPEN_TIME:
            return "PRE-MARKET"
        if clock < MARKET_OPEN_TIME:
            return "PRE-OPEN"
        if clock <= MARKET_CLOSE_TIME:
            return "OPEN"
        return "CLOSED"

    # ----------------------------------------------------------- universe
    def instrument(self, symbol: str) -> Instrument | None:
        token = _normalise_token(symbol)
        for inst in self.watchlist:
            if token in {
                _normalise_token(inst.symbol),
                _normalise_token(inst.name),
                _normalise_token(inst.nse_symbol or ""),
            }:
                return inst
        return None

    def resolve_instrument(self, query: str) -> Instrument:
        """Resolve user input (``nifty``, ``BANKNIFTY``, ``reliance``, ``TCS.NS``).

        Raises :class:`UnknownInstrument` for anything that is neither in the
        watchlist nor a syntactically valid NSE ticker.
        """
        text = (query or "").strip()
        if not text:
            raise UnknownInstrument("No symbol supplied")

        known = self.instrument(text)
        if known:
            return known

        token = _normalise_token(text)
        alias = _ALIASES.get(token)
        if alias:
            return _INDEX_INSTRUMENTS[alias]

        try:
            return equity_instrument(text)
        except UnknownInstrument as exc:
            raise UnknownInstrument(f"Unknown symbol: {query!r}") from exc

    # ---------------------------------------------------------- validation
    def validate(self) -> list[str]:
        """Return blocking configuration problems (empty when runnable)."""
        problems: list[str] = []
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set")
        if not self.telegram_chat_ids:
            problems.append("TELEGRAM_CHAT_IDS is empty - no alert destination")
        if not self.watchlist:
            problems.append("WATCHLIST resolved to zero instruments")
        return problems

    def require_runnable(self) -> None:
        problems = self.validate()
        if problems:
            raise ConfigError("; ".join(problems))

    def public_summary(self) -> dict[str, object]:
        """Secret-free snapshot for logging and ``/status``."""
        return {
            "telegram_ready": self.telegram_ready,
            "email_ready": self.email_ready,
            "groq_ready": bool(self.groq_api_key),
            "groq_model": self.groq_model,
            "watchlist": [inst.symbol for inst in self.watchlist],
            "scan_interval_minutes": self.scan_interval_minutes,
            "scan_timeframe": self.scan_timeframe,
            "confirm_timeframe": self.confirm_timeframe,
            "min_risk_reward": self.min_risk_reward,
            "alert_min_severity": self.alert_min_severity,
            "desktop_toast": self.desktop_toast,
            "voice_alerts": self.voice_alerts,
            "auto_arm": self.auto_arm,
            "update_repo": self.update_repo,
            "setup_done": self.setup_done,
            "market_holidays": len(self.market_holidays),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings.load()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class _VendorNoiseFilter(logging.Filter):
    """Drop known deprecation chatter from the unofficial NSE scraper."""

    _needles = ("/api/quote-equity is retired",)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(needle in message for needle in self._needles)


def configure_logging(settings: Settings | None = None) -> None:
    """Console + rotating-file logging, idempotent."""
    cfg = settings or get_settings()
    level = getattr(logging, cfg.log_level, logging.INFO)
    root = logging.getLogger()
    if getattr(root, "_tradexvm_configured", False):
        root.setLevel(level)
        return

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    noise_filter = _VendorNoiseFilter()
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(noise_filter)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            cfg.log_dir / "tradexvm.log",
            maxBytes=cfg.log_max_bytes,
            backupCount=cfg.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(noise_filter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover
        LOGGER.warning("File logging disabled: %s", exc)

    root.setLevel(level)
    # yfinance/nsepython are noisy on transient upstream errors; we retry and
    # report failures ourselves.
    for noisy in ("yfinance", "peewee", "urllib3", "nsepython"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    root._tradexvm_configured = True  # type: ignore[attr-defined]


if __name__ == "__main__":  # pragma: no cover - manual config check
    configure_logging()
    s = get_settings()
    import json

    print(json.dumps(s.public_summary(), indent=2, default=str))
    print("market phase:", s.market_phase())
    for probe in ("nifty", "banknifty", "reliance", "TCS.NS", "M&M"):
        print(f"  {probe:12} -> {s.resolve_instrument(probe)}")
