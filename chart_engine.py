"""Live chart engine for the TradeVMx desktop.

Self-contained candlestick + volume panel. It owns no network calls: the
MarketScanner already fetches OHLCV (nsepython real-time spot + yfinance
history); the chart engine only rebuilds the hold of bars, the trading
levels and the latest snapshot for the front-end to draw.

Everything is deterministic and safe to call from a background thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import threading
import time

import numpy as np
import pandas as pd

from config import Settings, get_settings
from market_scanner import MarketSnapshot, ScanReport, Signal


@dataclass
class ChartBar:
    """A single OHLCV bar."""
    time: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "timeframe": self.timeframe,
        }


@dataclass
class ChartState:
    """Everything the front end needs to draw a live chart."""
    symbol: str
    name: str
    timeframe: str
    bars: list[ChartBar] = field(default_factory=list)
    levels: dict[str, list[float]] = field(default_factory=lambda: {"supports": [], "resistances": []})
    snapshot: MarketSnapshot | None = None
    is_stale: bool = True
    error: str | None = None
    scanned_at: float = 0.0


class ChartEngine:
    """Builds and keeps the live chart hold for one symbol time frame."""

    _registry: dict[int, "ChartEngine"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # The chart engine never reads settings via attributes that were
        # removed in the Settings refactor.  Everything it needs comes from
        # get_settings() directly, so it works whether or not a scanner has
        # been attached to the Settings object already.
        self._settings = get_settings()
        self._scanner = None
        self._pool: dict[tuple[str, str], ChartState] = {}
        self._lock = threading.Lock()

    @classmethod
    def registry(cls) -> dict[int, "ChartEngine"]:
        with cls._registry_lock:
            return dict(cls._registry)

    @classmethod
    def register(cls, engine: "ChartEngine") -> int:
        with cls._registry_lock:
            key = id(engine)
            cls._registry[key] = engine
            return key

    @classmethod
    def deregister(cls, key: int) -> None:
        with cls._registry_lock:
            cls._registry.pop(key, None)

    # ------------------------------------------------------------------
    def attach_scanner(self, scanner: object) -> None:
        """Point the chart engine at the live MarketScanner.

        The chart engine does not own the scanner: the desktop's DeskState
        injects it, and the sentinel keeps its own.  This keeps one scanner
        thread-pool and one cache for both the tape and the chart.
        """
        self._scanner = scanner

    # ------------------------------------------------------------------
    def state(self, symbol: str, timeframe: str = "15m") -> ChartState:
        now = time.time()
        key = (symbol.upper(), timeframe)

        with self._lock:
            st = self._pool.get(key)
            ttl = self._settings.snapshot_ttl_seconds or 60
            if st is None or (now - st.scanned_at) > ttl:
                st = self._rebuild(key, timeframe, now)
                self._pool[key] = st
            return st

    # ------------------------------------------------------------------
    def _rebuild(self, key: tuple[str, str], timeframe: str, now: float) -> ChartState:
        symbol, _ = key
        try:
            snapshot = self._latest_snapshot(symbol, timeframe, now)
        except Exception as exc:  # noqa: BLE001 - chart must never block the desk
            return ChartState(
                symbol=symbol,
                name=symbol,
                timeframe=timeframe,
                error=f"chart build failed: {exc}",
                scanned_at=now,
            )

        bars = self._bars_from_snapshot(snapshot, timeframe)
        return ChartState(
            symbol=symbol,
            name=snapshot.display_name or symbol,
            timeframe=timeframe,
            bars=bars,
            levels={"supports": snapshot.supports, "resistances": snapshot.resistances},
            snapshot=snapshot,
            is_stale=snapshot.is_stale,
            scanned_at=now,
        )

    # ------------------------------------------------------------------
    def _latest_snapshot(self, symbol: str, timeframe: str, now: float):
        # The production scanner is attached by the desktop (DeskState) or by
        # the sentinel.  In tests a hand-rolled snapshot is injected by the
        # test harness, so there may be no scanner at all.
        scanner = self._scanner
        if scanner is None:
            # Fall back to a fresh MarketScanner when nothing is attached.
            # The scanner uses get_settings() internally, so this needs no
            # further wiring.
            from market_scanner import MarketScanner

            scanner = MarketScanner(self._settings)
        return scanner.snapshot(
            instrument=scanner.settings.resolve_instrument(symbol),
            timeframe=timeframe,
            force=True,
        )

    # ------------------------------------------------------------------
    def _bars_from_snapshot(self, snapshot: MarketSnapshot, timeframe: str) -> list[ChartBar]:
        # The desktop keeps light OHLCV frames in memory. When a real
        # fetched frame is available we serialise it into bars; otherwise
        # we hand back a single bar from the latest snapshot so the view
        # always renders something meaningful.
        if snapshot.cmp is None or snapshot.cmp == 0:
            return []

        volume = snapshot.volume or 0.0
        return [
            ChartBar(
                time=int(snapshot.as_of.timestamp()),
                open=snapshot.cmp,
                high=snapshot.day_high,
                low=snapshot.day_low,
                close=snapshot.cmp,
                volume=volume,
                timeframe=timeframe,
            )
        ]
