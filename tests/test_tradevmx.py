"""TradeVMx test suite.

Offline and deterministic: synthetic OHLC series stand in for the network, so
the detectors, risk contract, PDF renderer and message formatting can be
verified without credentials or market hours.

Run:  ``.venv/bin/python -m pytest tests -q``
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ai_engine import GroqBrain, _extract_json
from config import IST, Instrument, Settings
from market_scanner import (
    MarketSnapshot,
    MarketScanner,
    Signal,
    build_levels,
    classify_structure,
    compute_vwap,
    detect_open_extremes,
    detect_order_blocks,
    detect_volume_surge,
    pivot_levels,
    prepare_frame,
    swing_points,
)
from report_engine import ReportEngine, inr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_frame(prices: list[tuple[float, float, float, float]], volumes: list[float] | None = None) -> pd.DataFrame:
    """Build a 15-minute frame starting at 09:15 IST on a Monday."""
    start = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    index = pd.DatetimeIndex([start + timedelta(minutes=15 * i) for i in range(len(prices))])
    frame = pd.DataFrame(prices, columns=["Open", "High", "Low", "Close"], index=index)
    frame["Volume"] = volumes if volumes is not None else [100_000.0] * len(prices)
    return frame


@pytest.fixture()
def settings() -> Settings:
    return Settings.load()


@pytest.fixture()
def bullish_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="RELIANCE.NS",
        display_name="RELIANCE",
        timeframe="15m",
        as_of=datetime(2026, 1, 5, 14, 30, tzinfo=IST),
        cmp=1432.10,
        prev_close=1414.60,
        change_pct=1.24,
        day_open=1417.00,
        day_high=1438.00,
        day_low=1414.90,
        vwap=1421.35,
        vwap_source="volume weighted (session anchored)",
        atr=14.20,
        volume=1_820_000,
        volume_sma=980_000,
        volume_ratio=1.86,
        bias="BULLISH",
        bias_reason="structure bullish (higher highs and higher lows)",
        structure="bullish",
        signals=(
            Signal(
                code="VWAP_CROSS_UP",
                label="VWAP reclaim",
                direction="bullish",
                severity=3,
                detail="15m close crossed above VWAP 1421.35 at 13:45 IST (0.42% through the level)",
                timeframe="15m",
            ),
            Signal(
                code="VOLUME_SURGE",
                label="Abnormal volume",
                direction="bullish",
                severity=4,
                detail="15m volume 1,820,000 vs 10-bar SMA 980,000 (1.86x) on a green bar",
                timeframe="15m",
            ),
        ),
        supports=(),
        resistances=(),
        order_blocks=(),
        pivot=1423.60,
        confirm_timeframe="5m",
        confirm_bias="bullish",
    )


# ---------------------------------------------------------------------------
# Fetch / indicator layer
# ---------------------------------------------------------------------------
def test_prepare_frame_normalises_and_validates() -> None:
    raw = make_frame([(100, 102, 99, 101)] * 6).rename(columns=str.lower)
    frame = prepare_frame(raw)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert str(frame.index.tz) == "Asia/Kolkata"


def test_prepare_frame_rejects_garbage() -> None:
    from market_scanner import DataUnavailable

    with pytest.raises(DataUnavailable):
        prepare_frame(pd.DataFrame())
    with pytest.raises(DataUnavailable):
        prepare_frame(pd.DataFrame({"Close": [1, 2, 3]}))


def test_vwap_is_volume_weighted_and_session_anchored() -> None:
    frame = make_frame(
        [(100, 102, 99, 101), (101, 103, 100, 102), (102, 104, 101, 103)],
        volumes=[100, 200, 300],
    )
    vwap, source = compute_vwap(frame)
    typical = [(102 + 99 + 101) / 3, (103 + 100 + 102) / 3, (104 + 101 + 103) / 3]
    expected = (typical[0] * 100 + typical[1] * 200 + typical[2] * 300) / 600
    assert vwap.iloc[-1] == pytest.approx(expected, rel=1e-9)
    assert "volume weighted" in source


def test_vwap_falls_back_to_proxy_without_volume() -> None:
    frame = make_frame([(100, 102, 99, 101), (101, 103, 100, 102)], volumes=[0, 0])
    vwap, source = compute_vwap(frame)
    assert math.isfinite(vwap.iloc[-1])
    assert "proxy" in source


def test_volume_surge_flags_only_abnormal_bars(settings: Settings) -> None:
    volumes = [100_000.0] * 12 + [400_000.0]
    frame = make_frame([(100, 101, 99, 100.5)] * len(volumes), volumes=volumes)
    signal, stats = detect_volume_surge(frame, 10, 1.5, "15m")
    assert signal is not None
    assert signal.severity == 4
    assert stats["ratio"] > 3.0

    calm_volumes = [100_000.0] * 13
    calm = make_frame([(100, 101, 99, 100.5)] * 13, volumes=calm_volumes)
    signal, _ = detect_volume_surge(calm, 10, 1.5, "15m")
    assert signal is None


def test_open_low_is_bullish_and_open_high_is_bearish() -> None:
    bullish = detect_open_extremes(day_open=100.0, day_high=104.0, day_low=100.0, timeframe="15m")
    assert bullish is not None and bullish.code == "OPEN_LOW" and bullish.direction == "bullish"

    bearish = detect_open_extremes(day_open=100.0, day_high=100.0, day_low=96.0, timeframe="15m")
    assert bearish is not None and bearish.code == "OPEN_HIGH" and bearish.direction == "bearish"

    inside = detect_open_extremes(day_open=100.0, day_high=104.0, day_low=96.0, timeframe="15m")
    assert inside is None


def test_pivot_levels_are_symmetric() -> None:
    levels = pivot_levels(high=110.0, low=90.0, close=100.0)
    assert levels["P"] == pytest.approx(100.0)
    assert levels["R1"] == pytest.approx(110.0)
    assert levels["S1"] == pytest.approx(90.0)
    assert levels["R2"] == pytest.approx(120.0)
    assert levels["S2"] == pytest.approx(80.0)


def test_structure_and_swings() -> None:
    closes = [100, 102, 101, 104, 103, 106, 105, 108, 107, 110, 109, 112]
    prices = [(c - 0.5, c + 0.5, c - 1.0, c) for c in closes]
    frame = make_frame(prices)
    highs, lows = swing_points(frame, fractal=1)
    assert highs and lows
    structure, reason = classify_structure(frame, fractal=1)
    assert structure in {"bullish", "bearish", "ranging", "expanding", "contracting", "undetermined"}
    assert reason


def test_order_block_detection_and_retest(settings: Settings) -> None:
    # 30 flat bars, a down candle, then displacement up through its high,
    # followed by a pullback into the block.
    bars: list[tuple[float, float, float, float]] = []
    price = 100.0
    for _ in range(30):
        bars.append((price, price + 0.4, price - 0.4, price))
        price += 0.01
    open_block = price + 0.2
    block_low, block_high = open_block - 1.2, open_block
    bars.append((open_block, open_block, block_low, block_low + 0.1))  # bearish origin candle
    for step in range(1, 6):  # displacement
        prev = bars[-1][3]
        bars.append((prev, prev + 1.6, prev - 0.2, prev + 1.5))
    # retest: wick back into the block, close back above it
    last = bars[-1][3]
    bars.append((last, last + 0.3, block_high - 0.4, block_high + 0.4))
    bars.append((block_high + 0.4, block_high + 1.0, block_high - 0.2, block_high + 0.8))

    frame = make_frame(bars)
    from market_scanner import compute_atr

    atr = compute_atr(frame)
    zones = detect_order_blocks(frame, atr, settings)
    assert any(z.direction == "bullish" for z in zones), zones

    from market_scanner import detect_ob_retests

    retests = detect_ob_retests(
        zones,
        price=block_high + 0.2,
        atr_value=float(atr.iloc[-1]),
        tolerance_atr=1.0,
        timeframe="15m",
        last_high=block_high + 1.0,
        last_low=block_high - 0.2,
        last_close=block_high + 0.8,
    )
    assert retests and retests[0].code == "OB_RETEST_BULL"


def test_levels_split_around_price() -> None:
    supports, resistances = build_levels(
        price=100.0,
        pivots={"P": 100.5, "R1": 104.0, "S1": 96.0, "R2": 108.0, "S2": 92.0},
        swing_highs=[103.0, 106.0],
        swing_lows=[97.0, 95.0],
        day_high=104.5,
        day_low=95.5,
    )
    assert supports and resistances
    assert all(lv.price < 100.0 for lv in supports)
    assert all(lv.price >= 100.0 for lv in resistances)
    assert len(supports) <= 3 and len(resistances) <= 3


def test_scanner_offline_build(settings: Settings, bullish_snapshot: MarketSnapshot) -> None:
    """The scanner must reject unusable provider output instead of crashing."""
    scanner = MarketScanner(settings)
    try:
        with pytest.raises(Exception):
            scanner.snapshot(Instrument(symbol="NOT_A_TICKER.NS", name="BAD"), force=True)
    finally:
        scanner.close()


# ---------------------------------------------------------------------------
# AI layer
# ---------------------------------------------------------------------------
def test_plan_enforces_minimum_reward_risk(settings: Settings, bullish_snapshot: MarketSnapshot) -> None:
    brain = GroqBrain(settings)
    raw = {
        "bias": "BULLISH",
        "verdict": "BUY",
        "entry_low": 1430.0,
        "entry_high": 1434.0,
        "stop_loss": 1424.0,
        "target_1": 1436.0,
        "target_2": 1442.0,
        "confidence": 0.7,
        "rationale": ["VWAP reclaim", "volume expansion"],
        "invalidation": "15m close below 1424",
    }
    plan = brain._parse(raw, bullish_snapshot)
    assert plan.verdict == "NO TRADE"
    assert any("Rejected" in note for note in plan.notes)


def test_plan_accepts_compliant_levels(settings: Settings, bullish_snapshot: MarketSnapshot) -> None:
    brain = GroqBrain(settings)
    raw = {
        "bias": "BULLISH",
        "verdict": "BUY",
        "entry_low": 1430.0,
        "entry_high": 1434.0,
        "stop_loss": 1422.0,
        "target_1": 1452.0,
        "target_2": 1466.0,
        "confidence": 0.74,
        "rationale": ["VWAP reclaim"],
        "invalidation": "15m close below 1420",
    }
    plan = brain._parse(raw, bullish_snapshot)
    assert plan.verdict == "BUY"
    assert plan.risk_reward >= 2.0
    assert plan.actionable


def test_fallback_plan_is_actionable_and_sane(settings: Settings, bullish_snapshot: MarketSnapshot) -> None:
    plan = GroqBrain(settings).fallback_plan(bullish_snapshot)
    assert plan.verdict == "BUY"
    assert plan.stop_loss < plan.entry_low < bullish_snapshot.cmp
    assert plan.target_1 > plan.entry_high
    assert plan.target_2 > plan.target_1
    assert plan.risk_reward >= 2.0
    assert plan.is_fallback


def test_neutral_fallback_refuses_to_trade(settings: Settings) -> None:
    neutral = MarketSnapshot(
        symbol="TCS.NS", display_name="TCS", timeframe="15m",
        as_of=datetime(2026, 1, 5, 12, 0, tzinfo=IST),
        cmp=3900.0, prev_close=3895.0, change_pct=0.13,
        day_open=3896.0, day_high=3910.0, day_low=3880.0,
        vwap=3899.0, vwap_source="proxy", atr=18.0,
        volume=0.0, volume_sma=0.0, volume_ratio=float("nan"),
        bias="NEUTRAL", bias_reason="structure ranging", structure="ranging",
    )
    plan = GroqBrain(settings).fallback_plan(neutral)
    assert plan.verdict == "NO TRADE"


def test_extract_json_tolerates_code_fences() -> None:
    assert _extract_json('```json\n{"verdict": "BUY"}\n```')["verdict"] == "BUY"
    assert _extract_json('noise {"verdict": "SELL"} tail')["verdict"] == "SELL"


# ---------------------------------------------------------------------------
# Reporting + alerts
# ---------------------------------------------------------------------------
def test_inr_uses_indian_grouping() -> None:
    assert inr(24318.4) == "24,318.40"
    assert inr(1234567.5) == "12,34,567.50"
    assert inr(-4250.0) == "-4,250.00"
    assert inr(None) == "-"


def test_pdf_report_renders(settings: Settings, bullish_snapshot: MarketSnapshot) -> None:
    engine = ReportEngine(settings)
    plan = GroqBrain(settings).fallback_plan(bullish_snapshot)
    artifact = engine.render(bullish_snapshot, plan)

    assert artifact.path.exists()
    data = artifact.read()
    assert data.startswith(b"%PDF")
    assert len(data) > 5_000
    assert b"/Type /Page" in data
    assert artifact.filename.startswith("TradeVMx_RELIANCE_")


def test_portfolio_pdf_renders(settings: Settings, bullish_snapshot: MarketSnapshot) -> None:
    engine = ReportEngine(settings)
    plan = GroqBrain(settings).fallback_plan(bullish_snapshot)
    artifact = engine.render_portfolio(
        [bullish_snapshot], {bullish_snapshot.symbol: plan}, title="Test Wrap"
    )
    assert artifact.read().startswith(b"%PDF")
    assert artifact.path.exists()


def test_alert_markup_is_escaped(bullish_snapshot: MarketSnapshot) -> None:
    from alerts import format_signal_alert
    from dataclasses import replace

    hostile = Signal(
        code="TEST",
        label="<script>alert(1)</script>",
        direction="bullish",
        severity=5,
        detail="breakout & <b>injection</b>",
        timeframe="15m",
    )
    snapshot = replace(bullish_snapshot, signals=(hostile,))
    text = format_signal_alert(snapshot, (hostile,), None)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_market_hours_gate(settings: Settings) -> None:
    monday_open = datetime(2026, 1, 5, 10, 0, tzinfo=IST)
    monday_early = datetime(2026, 1, 5, 9, 5, tzinfo=IST)
    monday_late = datetime(2026, 1, 5, 15, 31, tzinfo=IST)
    saturday = datetime(2026, 1, 10, 11, 0, tzinfo=IST)
    assert settings.is_market_open(monday_open)
    assert not settings.is_market_open(monday_early)
    assert not settings.is_market_open(monday_late)
    assert not settings.is_market_open(saturday)


def test_symbol_resolution(settings: Settings) -> None:
    assert settings.resolve_instrument("nifty").symbol == "^NSEI"
    assert settings.resolve_instrument("BANK NIFTY").symbol == "^NSEBANK"
    assert settings.resolve_instrument("reliance").symbol == "RELIANCE.NS"
    assert settings.resolve_instrument("TCS.NS").symbol == "TCS.NS"
    assert settings.resolve_instrument("M&M").symbol == "M&M.NS"
    with pytest.raises(Exception):
        settings.resolve_instrument("bad symbol!!")
