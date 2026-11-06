"""The pieces the report, assistant and search depend on."""

from __future__ import annotations

import datetime as dt

import studio


def _ist_epoch(hour: int, minute: int = 0) -> float:
    """Epoch seconds for an IST wall-clock time on a fixed trading date."""
    stamp = dt.datetime(2026, 11, 6, hour, minute, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    return stamp.timestamp()


def test_hourly_peaks_groups_by_ist_hour_and_skips_premarket() -> None:
    bars = []
    for index in range(8):  # 09:15 -> 11:15 IST, 15-minute bars
        bars.append(
            {
                "time": _ist_epoch(9, 15) + index * 900,
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 1000 + index * 100,
            }
        )
    bars.append(
        {  # 08:00 IST, before the open
            "time": _ist_epoch(8, 0),
            "open": 90, "high": 95, "low": 89, "close": 94, "volume": 99999,
        }
    )
    peaks = studio.hourly_peaks(bars)
    hours = [p["hour"] for p in peaks]
    assert hours == [9, 10, 11]
    assert peaks[0]["label"] == "09:00–10:00"
    assert peaks[0]["volume"] == 1000 + 1100 + 1200


def test_derive_plan_respects_direction() -> None:
    up = studio.derive_plan(
        {"symbol": "X.NS", "cmp": 100, "atr": 2, "verdict": "BUY", "bias": "BULLISH"}
    )
    assert up["verdict"] == "BUY"
    assert up["stop_loss"] < up["entry_low"] <= up["entry_high"] < up["target_1"] < up["target_2"]
    down = studio.derive_plan(
        {"symbol": "X.NS", "cmp": 100, "atr": 2, "verdict": "SELL", "bias": "BEARISH"}
    )
    assert down["stop_loss"] > down["entry_high"] >= down["entry_low"] > down["target_1"] > down["target_2"]
    assert studio.derive_plan({"symbol": "X.NS", "cmp": 100, "verdict": "NO TRADE"}) == {}


def test_snapshot_from_bars_produces_a_tradeable_read() -> None:
    bars = [
        {
            "time": _ist_epoch(9, 15) + index * 900,
            "open": 100 + index * 0.5,
            "high": 101 + index * 0.5,
            "low": 99 + index * 0.5,
            "close": 100.5 + index * 0.5,
            "volume": 5000 + index * 10,
        }
        for index in range(30)
    ]
    row = studio.snapshot_from_bars("TEST.NS", "Test Ltd", bars)
    assert row is not None
    assert row["cmp"] > row["vwap"] or row["cmp"] < row["vwap"]
    assert row["bias"] in {"BULLISH", "BEARISH", "NEUTRAL"}
    assert row["structure"] in {"HH/HL", "LH/LL", "range"}
    assert row["plan"]["verdict"] in {"BUY", "SELL"}
    assert studio.snapshot_from_bars("TEST.NS", "Test Ltd", []) is None


def test_detect_symbol_finds_a_company_in_a_sentence() -> None:
    hit = studio.detect_symbol("what is the read on Reliance today?")
    assert hit is not None and hit["symbol"].upper().startswith("RELIANCE")
    assert studio.detect_symbol("how is the market doing?") is None


def test_assistant_lookup_answers_for_an_unscanned_stock(monkeypatch) -> None:
    bars = [
        {
            "time": _ist_epoch(9, 15) + index * 900,
            "open": 500 + index,
            "high": 505 + index,
            "low": 495 + index,
            "close": 503 + index,
            "volume": 2000 + index * 5,
        }
        for index in range(24)
    ]
    monkeypatch.setattr(studio, "history_bars", lambda *a, **k: {"bars": bars, "source": "test"})
    result = studio.assistant_answer(
        "tell me about INFY",
        {"time": "10:30", "phase": "OPEN", "scanned": 0, "actionable": 0, "rows": []},
    )
    assert result["source"] == "local"
    assert result["focus"]["symbol"].upper().startswith("INFY")
    assert "INFY" in result["answer"]


def test_updater_payload_declares_assets(monkeypatch) -> None:
    import updater

    payload = updater.check("owner/repo", "1.0.0")
    assert "assets" in payload
    assert payload["assets"] == []
