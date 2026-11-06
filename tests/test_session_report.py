"""The long-form market report must be a real multi-page, charted PDF."""

from __future__ import annotations

import math
import re
from pathlib import Path

import studio


def _rows(count: int = 4) -> list[dict]:
    out = []
    for index in range(count):
        price = 1000.0 + index * 250
        out.append(
            {
                "symbol": f"TEST{index}.NS",
                "name": f"TEST NAME {index}",
                "cmp": price,
                "change_pct": (1.2 if index % 2 == 0 else -1.4),
                "bias": "BULLISH" if index % 2 == 0 else "BEARISH",
                "structure": "HH/HL" if index % 2 == 0 else "LH/LL",
                "vwap": price - 6,
                "atr": price * 0.012,
                "volume_ratio": 1.6,
                "ltp_source": "test",
                "verdict": "BUY" if index % 2 == 0 else "SELL",
                "risk_reward": 2.1,
                "signals": [
                    {"label": "VWAP reclaim", "timeframe": "15m", "severity": 4, "detail": "closed above VWAP"},
                ],
            }
        )
    return out


def _bars(count: int = 60) -> list[dict]:
    return [
        {
            "time": index,
            "open": 1000 + index,
            "high": 1012 + index,
            "low": 994 + index,
            "close": 1006 + index,
            "volume": 1000 + index * 7,
        }
        for index in range(count)
    ]


def test_session_report_is_multi_page_and_charted(tmp_path: Path) -> None:
    rows = _rows(4)
    bars = {row["symbol"]: _bars() for row in rows}
    plans = {
        row["symbol"]: {
            "verdict": row["verdict"],
            "entry_low": row["cmp"] - 5,
            "entry_high": row["cmp"] + 5,
            "stop_loss": row["cmp"] - 20,
            "target_1": row["cmp"] + 30,
            "target_2": row["cmp"] + 55,
            "risk_reward": 2.1,
        }
        for row in rows
    }
    artifact = studio.build_session_report(
        {
            "time": "10:30",
            "phase": "OPEN",
            "timeframe": "15m",
            "scanned": len(rows),
            "actionable": len(plans),
            "rows": rows,
        },
        out_dir=tmp_path,
        bars_by_symbol=bars,
        plans=plans,
        owner="Test Desk",
    )
    path = Path(artifact["path"])
    assert path.exists()
    data = path.read_bytes()
    assert data[:5] == b"%PDF-"
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    assert pages >= 8, f"expected at least 8 pages, got {pages}"
    assert artifact["bytes"] == path.stat().st_size


def test_candle_drawing_survives_bad_bars() -> None:
    drawing = studio._candle_drawing([{"open": "x"}, {}, {"close": math.nan}])
    assert drawing is not None
