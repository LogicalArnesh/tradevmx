"""TradeXVm :: live market scanner.

Responsible for

* pulling intraday OHLCV (yfinance) + a live spot overlay (nsepython),
* session-anchored VWAP,
* abnormal volume detection (x-period SMA),
* Open = High / Open = Low trend-day detection,
* basic Smart-Money-Concept order blocks and retest detection,
* market structure (HH/HL vs LH/LL), pivot and swing S/R levels,
* packaging everything into an immutable :class:`MarketSnapshot`.

The module is deliberately synchronous and side-effect free (apart from
logging); ``bot.py`` and the AI layer consume its output.  Every public entry
point degrades gracefully: one broken symbol never aborts a whole scan.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time as clock
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
import yfinance as yf

try:  # NSE endpoints are best-effort: yfinance is the primary feed
    import nsepython
except Exception:  # pragma: no cover - optional dependency
    nsepython = None  # type: ignore[assignment]

from config import (
    IST,
    Instrument,
    Settings,
    configure_logging,
    get_settings,
)

LOGGER = logging.getLogger(__name__)

SUPPORTED_INTERVALS: frozenset[str] = frozenset(
    {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}
)
VWAP_CROSS_LOOKBACK = 3
MAX_STALE_MINUTES = 25
_FRACTAL = 2


class DataUnavailable(RuntimeError):
    """Raised when a provider cannot supply usable OHLCV for a symbol."""


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Signal:
    """A single detected setup."""

    code: str
    label: str
    direction: str  # "bullish" | "bearish" | "neutral"
    severity: int  # 1 (noise) .. 5 (high conviction)
    detail: str
    timeframe: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "direction": self.direction,
            "severity": self.severity,
            "detail": self.detail,
            "timeframe": self.timeframe,
        }


@dataclass(frozen=True, slots=True)
class Level:
    """A horizontal support/resistance level."""

    price: float
    role: str  # "support" | "resistance"
    source: str
    strength: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": round(self.price, 2),
            "role": self.role,
            "source": self.source,
            "strength": self.strength,
        }


@dataclass(frozen=True, slots=True)
class OrderBlock:
    """A simplified SMC order block: the last opposing candle before displacement."""

    direction: str  # "bullish" | "bearish"
    top: float
    bottom: float
    origin_at: datetime
    confirmed_at: datetime
    state: str = "fresh"  # "fresh" | "tested" | "invalidated"

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "top": round(self.top, 2),
            "bottom": round(self.bottom, 2),
            "mid": round(self.mid, 2),
            "origin_at": self.origin_at.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Everything the desk knows about one instrument on one timeframe."""

    symbol: str
    display_name: str
    timeframe: str
    as_of: datetime

    cmp: float
    prev_close: float
    change_pct: float
    day_open: float
    day_high: float
    day_low: float
    vwap: float
    vwap_source: str
    atr: float

    volume: float
    volume_sma: float
    volume_ratio: float

    bias: str
    bias_reason: str
    structure: str

    signals: tuple[Signal, ...] = ()
    supports: tuple[Level, ...] = ()
    resistances: tuple[Level, ...] = ()
    order_blocks: tuple[OrderBlock, ...] = ()

    pivot: float | None = None
    is_index: bool = False
    is_stale: bool = False
    ltp_source: str = "yfinance"
    confirm_timeframe: str | None = None
    confirm_bias: str | None = None
    warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------- queries
    def actionable_signals(self, min_severity: int = 3) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.severity >= min_severity)

    @property
    def max_severity(self) -> int:
        return max((s.severity for s in self.signals), default=0)

    @property
    def vwap_distance_pct(self) -> float:
        if not self.vwap:
            return 0.0
        return (self.cmp - self.vwap) / self.vwap * 100.0

    def signal_codes(self) -> tuple[str, ...]:
        return tuple(sorted({s.code for s in self.signals}))

    def to_prompt_payload(self) -> dict[str, Any]:
        """JSON-safe payload handed to the LLM (and logged for audit)."""
        return {
            "symbol": self.display_name,
            "ticker": self.symbol,
            "timeframe": self.timeframe,
            "as_of_ist": self.as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "cmp": round(self.cmp, 2),
            "prev_close": round(self.prev_close, 2),
            "change_pct": round(self.change_pct, 2),
            "day_open": round(self.day_open, 2),
            "day_high": round(self.day_high, 2),
            "day_low": round(self.day_low, 2),
            "vwap": round(self.vwap, 2),
            "vwap_source": self.vwap_source,
            "vwap_distance_pct": round(self.vwap_distance_pct, 2),
            "atr_14": round(self.atr, 2),
            "volume": int(self.volume),
            "volume_sma": int(self.volume_sma),
            "volume_ratio": round(self.volume_ratio, 2) if math.isfinite(self.volume_ratio) else None,
            "bias": self.bias,
            "bias_reason": self.bias_reason,
            "structure": self.structure,
            "signals": [s.as_dict() for s in self.signals],
            "supports": [lv.as_dict() for lv in self.supports],
            "resistances": [lv.as_dict() for lv in self.resistances],
            "order_blocks": [ob.as_dict() for ob in self.order_blocks],
            "data_quality": {
                "is_stale": self.is_stale,
                "ltp_source": self.ltp_source,
                "warnings": list(self.warnings),
            },
        }


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Result of one full watchlist sweep."""

    created_at: datetime
    snapshots: tuple[MarketSnapshot, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)

    def actionable(self, min_severity: int = 3) -> tuple[MarketSnapshot, ...]:
        return tuple(
            s for s in self.snapshots if s.max_severity >= min_severity
        )

    @property
    def scanned(self) -> int:
        return len(self.snapshots)

    def by_symbol(self, symbol: str) -> MarketSnapshot | None:
        return next((s for s in self.snapshots if s.symbol == symbol), None)


# ---------------------------------------------------------------------------
# Pure indicator maths
# ---------------------------------------------------------------------------
def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def prepare_frame(raw: pd.DataFrame | None) -> pd.DataFrame:
    """Normalise provider output into a clean, IST-indexed OHLCV frame."""
    if raw is None or not isinstance(raw, pd.DataFrame) or raw.empty:
        raise DataUnavailable("provider returned no rows")

    df = raw.copy()
    df.columns = [str(col).strip().title() for col in df.columns]
    missing = {"Open", "High", "Low", "Close"} - set(df.columns)
    if missing:
        raise DataUnavailable(f"missing OHLC columns: {sorted(missing)}")
    if "Volume" not in df.columns:
        df["Volume"] = 0.0

    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataUnavailable("non-datetime index")
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    df = df[["Open", "High", "Low", "Close", "Volume"]]
    for column in df.columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df["Volume"] = df["Volume"].fillna(0.0).clip(lower=0.0)
    df = df[df["High"] >= df["Low"]]
    df = df[~df.index.duplicated(keep="last")].sort_index()

    if df.empty:
        raise DataUnavailable("frame empty after cleaning")
    if len(df) < 5:
        raise DataUnavailable(f"insufficient bars ({len(df)})")
    return df


def compute_vwap(df: pd.DataFrame) -> tuple[pd.Series, str]:
    """Session-anchored VWAP.

    Indices publish no volume on Yahoo, so a session-anchored cumulative
    typical-price mean is used and explicitly labelled as a proxy.
    """
    sessions = pd.Series(df.index.normalize(), index=df.index)
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    volume = df["Volume"].astype(float)

    if float(volume.sum()) <= 0:
        proxy = typical.groupby(sessions).transform(lambda s: s.expanding().mean())
        return proxy.ffill().fillna(typical), "session typical-price proxy (no volume)"

    cum_volume = volume.groupby(sessions).cumsum()
    cum_pv = (typical * volume).groupby(sessions).cumsum()
    vwap = cum_pv.div(cum_volume.replace(0.0, np.nan))
    vwap = vwap.groupby(sessions).ffill().fillna(typical)
    return vwap, "volume weighted (session anchored)"


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    true_range = true_range.fillna(high - low)
    atr = true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return atr.fillna(true_range.expanding().mean())


def volume_profile(df: pd.DataFrame, period: int) -> dict[str, float]:
    """Ratio of the latest bar volume to the mean of the *preceding* bars."""
    volume = df["Volume"].astype(float)
    min_periods = max(3, period // 2)
    sma = volume.rolling(period, min_periods=min_periods).mean().shift(1)
    last = float(volume.iloc[-1])
    baseline = float(sma.iloc[-1]) if pd.notna(sma.iloc[-1]) else float("nan")
    ratio = last / baseline if baseline and math.isfinite(baseline) and baseline > 0 else float("nan")
    return {"last": last, "sma": baseline, "ratio": ratio}


def swing_points(df: pd.DataFrame, fractal: int = _FRACTAL) -> tuple[list[float], list[float]]:
    """Fractal swing highs/lows, most recent last."""
    if len(df) < (2 * fractal + 1):
        return [], []
    window = 2 * fractal + 1
    highs = df["High"].where(df["High"] == df["High"].rolling(window, center=True).max())
    lows = df["Low"].where(df["Low"] == df["Low"].rolling(window, center=True).min())
    swing_highs = [float(v) for v in highs.dropna().tail(12).tolist()]
    swing_lows = [float(v) for v in lows.dropna().tail(12).tolist()]
    return swing_highs, swing_lows


def classify_structure(
    df: pd.DataFrame, fractal: int = _FRACTAL
) -> tuple[str, str]:
    """Classify market structure from the last two swing highs and lows."""
    swing_highs, swing_lows = swing_points(df, fractal)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "undetermined", "not enough completed swings"

    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1] > swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1] < swing_lows[-2]

    detail = (
        f"swing highs {swing_highs[-2]:.2f}->{swing_highs[-1]:.2f}, "
        f"swing lows {swing_lows[-2]:.2f}->{swing_lows[-1]:.2f}"
    )
    if hh and hl:
        return "bullish", f"higher highs and higher lows ({detail})"
    if lh and ll:
        return "bearish", f"lower highs and lower lows ({detail})"
    if hh and ll:
        return "expanding", f"expanding range ({detail})"
    if lh and hl:
        return "contracting", f"contracting range ({detail})"
    return "ranging", f"mixed swings ({detail})"


def pivot_levels(high: float, low: float, close: float) -> dict[str, float]:
    """Classic floor-trader pivots from the previous completed session."""
    pivot = (high + low + close) / 3.0
    span = high - low
    return {
        "P": pivot,
        "R1": 2 * pivot - low,
        "R2": pivot + span,
        "R3": high + 2 * (pivot - low),
        "S1": 2 * pivot - high,
        "S2": pivot - span,
        "S3": low - 2 * (high - pivot),
    }


def detect_vwap_cross(
    df: pd.DataFrame, vwap: pd.Series, timeframe: str, lookback: int = VWAP_CROSS_LOOKBACK
) -> Signal | None:
    """Most recent close crossing VWAP within ``lookback`` bars."""
    if len(df) < 3 or len(vwap) != len(df):
        return None

    lookback = max(1, min(lookback, len(df) - 1))
    for offset in range(1, lookback + 1):
        i = len(df) - offset
        if i < 1:
            continue
        prev_delta = float(df["Close"].iloc[i - 1]) - float(vwap.iloc[i - 1])
        curr_delta = float(df["Close"].iloc[i]) - float(vwap.iloc[i])
        if not all(math.isfinite(v) for v in (prev_delta, curr_delta, float(vwap.iloc[i]))):
            continue
        if prev_delta <= 0 < curr_delta or prev_delta >= 0 > curr_delta:
            bullish = curr_delta > 0
            stamp = df.index[i].strftime("%H:%M")
            gap = abs(curr_delta) / float(vwap.iloc[i]) * 100.0
            return Signal(
                code="VWAP_CROSS_UP" if bullish else "VWAP_CROSS_DOWN",
                label="VWAP reclaim" if bullish else "VWAP rejection",
                direction="bullish" if bullish else "bearish",
                severity=3,
                detail=(
                    f"{timeframe} close crossed {'above' if bullish else 'below'} VWAP "
                    f"{float(vwap.iloc[i]):.2f} at {stamp} IST "
                    f"({gap:.2f}% through the level)"
                ),
                timeframe=timeframe,
            )
    return None


def detect_volume_surge(
    df: pd.DataFrame, period: int, multiple: float, timeframe: str
) -> tuple[Signal | None, dict[str, float]]:
    """Abnormal volume vs the trailing ``period``-bar SMA."""
    stats = volume_profile(df, period)
    ratio = stats["ratio"]
    if not math.isfinite(ratio) or ratio < multiple:
        return None, stats

    last, first = float(df["Close"].iloc[-1]), float(df["Open"].iloc[-1])
    direction = "bullish" if last >= first else "bearish"
    severity = 4 if ratio >= 2.0 else 3
    return (
        Signal(
            code="VOLUME_SURGE",
            label="Abnormal volume",
            direction=direction,
            severity=severity,
            detail=(
                f"{timeframe} volume {stats['last']:,.0f} vs {period}-bar SMA "
                f"{stats['sma']:,.0f} ({ratio:.2f}x) on a {'green' if direction == 'bullish' else 'red'} bar"
            ),
            timeframe=timeframe,
        ),
        stats,
    )


def detect_open_extremes(
    day_open: float, day_high: float, day_low: float, timeframe: str
) -> Signal | None:
    """Open = High (distribution) / Open = Low (accumulation)."""
    if day_open <= 0:
        return None
    tolerance = max(day_open * 0.0005, 0.05)
    if day_high - day_open <= tolerance:
        return Signal(
            code="OPEN_HIGH",
            label="Open = High",
            direction="bearish",
            severity=3,
            detail=(
                f"Session has not traded above the {day_open:.2f} open "
                f"(high {day_high:.2f}) - sellers in control, supply overhead"
            ),
            timeframe=timeframe,
        )
    if day_open - day_low <= tolerance:
        return Signal(
            code="OPEN_LOW",
            label="Open = Low",
            direction="bullish",
            severity=3,
            detail=(
                f"Session has not traded below the {day_open:.2f} open "
                f"(low {day_low:.2f}) - buyers absorbing every dip"
            ),
            timeframe=timeframe,
        )
    return None


def detect_order_blocks(
    df: pd.DataFrame, atr: pd.Series, settings: Settings
) -> tuple[OrderBlock, ...]:
    """Last opposing candle before a displacement leg, with lifecycle state."""
    if len(df) < 30:
        return ()

    lookback = min(settings.ob_lookback_bars, len(df) - 2)
    if lookback < 10:
        return ()

    body = (df["Close"] - df["Open"]).to_numpy()
    threshold = (settings.ob_displacement_atr * atr).to_numpy()
    closes = df["Close"].to_numpy()
    opens = df["Open"].to_numpy()
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()
    stamps = df.index

    found: list[OrderBlock] = []
    start = max(len(df) - lookback, 1)

    for i in range(start, len(df)):
        if not math.isfinite(float(threshold[i])):
            continue
        bullish_displacement = body[i] > threshold[i]
        bearish_displacement = -body[i] > threshold[i]
        if not (bullish_displacement or bearish_displacement):
            continue

        wanted = "down" if bullish_displacement else "up"
        origin = None
        for k in range(i - 1, max(i - 13, -1), -1):
            is_down = closes[k] < opens[k]
            if (wanted == "down" and is_down) or (wanted == "up" and not is_down):
                origin = k
                break
        if origin is None:
            continue

        top, bottom = float(highs[origin]), float(lows[origin])
        if top <= bottom:
            continue
        if bullish_displacement and closes[i] <= top:
            continue
        if bearish_displacement and closes[i] >= bottom:
            continue

        after = df.iloc[i + 1 :]
        state = "fresh"
        if not after.empty:
            if bullish_displacement:
                if float(after["Close"].min()) < bottom:
                    state = "invalidated"
                elif float(after["Low"].min()) <= top:
                    state = "tested"
            else:
                if float(after["Close"].max()) > top:
                    state = "invalidated"
                elif float(after["High"].max()) >= bottom:
                    state = "tested"

        found.append(
            OrderBlock(
                direction="bullish" if bullish_displacement else "bearish",
                top=top,
                bottom=bottom,
                origin_at=stamps[origin].to_pydatetime(),
                confirmed_at=stamps[i].to_pydatetime(),
                state=state,
            )
        )

    if not found:
        return ()

    # Keep the freshest non-overlapping zones per direction.
    kept: list[OrderBlock] = []
    counts = {"bullish": 0, "bearish": 0}
    for zone in sorted(found, key=lambda z: z.confirmed_at, reverse=True):
        if zone.state == "invalidated" or counts[zone.direction] >= 3:
            continue
        overlaps = any(
            other.direction == zone.direction
            and not (zone.bottom > other.top or zone.top < other.bottom)
            for other in kept
        )
        if overlaps:
            continue
        kept.append(zone)
        counts[zone.direction] += 1

    return tuple(sorted(kept, key=lambda z: z.confirmed_at))


def detect_ob_retests(
    zones: Sequence[OrderBlock],
    price: float,
    atr_value: float,
    tolerance_atr: float,
    timeframe: str,
    last_high: float,
    last_low: float,
    last_close: float,
) -> tuple[Signal, ...]:
    """Price trading back into a live order block, with rejection confirmation."""
    if not zones or price <= 0:
        return ()
    tolerance = max(atr_value * tolerance_atr, price * 0.0005)
    signals: list[Signal] = []

    for zone in zones:
        if zone.state == "invalidated":
            continue
        lower, upper = zone.bottom - tolerance, zone.top + tolerance
        if not (lower <= price <= upper):
            continue

        bullish = zone.direction == "bullish"
        rejected = (
            last_close > zone.top and last_low <= zone.top + tolerance
            if bullish
            else last_close < zone.bottom and last_high >= zone.bottom - tolerance
        )
        distance = abs(price - zone.mid) / price * 100.0
        signals.append(
            Signal(
                code="OB_RETEST_BULL" if bullish else "OB_RETEST_BEAR",
                label=("Bullish order block retest" if bullish else "Bearish order block retest")
                + (" (rejected)" if rejected else " (in progress)"),
                direction=zone.direction,
                severity=4 if rejected else 3,
                detail=(
                    f"{timeframe} price {price:.2f} inside {zone.direction} OB "
                    f"{zone.bottom:.2f}-{zone.top:.2f} ({zone.state}, {distance:.2f}% from mid)"
                    + (" with rejection close" if rejected else "")
                ),
                timeframe=timeframe,
            )
        )
        if len(signals) >= 2:
            break
    return tuple(signals)


def _dedupe_levels(levels: Iterable[Level], price: float, tolerance: float) -> list[Level]:
    ordered = sorted(levels, key=lambda lv: (-lv.strength, abs(lv.price - price)))
    kept: list[Level] = []
    for level in ordered:
        if abs(level.price - price) / price < 0.0005:
            continue
        if any(abs(level.price - other.price) / price <= tolerance for other in kept):
            continue
        kept.append(level)
    return kept


def build_levels(
    *,
    price: float,
    pivots: dict[str, float],
    swing_highs: Sequence[float],
    swing_lows: Sequence[float],
    day_high: float,
    day_low: float,
) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
    """Rank the most relevant supports and resistances around price."""
    if price <= 0:
        return (), ()

    candidates: list[Level] = []
    for name, value in pivots.items():
        if not math.isfinite(value) or value <= 0:
            continue
        role = "support" if value <= price else "resistance"
        candidates.append(
            Level(price=float(value), role=role, source=f"pivot {name}", strength=2 if name == "P" else 1)
        )
    for value in swing_highs:
        role = "support" if value <= price else "resistance"
        candidates.append(Level(price=float(value), role=role, source="swing high", strength=2))
    for value in swing_lows:
        role = "support" if value <= price else "resistance"
        candidates.append(Level(price=float(value), role=role, source="swing low", strength=2))
    if day_high > 0:
        candidates.append(
            Level(price=day_high, role="support" if day_high <= price else "resistance",
                  source="session high", strength=2)
        )
    if day_low > 0:
        candidates.append(
            Level(price=day_low, role="support" if day_low <= price else "resistance",
                  source="session low", strength=2)
        )

    kept = _dedupe_levels(candidates, price, tolerance=0.0015)
    supports = sorted((lv for lv in kept if lv.price < price), key=lambda lv: lv.price, reverse=True)[:3]
    resistances = sorted((lv for lv in kept if lv.price >= price), key=lambda lv: lv.price)[:3]
    return tuple(supports), tuple(resistances)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class MarketScanner:
    """Fetches, enriches and caches per-instrument market snapshots."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: dict[tuple[str, str], tuple[datetime, MarketSnapshot]] = {}
        self._cache_lock = threading.Lock()
        self._nse_pool = ThreadPoolExecutor(
            max_workers=max(2, self.settings.scan_workers),
            thread_name_prefix="nse-live",
        )

    # -------------------------------------------------------------- fetch
    def _history(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        """yfinance fetch with bounded retries (blocking, thread-safe)."""
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                raw = yf.Ticker(symbol).history(
                    period=period,
                    interval=interval,
                    auto_adjust=False,
                    prepost=False,
                    actions=False,
                    raise_errors=False,
                )
                return prepare_frame(raw)
            except DataUnavailable as exc:
                last_error = exc
                LOGGER.debug("%s %s/%s returned no usable data: %s", symbol, interval, period, exc)
            except Exception as exc:  # provider/network failure
                last_error = exc
                LOGGER.warning(
                    "History fetch failed for %s (%s/%s) attempt %s/3: %s",
                    symbol, interval, period, attempt, exc,
                )
            clock.sleep(1.5 * attempt)
        raise DataUnavailable(f"{symbol} {interval}: {last_error}")

    def _intraday(self, instrument: Instrument, interval: str) -> pd.DataFrame:
        if interval not in SUPPORTED_INTERVALS:
            LOGGER.warning("Unsupported interval %r, using 15m", interval)
            interval = "15m"
        return self._history(instrument.symbol, interval=interval, period="5d")

    def _daily(self, instrument: Instrument) -> pd.DataFrame | None:
        try:
            return self._history(instrument.symbol, interval="1d", period="3mo")
        except DataUnavailable as exc:
            LOGGER.warning("Daily context unavailable for %s: %s", instrument.symbol, exc)
            return None

    def _nse_quote(self, instrument: Instrument) -> dict[str, float] | None:
        """Best-effort live spot from NSE (real-time vs Yahoo's 15-min delay)."""
        if not self.settings.enable_nse_live or nsepython is None:
            return None

        def _call() -> Any:
            if instrument.is_index:
                target = instrument.nse_symbol or instrument.name
                return nsepython.nse_get_index_quote(target)  # type: ignore[union-attr]
            return nsepython.nse_eq(instrument.nse_symbol or instrument.name)  # type: ignore[union-attr]

        try:
            future: Future[Any] = self._nse_pool.submit(_call)
            payload = future.result(timeout=self.settings.nse_timeout_seconds)
        except FutureTimeout:
            LOGGER.debug("NSE quote timed out for %s", instrument.symbol)
            return None
        except Exception as exc:  # NSE blocks/rate-limits aggressively
            LOGGER.debug("NSE quote failed for %s: %s", instrument.symbol, exc)
            return None

        if not isinstance(payload, dict):
            return None
        source: dict[str, Any] = payload
        if isinstance(payload.get("priceInfo"), dict):
            source = payload["priceInfo"]
        elif isinstance(payload.get("data"), dict):
            source = payload["data"]

        price = _first_float(source, ("lastPrice", "ltp", "last", "close", "price", "lastTradedPrice"))
        change = _first_float(source, ("pChange", "changePercent", "percentChange", "perChange"))
        if price is None:
            return None
        return {"price": price, "change_pct": change} if change is not None else {"price": price}

    # ------------------------------------------------------------ build
    def snapshot(
        self, instrument: Instrument, timeframe: str | None = None, *, force: bool = False
    ) -> MarketSnapshot:
        interval = timeframe or self.settings.scan_timeframe
        key = (instrument.symbol, interval)
        now = datetime.now(IST)

        if not force:
            with self._cache_lock:
                cached = self._cache.get(key)
            if cached and (now - cached[0]).total_seconds() < self.settings.snapshot_ttl_seconds:
                return cached[1]

        snapshot = self._build(instrument, interval, now)
        with self._cache_lock:
            self._cache[key] = (now, snapshot)
        return snapshot

    def _build(self, instrument: Instrument, interval: str, now: datetime) -> MarketSnapshot:
        warnings: list[str] = []
        frame = self._intraday(instrument, interval)
        _, source = compute_vwap(frame)

        # --- current session slice (fall back to the last session if stale)
        session_date = frame.index.date.max()
        if session_date != now.date():
            warnings.append(
                f"No prints for {now.date()}; showing last session {session_date}."
            )
        session = frame[frame.index.date == session_date]
        if session.empty:  # defensive; frame always has rows
            session = frame.tail(40)

        day_open = float(session["Open"].iloc[0])
        day_high = float(session["High"].max())
        day_low = float(session["Low"].min())

        # --- daily context: previous close + previous session pivots
        daily = self._daily(instrument)
        prev_close = day_open
        prev_high, prev_low = day_high, day_low
        if daily is not None:
            completed = daily[daily.index.date < session_date]
            if completed.empty:
                completed = daily
            row = completed.iloc[-1]
            prev_close = float(row["Close"])
            prev_high, prev_low = float(row["High"]), float(row["Low"])
        else:
            warnings.append("Daily context unavailable; pivots use session extremes.")

        # --- indicators
        vwap_series, vwap_source = compute_vwap(frame)
        atr_series = compute_atr(frame)
        atr_value = float(atr_series.iloc[-1])
        if not math.isfinite(atr_value) or atr_value <= 0:
            atr_value = max(day_high - day_low, float(session["Close"].iloc[-1]) * 0.005)

        cmp_value = float(session["Close"].iloc[-1])
        ltp_source = "yfinance"
        live = self._nse_quote(instrument)
        if live:
            live_price = live["price"]
            # Guard against bad stock-page payloads (wrong series, stale cache).
            if 0 < abs(live_price - cmp_value) / cmp_value <= 0.08:
                cmp_value = live_price
                ltp_source = "nsepython"
                day_high = max(day_high, live_price)
                day_low = min(day_low, live_price)
            else:
                warnings.append(
                    f"NSE overlay {live_price:.2f} discarded (CMP {cmp_value:.2f})."
                )

        vwap_value = float(vwap_series.iloc[-1])
        if not math.isfinite(vwap_value) or vwap_value <= 0:
            vwap_value = cmp_value
            warnings.append("VWAP unavailable; defaulted to CMP.")

        vol_stats = volume_profile(session, self.settings.volume_sma_period)

        # --- detectors (primary timeframe)
        signals: list[Signal] = []
        cross = detect_vwap_cross(frame, vwap_series, interval)
        if cross:
            signals.append(cross)

        volume_signal, _ = detect_volume_surge(
            session, self.settings.volume_sma_period, self.settings.volume_surge_multiple, interval
        )
        if volume_signal:
            signals.append(volume_signal)

        open_signal = detect_open_extremes(day_open, day_high, day_low, interval)
        if open_signal:
            signals.append(open_signal)

        order_blocks = detect_order_blocks(frame, atr_series, self.settings)
        retests = detect_ob_retests(
            order_blocks,
            price=cmp_value,
            atr_value=atr_value,
            tolerance_atr=self.settings.ob_retest_tolerance_atr,
            timeframe=interval,
            last_high=float(session["High"].iloc[-1]),
            last_low=float(session["Low"].iloc[-1]),
            last_close=float(session["Close"].iloc[-1]),
        )
        signals.extend(retests)

        # --- confirmation timeframe (only if different)
        confirm_bias: str | None = None
        confirm_interval = self.settings.confirm_timeframe
        if confirm_interval and confirm_interval != interval:
            try:
                confirm_frame = self._intraday(instrument, confirm_interval)
                confirm_vwap, _ = compute_vwap(confirm_frame)
                confirm_session = confirm_frame[confirm_frame.index.date == confirm_frame.index.date.max()]
                confirm_atr = compute_atr(confirm_frame)
                confirm_signals: list[Signal] = []
                confirm_cross = detect_vwap_cross(confirm_frame, confirm_vwap, confirm_interval)
                if confirm_cross:
                    confirm_signals.append(confirm_cross)
                confirm_volume, _ = detect_volume_surge(
                    confirm_session,
                    self.settings.volume_sma_period,
                    self.settings.volume_surge_multiple,
                    confirm_interval,
                )
                if confirm_volume:
                    confirm_signals.append(confirm_volume)
                confirm_conf = detect_ob_retests(
                    detect_order_blocks(confirm_frame, confirm_atr, self.settings),
                    price=cmp_value,
                    atr_value=float(confirm_atr.iloc[-1]),
                    tolerance_atr=self.settings.ob_retest_tolerance_atr,
                    timeframe=confirm_interval,
                    last_high=float(confirm_session["High"].iloc[-1]),
                    last_low=float(confirm_session["Low"].iloc[-1]),
                    last_close=float(confirm_session["Close"].iloc[-1]),
                )
                confirm_signals.extend(confirm_conf)
                if confirm_signals:
                    signals.extend(confirm_signals)
                    confirm_bias = "bullish" if sum(
                        (1 if s.direction == "bullish" else -1 if s.direction == "bearish" else 0) * s.severity
                        for s in confirm_signals
                    ) > 0 else "bearish"
            except DataUnavailable as exc:
                warnings.append(f"{confirm_interval} confirmation unavailable: {exc}")

        # --- structure + bias
        structure, structure_reason = classify_structure(
            frame, max(1, self.settings.swing_fractal_bars)
        )
        # Volume is direction-agnostic confirmation: a green bar on abnormal
        # volume must not by itself turn a fade into a bullish bias.
        signal_score = sum(
            (1 if s.direction == "bullish" else -1 if s.direction == "bearish" else 0)
            * (0.5 + 0.5 * s.severity)
            for s in signals
            if s.code != "VOLUME_SURGE"
        )
        vwap_score = 1.0 if cmp_value > vwap_value else -1.0 if cmp_value < vwap_value else 0.0
        structure_score = {"bullish": 2.0, "bearish": -2.0}.get(structure, 0.0)
        total = signal_score + vwap_score + structure_score
        if total >= 2.5:
            bias = "BULLISH"
        elif total <= -2.5:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        bias_reason = (
            f"{structure_reason}; CMP "
            f"{'above' if vwap_score > 0 else 'below' if vwap_score < 0 else 'at'} VWAP; "
            f"signal score {total:+.1f}"
        )

        # --- levels
        pivots = pivot_levels(prev_high, prev_low, prev_close)
        swing_highs, swing_lows = swing_points(frame, max(1, self.settings.swing_fractal_bars))
        supports, resistances = build_levels(
            price=cmp_value,
            pivots=pivots,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            day_high=day_high,
            day_low=day_low,
        )

        # --- staleness
        last_bar_minutes = (now - frame.index[-1].to_pydatetime()).total_seconds() / 60.0
        is_stale = session_date != now.date()
        if self.settings.is_market_open(now) and last_bar_minutes > MAX_STALE_MINUTES:
            is_stale = True
            warnings.append(f"Last candle is {last_bar_minutes:.0f} minutes old.")

        change_pct = ((cmp_value - prev_close) / prev_close * 100.0) if prev_close else 0.0

        return MarketSnapshot(
            symbol=instrument.symbol,
            display_name=instrument.name,
            timeframe=interval,
            as_of=now,
            cmp=round(cmp_value, 2),
            prev_close=round(prev_close, 2),
            change_pct=round(change_pct, 2),
            day_open=round(day_open, 2),
            day_high=round(day_high, 2),
            day_low=round(day_low, 2),
            vwap=round(vwap_value, 2),
            vwap_source=vwap_source,
            atr=round(atr_value, 2),
            volume=float(vol_stats["last"]),
            volume_sma=float(vol_stats["sma"]) if math.isfinite(vol_stats["sma"]) else 0.0,
            volume_ratio=round(vol_stats["ratio"], 2) if math.isfinite(vol_stats["ratio"]) else float("nan"),
            bias=bias,
            bias_reason=bias_reason,
            structure=structure,
            signals=tuple(signals),
            supports=supports,
            resistances=resistances,
            order_blocks=order_blocks,
            pivot=round(pivots["P"], 2),
            is_index=instrument.is_index,
            is_stale=is_stale,
            ltp_source=ltp_source,
            confirm_timeframe=confirm_interval if confirm_bias else None,
            confirm_bias=confirm_bias,
            warnings=tuple(warnings),
        )

    # --------------------------------------------------------------- scan
    def scan(
        self,
        instruments: Sequence[Instrument] | None = None,
        *,
        force: bool = True,
        timeframe: str | None = None,
    ) -> ScanReport:
        """Sweep the watchlist concurrently.  Never raises for a single symbol."""
        targets = list(instruments or self.settings.watchlist)
        snapshots: dict[str, MarketSnapshot] = {}
        errors: dict[str, str] = {}
        started = clock.monotonic()

        with ThreadPoolExecutor(
            max_workers=min(self.settings.scan_workers, max(1, len(targets))),
            thread_name_prefix="scan",
        ) as pool:
            futures = {
                pool.submit(self.snapshot, inst, timeframe, force=force): inst
                for inst in targets
            }
            for future, instrument in futures.items():
                try:
                    snapshots[instrument.symbol] = future.result()
                except Exception as exc:  # noqa: BLE001 - isolation is the point
                    errors[instrument.symbol] = str(exc) or exc.__class__.__name__
                    LOGGER.exception("Snapshot failed for %s", instrument.symbol)

        ordered = tuple(snapshots[inst.symbol] for inst in targets if inst.symbol in snapshots)
        LOGGER.info(
            "Scan complete in %.2fs - %s/%s instruments, %s errors, %s actionable",
            clock.monotonic() - started,
            len(ordered),
            len(targets),
            len(errors),
            len([s for s in ordered if s.max_severity >= self.settings.alert_min_severity]),
        )
        return ScanReport(created_at=datetime.now(IST), snapshots=ordered, errors=errors)

    # ------------------------------------------------------------ misc
    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def close(self) -> None:
        self._nse_pool.shutdown(wait=False, cancel_futures=True)


def _first_float(payload: dict[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        if key not in payload:
            continue
        raw = payload[key]
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
        elif isinstance(raw, str):
            try:
                value = float(raw.replace(",", "").replace("%", "").strip())
            except ValueError:
                continue
        else:
            continue
        if math.isfinite(value):
            return value
    return None


# ---------------------------------------------------------------------------
# Manual use:  python market_scanner.py RELIANCE
# ---------------------------------------------------------------------------
def _main() -> int:  # pragma: no cover
    import sys

    configure_logging()
    settings = get_settings()
    query = sys.argv[1] if len(sys.argv) > 1 else "NIFTY"
    scanner = MarketScanner(settings)
    try:
        snapshot = scanner.snapshot(settings.resolve_instrument(query), force=True)
    finally:
        scanner.close()
    print(json.dumps(snapshot.to_prompt_payload(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
