"""TradeVMx Studio :: search, history, assistant, paper book and daily brief.

This module holds the pieces that make the desktop app a working desk rather
than a status page:

* :func:`search_instruments` - tolerant search over a large NSE universe so any
  listed stock can be found by ticker or by company name,
* :func:`history_bars` - real OHLCV history for an adjustable timeline (the
  chart is no longer a single-bar snapshot),
* :func:`assistant_answer` - the AI chamber.  Groq when a key is present, a
  deterministic local digest otherwise, so the chamber never dead-ends,
* :class:`PaperPortfolio` - a simulated demat account with cash, positions,
  realised/unrealised P&L, persisted to a JSON file,
* :func:`build_daily_brief` - a branded, professional PDF (logo, licence id,
  copyright footer) summarising the session,
* :func:`ensure_shortcut` - a desktop shortcut so the app opens with a click.

Everything degrades gracefully: no network, no key, no logo, no reportlab, the
caller still gets a useful result instead of an exception.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "web" / "static" / "logo.png"

PRODUCT = "TradeVMx"
TAGLINE = "NSE Market Desk"
COPYRIGHT = "© 2026 LogicalArnesh · All rights reserved."
LICENCE_ID = "TVMX-2026-DESK"
DEFAULT_CASH = 1_000_000.0

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
INDICES: tuple[dict[str, str], ...] = (
    {"symbol": "^NSEI", "name": "NIFTY 50", "kind": "index"},
    {"symbol": "^NSEBANK", "name": "BANK NIFTY", "kind": "index"},
    {"symbol": "^CNXIT", "name": "NIFTY IT", "kind": "index"},
    {"symbol": "^NSEMDCP50", "name": "NIFTY MIDCAP 50", "kind": "index"},
)

_UNIVERSE_RAW = """
RELIANCE|Reliance Industries
HDFCBANK|HDFC Bank
ICICIBANK|ICICI Bank
INFY|Infosys
TCS|Tata Consultancy Services
SBIN|State Bank of India
BHARTIARTL|Bharti Airtel
LT|Larsen & Toubro
ITC|ITC
KOTAKBANK|Kotak Mahindra Bank
AXISBANK|Axis Bank
HINDUNILVR|Hindustan Unilever
BAJFINANCE|Bajaj Finance
MARUTI|Maruti Suzuki
ASIANPAINT|Asian Paints
SUNPHARMA|Sun Pharmaceutical
TITAN|Titan Company
ULTRACEMCO|UltraTech Cement
WIPRO|Wipro
NESTLEIND|Nestle India
ADANIENT|Adani Enterprises
ADANIPORTS|Adani Ports
NTPC|NTPC
POWERGRID|Power Grid
ONGC|Oil & Natural Gas Corp
COALINDIA|Coal India
TATAMOTORS|Tata Motors
TATASTEEL|Tata Steel
JSWSTEEL|JSW Steel
HINDALCO|Hindalco Industries
GRASIM|Grasim Industries
TECHM|Tech Mahindra
HCLTECH|HCL Technologies
DRREDDY|Dr Reddy's Laboratories
CIPLA|Cipla
DIVISLAB|Divi's Laboratories
APOLLOHOSP|Apollo Hospitals
BAJAJFINSV|Bajaj Finserv
BAJAJ-AUTO|Bajaj Auto
HEROMOTOCO|Hero MotoCorp
EICHERMOT|Eicher Motors
M&M|Mahindra & Mahindra
TATACONSUM|Tata Consumer Products
BRITANNIA|Britannia Industries
SBILIFE|SBI Life Insurance
HDFCLIFE|HDFC Life Insurance
ICICIPRULI|ICICI Prudential Life
ICICIGI|ICICI Lombard
INDUSINDBK|IndusInd Bank
BANDHANBNK|Bandhan Bank
FEDERALBNK|Federal Bank
IDFCFIRSTB|IDFC First Bank
PNB|Punjab National Bank
BANKBARODA|Bank of Baroda
CANBK|Canara Bank
DLF|DLF
GODREJPROP|Godrej Properties
OBEROIRLTY|Oberoi Realty
LODHA|Macrotech Developers
PIDILITIND|Pidilite Industries
BERGEPAINT|Berger Paints
DABUR|Dabur India
MARICO|Marico
GODREJCP|Godrej Consumer
COLPAL|Colgate-Palmolive India
TATAPOWER|Tata Power
ADANIGREEN|Adani Green Energy
ADANITRANS|Adani Transmission
SIEMENS|Siemens India
ABB|ABB India
BHEL|Bharat Heavy Electricals
BEL|Bharat Electronics
HAL|Hindustan Aeronautics
IRCTC|IRCTC
DMART|Avenue Supermarts
ZOMATO|Zomato
PAYTM|One97 Communications
NYKAA|FSN E-Commerce
POLICYBZR|PB Fintech
TRENT|Trent
JUBLFOOD|Jubilant FoodWorks
PAGEIND|Page Industries
HAVELLS|Havells India
VOLTAS|Voltas
CROMPTON|Crompton Greaves
MOTHERSON|Samvardhana Motherson
BOSCHLTD|Bosch
TVSMOTOR|TVS Motor
ASHOKLEY|Ashok Leyland
ESCORTS|Escorts Kubota
BALKRISIND|Balkrishna Industries
MRF|MRF
APOLLOTYRE|Apollo Tyres
CEATLTD|CEAT
SHREECEM|Shree Cement
AMBUJACEM|Ambuja Cements
ACC|ACC
JINDALSTEL|Jindal Steel
SAIL|Steel Authority of India
VEDL|Vedanta
NMDC|NMDC
GAIL|GAIL India
IOC|Indian Oil
BPCL|Bharat Petroleum
HINDPETRO|Hindustan Petroleum
PETRONET|Petronet LNG
PIIND|PI Industries
UPL|UPL
SRF|SRF
COROMANDEL|Coromandel International
CHAMBLFERT|Chambal Fertilisers
AUROPHARMA|Aurobindo Pharma
LUPIN|Lupin
TORNTPHARM|Torrent Pharmaceuticals
ALKEM|Alkem Laboratories
ZYDUSLIFE|Zydus Lifesciences
GLENMARK|Glenmark Pharmaceuticals
BIOCON|Biocon
MAXHEALTH|Max Healthcare
FORTIS|Fortis Healthcare
NAUKRI|Info Edge
INDIAMART|IndiaMART
JUSTDIAL|Just Dial
LTIM|LTIMindtree
PERSISTENT|Persistent Systems
COFORGE|Coforge
MPHASIS|Mphasis
KPITTECH|KPIT Technologies
TATAELXSI|Tata Elxsi
OFSS|Oracle Financial Services
"""


def _parse_universe() -> tuple[dict[str, str], ...]:
    entries: list[dict[str, str]] = []
    for line in _UNIVERSE_RAW.strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        ticker, name = line.split("|", 1)
        ticker = ticker.strip().upper()
        entries.append(
            {
                "symbol": f"{ticker}.NS",
                "ticker": ticker,
                "name": name.strip(),
                "kind": "equity",
            }
        )
    return tuple(entries)


NSE_UNIVERSE: tuple[dict[str, str], ...] = _parse_universe()


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


MASTER_CACHE = BASE_DIR / "data" / "nse_equity_master.json"
MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
MASTER_TTL_SECONDS = 7 * 24 * 3600
_master_lock = threading.Lock()
_master_memo: list[dict[str, str]] | None = None


def symbol_master(*, force: bool = False) -> list[dict[str, str]]:
    """The full NSE cash-equity list, cached on disk for a week.

    The desk ships with a curated universe so search works offline; this adds the
    remaining listed companies (~2,000) the first time the app can reach NSE.
    """
    global _master_memo
    with _master_lock:
        if _master_memo is not None and not force:
            return _master_memo
        cached: list[dict[str, str]] = []
        try:
            if MASTER_CACHE.exists() and (
                not force
                and (time.time() - MASTER_CACHE.stat().st_mtime) < MASTER_TTL_SECONDS
            ):
                cached = json.loads(MASTER_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt cache is simply rebuilt
            cached = []
        if cached:
            _master_memo = cached
            return cached
        fetched: list[dict[str, str]] = []
        try:
            import requests

            response = requests.get(
                MASTER_URL,
                timeout=8,
                headers={
                    "User-Agent": "Mozilla/5.0 (TradeVMx desk)",
                    "Accept": "text/csv,*/*",
                },
            )
            response.raise_for_status()
            for index, line in enumerate(response.text.splitlines()):
                if index == 0 or not line.strip():
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                symbol, name = parts[0].strip(), parts[1].strip()
                if not symbol or not name:
                    continue
                fetched.append(
                    {"symbol": f"{symbol}.NS", "name": name.title(), "kind": "equity"}
                )
        except Exception as exc:  # noqa: BLE001 - offline is a normal state
            LOGGER.info("NSE symbol master unavailable: %s", exc)
            fetched = []
        if fetched:
            try:
                MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
                MASTER_CACHE.write_text(json.dumps(fetched), encoding="utf-8")
            except OSError:
                pass
        _master_memo = fetched or cached
        return _master_memo


def remote_search(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Ask Yahoo Finance for instruments matching a free-text company name."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001
        return []
    quotes: list[dict[str, Any]] = []
    try:
        searcher = yf.Search(query, max_results=max(limit, 12), news_count=0)
        quotes = list(getattr(searcher, "quotes", None) or [])
    except TypeError:
        try:
            quotes = list(yf.Search(query).quotes or [])
        except Exception:  # noqa: BLE001
            quotes = []
    except Exception as exc:  # noqa: BLE001 - search is best effort
        LOGGER.info("remote search failed for %s: %s", query, exc)
        quotes = []
    out: list[dict[str, Any]] = []
    for quote in quotes:
        try:
            symbol = str(quote.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            exchange = str(quote.get("exchange") or quote.get("exchDisp") or "").upper()
            kind = str(quote.get("quoteType") or "equity").lower()
            if kind == "index":
                kind = "index"
            elif kind not in {"equity", "etf", "index"}:
                kind = "equity"
            if "NSE" in exchange or symbol.endswith(".NS"):
                if not symbol.endswith(".NS") and "NS" in exchange:
                    symbol = f"{symbol}.NS"
            elif "BSE" in exchange or symbol.endswith(".BO"):
                if not symbol.endswith(".BO"):
                    symbol = f"{symbol}.BO"
            name = str(quote.get("longname") or quote.get("shortname") or symbol)
            out.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "kind": kind,
                    "description": f"{name} ({symbol})",
                    "exchange": exchange,
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return out[:limit]


def search_instruments(
    query: str,
    *,
    limit: int = 12,
    watchlist: Sequence[Any] = (),
    wide: bool = True,
) -> list[dict[str, Any]]:
    """Return ranked matches for a ticker or company name.

    Indices, the configured watchlist and the built-in universe are all
    searched.  With an empty query the watchlist plus indices are returned so
    the dropdown is never blank.
    """
    limit = max(1, min(int(limit or 12), 40))
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(symbol: str, name: str, kind: str) -> None:
        key = (symbol or "").upper()
        if not key or key in seen:
            return
        seen.add(key)
        pool.append(
            {
                "symbol": symbol,
                "name": name or symbol,
                "kind": kind,
                "description": f"{name or symbol} ({symbol})",
            }
        )

    for inst in watchlist:
        try:
            push(getattr(inst, "symbol", ""), getattr(inst, "name", ""), getattr(inst, "kind", "equity"))
        except Exception:  # noqa: BLE001 - a bad instrument must not break search
            continue
    for idx in INDICES:
        push(idx["symbol"], idx["name"], idx["kind"])
    for item in NSE_UNIVERSE:
        push(item["symbol"], item["name"], item["kind"])

    raw = (query or "").strip()
    if not raw:
        return pool[:limit]

    # Only widen with the full listed universe once the user is actually typing:
    # fetching the NSE master on first focus would stall the dropdown.
    if wide:
        try:
            for item in symbol_master():
                push(item["symbol"], item["name"], item["kind"])
        except Exception:  # noqa: BLE001 - offline keeps the curated list
            pass

    needle = _norm(raw)
    bare = raw.upper().replace(".NS", "").strip()

    def score(entry: dict[str, Any]) -> int:
        sym = entry["symbol"].upper()
        ticker = sym.replace(".NS", "")
        name = entry["name"].upper()
        nname = _norm(name)
        if ticker == bare or sym == raw.upper():
            return 0
        if ticker.startswith(bare) or sym.startswith(raw.upper()):
            return 1
        if name.startswith(raw.upper()):
            return 2
        if needle and needle in nname:
            return 3
        if bare and bare in ticker:
            return 4
        if needle and needle in sym:
            return 5
        return 99

    ranked = [(score(e), e) for e in pool]
    ranked = [(s, e) for s, e in ranked if s < 99]
    ranked.sort(key=lambda pair: (pair[0], pair[1]["symbol"]))
    return [e for _s, e in ranked][:limit]


# ---------------------------------------------------------------------------
# History (adjustable timeline)
# ---------------------------------------------------------------------------
_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m"}
_PERIOD = {"1m": "2d", "5m": "5d", "15m": "1mo", "30m": "3mo", "1h": "6mo"}


def _bar_records(df: Any, timeframe: str) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    try:
        columns = {str(c).lower(): c for c in df.columns}
    except Exception:  # noqa: BLE001
        return bars
    index = df.index
    for ts, row in zip(index, df.itertuples(index=False, name=None)):
        try:
            record = dict(zip(df.columns, row))
        except Exception:  # noqa: BLE001
            continue
        values = {str(k).lower(): v for k, v in record.items()}

        def pick(*names: str) -> float | None:
            for candidate in names:
                if candidate in values:
                    value = values[candidate]
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(number):
                        return number
            return None

        open_ = pick("open")
        high = pick("high")
        low = pick("low")
        close = pick("close", "adj close")
        volume = pick("volume") or 0.0
        if close is None:
            continue
        try:
            stamp = float(ts.timestamp())
        except Exception:  # noqa: BLE001
            continue
        bars.append(
            {
                "time": stamp,
                "open": open_ if open_ is not None else close,
                "high": high if high is not None else close,
                "low": low if low is not None else close,
                "close": close,
                "volume": volume,
                "timeframe": timeframe,
            }
        )
    return bars


def _flatten(frame: Any) -> Any:
    """Collapse a yfinance MultiIndex frame to plain OHLCV columns."""
    try:
        import pandas as pd  # noqa: F401
    except Exception:  # noqa: BLE001
        return frame
    if getattr(frame, "columns", None) is not None and getattr(frame.columns, "nlevels", 1) > 1:
        try:
            frame = frame.copy()
            frame.columns = frame.columns.get_level_values(0)
        except Exception:  # noqa: BLE001
            pass
    return frame


def history_bars(symbol: str, timeframe: str = "15m", limit: int = 200) -> dict[str, Any]:
    """Real OHLCV history for one symbol, newest last."""
    timeframe = timeframe if timeframe in _INTERVAL else "15m"
    limit = max(20, min(int(limit or 200), 400))
    out: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": [],
        "source": "none",
        "error": None,
    }
    if not symbol:
        out["error"] = "symbol is required"
        return out
    try:
        import yfinance as yf

        frame = yf.download(
            tickers=symbol,
            interval=_INTERVAL[timeframe],
            period=_PERIOD[timeframe],
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        frame = _flatten(frame)
        bars = _bar_records(frame, timeframe)
        if bars:
            out["bars"] = bars[-limit:]
            out["source"] = "yfinance"
        else:
            out["error"] = "no history returned"
    except Exception as exc:  # noqa: BLE001 - history is best effort
        LOGGER.warning("history failed for %s: %s", symbol, exc)
        out["error"] = str(exc)[:200]
    return out


def last_prices(symbols: Sequence[str]) -> dict[str, float]:
    """Best-effort last traded price per symbol (used to mark the paper book)."""
    wanted = [s for s in dict.fromkeys(symbols) if s]
    prices: dict[str, float] = {}
    if not wanted:
        return prices
    try:
        import yfinance as yf

        frame = yf.download(
            tickers=" ".join(wanted),
            interval="1d",
            period="5d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if frame is None or len(frame) == 0:
            return prices
        close = frame["Close"] if "Close" in getattr(frame, "columns", []) else frame
        if hasattr(close, "columns"):
            for column in close.columns:
                try:
                    value = float(close[column].dropna().iloc[-1])
                except Exception:  # noqa: BLE001
                    continue
                if math.isfinite(value):
                    prices[str(column).upper()] = value
        elif len(wanted) == 1:
            try:
                value = float(close.dropna().iloc[-1])
                if math.isfinite(value):
                    prices[wanted[0].upper()] = value
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("last_prices unavailable: %s", exc)
    return prices


# ---------------------------------------------------------------------------
# Paper demat account
# ---------------------------------------------------------------------------
class PaperPortfolio:
    """A simulated demat account.  Cash, positions, P&L, trade blotter."""

    def __init__(self, path: Path, starting_cash: float = DEFAULT_CASH) -> None:
        self.path = Path(path)
        self.starting_cash = float(starting_cash)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- store
    def _blank(self) -> dict[str, Any]:
        return {"cash": self.starting_cash, "positions": {}, "trades": []}

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._blank()
        if not isinstance(data, dict):
            return self._blank()
        data.setdefault("cash", self.starting_cash)
        data.setdefault("positions", {})
        data.setdefault("trades", [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - filesystem edge case
            LOGGER.warning("cannot persist paper book: %s", exc)

    # ------------------------------------------------------------- reading
    def snapshot(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        prices = {k.upper(): v for k, v in (prices or {}).items()}
        with self._lock:
            data = self._load()
        positions: list[dict[str, Any]] = []
        invested = 0.0
        value = 0.0
        for symbol, pos in (data.get("positions") or {}).items():
            qty = float(pos.get("qty", 0) or 0)
            avg = float(pos.get("avg", 0) or 0)
            if qty <= 0:
                continue
            ltp = prices.get(symbol.upper(), avg)
            cost = qty * avg
            worth = qty * ltp
            invested += cost
            value += worth
            positions.append(
                {
                    "symbol": symbol,
                    "name": pos.get("name") or symbol,
                    "qty": qty,
                    "avg": round(avg, 2),
                    "ltp": round(ltp, 2),
                    "cost": round(cost, 2),
                    "value": round(worth, 2),
                    "pnl": round(worth - cost, 2),
                    "pnl_pct": round((worth - cost) / cost * 100, 2) if cost else 0.0,
                }
            )
        cash = float(data.get("cash", self.starting_cash) or 0)
        equity = cash + value
        return {
            "starting_cash": round(self.starting_cash, 2),
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "market_value": round(value, 2),
            "equity": round(equity, 2),
            "realised": round(float(data.get("realised", 0) or 0), 2),
            "unrealised": round(value - invested, 2),
            "return_pct": round((equity - self.starting_cash) / self.starting_cash * 100, 2)
            if self.starting_cash
            else 0.0,
            "positions": sorted(positions, key=lambda p: -p["value"]),
            "trades": list(data.get("trades") or [])[-40:][::-1],
        }

    # ------------------------------------------------------------- writing
    def trade(
        self,
        action: str,
        symbol: str,
        qty: float,
        price: float,
        name: str = "",
    ) -> dict[str, Any]:
        action = (action or "").strip().upper()
        symbol = (symbol or "").strip().upper()
        if action not in {"BUY", "SELL"}:
            raise ValueError("action must be BUY or SELL")
        if not symbol:
            raise ValueError("symbol is required")
        try:
            qty = float(qty)
            price = float(price)
        except (TypeError, ValueError):
            raise ValueError("quantity and price must be numbers") from None
        if qty <= 0:
            raise ValueError("quantity must be greater than zero")
        if price <= 0:
            raise ValueError("price must be greater than zero")

        with self._lock:
            data = self._load()
            positions = data.setdefault("positions", {})
            cash = float(data.get("cash", self.starting_cash) or 0)
            pos = positions.get(symbol) or {"qty": 0.0, "avg": 0.0, "name": name or symbol}
            held = float(pos.get("qty", 0) or 0)
            avg = float(pos.get("avg", 0) or 0)

            if action == "BUY":
                cost = qty * price
                if cost > cash + 1e-6:
                    raise ValueError(
                        f"not enough cash: need {cost:,.0f}, available {cash:,.0f}"
                    )
                new_qty = held + qty
                new_avg = ((held * avg) + cost) / new_qty if new_qty else price
                positions[symbol] = {"qty": new_qty, "avg": new_avg, "name": pos.get("name") or symbol}
                cash -= cost
                realised = float(data.get("realised", 0) or 0)
            else:
                if qty > held + 1e-6:
                    raise ValueError(f"only {held:g} shares held")
                proceeds = qty * price
                realised = float(data.get("realised", 0) or 0) + (price - avg) * qty
                remaining = held - qty
                if remaining <= 1e-6:
                    positions.pop(symbol, None)
                else:
                    positions[symbol] = {
                        "qty": remaining,
                        "avg": avg,
                        "name": pos.get("name") or symbol,
                    }
                cash += proceeds

            data["cash"] = cash
            data["realised"] = realised
            trades = data.setdefault("trades", [])
            trades.append(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "action": action,
                    "symbol": symbol,
                    "name": name or symbol,
                    "qty": qty,
                    "price": round(price, 2),
                    "value": round(qty * price, 2),
                }
            )
            data["trades"] = trades[-200:]
            self._save(data)
        return self.snapshot({symbol: price})


# ---------------------------------------------------------------------------
# AI assistant chamber
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You are TradeVMx, the assistant inside a personal Indian equities desk. You are given "
    "the desk's live sweep (every tracked instrument with price, change, VWAP, ATR, "
    "structure, bias, verdict and signals) and, when the user asks about a specific stock, "
    "a live lookup for that stock.\n"
    "Rules: use only supplied numbers, never invent prices, targets, news or fundamentals. "
    "Be concrete: name the instrument, the level and the reason. If the desk data does not "
    "cover something, say exactly that in one sentence. Write 2-4 short paragraphs of plain "
    "prose, no markdown headings or bullets, no greetings, no filler. Finish with one "
    "sentence telling the user what to check next."
)

_STOPWORDS = {
    "the", "and", "for", "what", "which", "give", "show", "tell", "today", "todays",
    "market", "session", "report", "summary", "brief", "stock", "stock's", "stocks",
    "should", "would", "could", "about", "there", "this", "that", "with", "from",
    "good", "best", "strongest", "weakest", "risk", "open", "close", "now", "buy",
    "sell", "hold", "price", "prices", "chart", "page", "pdf", "please", "into",
    "tomorrow", "tomorrow's", "hour", "peak", "why", "how", "when", "more", "than",
    "nifty", "bank", "sensex", "desk", "like", "make", "need", "want", "also",
}


def _structure_of(bars: Sequence[dict[str, Any]]) -> str:
    """Fractal swing labelling: HH/HL, LH/LL, or range."""
    clean = []
    for bar in bars or []:
        try:
            clean.append((float(bar["high"]), float(bar["low"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(clean) < 7:
        return "range"
    window = 2
    highs, lows = [], []
    for index in range(window, len(clean) - window):
        high, low = clean[index]
        neighbours = clean[index - window:index] + clean[index + 1:index + 1 + window]
        if all(high >= other[0] for other in neighbours):
            highs.append(high)
        if all(low <= other[1] for other in neighbours):
            lows.append(low)
    if len(highs) < 2 or len(lows) < 2:
        # A clean trend has no local pivots at all: fall back to the slope of the
        # series so an uptrend is still labelled HH/HL instead of "range".
        half = max(1, len(clean) // 2)
        first = clean[:half]
        second = clean[-half:]
        first_mid = sum((h + l) / 2 for h, l in first) / len(first)
        second_mid = sum((h + l) / 2 for h, l in second) / len(second)
        change = (second_mid - first_mid) / first_mid if first_mid else 0.0
        if change > 0.0015:
            return "HH/HL"
        if change < -0.0015:
            return "LH/LL"
        return "range"
    higher_highs = highs[-1] > highs[-2]
    higher_lows = lows[-1] > lows[-2]
    if higher_highs and higher_lows:
        return "HH/HL"
    if not higher_highs and not higher_lows:
        return "LH/LL"
    return "range"


def snapshot_from_bars(
    symbol: str,
    name: str,
    bars: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Turn OHLCV into the same shape the sweep rows use."""
    clean = [b for b in bars or [] if isinstance(b, dict)]
    if len(clean) < 3:
        return None
    try:
        closes = [float(b["close"]) for b in clean]
        highs = [float(b["high"]) for b in clean]
        lows = [float(b["low"]) for b in clean]
        volumes = [float(b.get("volume") or 0) for b in clean]
    except (KeyError, TypeError, ValueError):
        return None
    cmp_ = closes[-1]
    first = closes[0] or cmp_
    typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    total_volume = sum(volumes)
    vwap = (
        sum(t * v for t, v in zip(typical, volumes)) / total_volume
        if total_volume > 0
        else sum(typical) / len(typical)
    )
    atr = sum(h - l for h, l in zip(highs, lows)) / len(clean)
    median_volume = sorted(volumes)[len(volumes) // 2] or 1.0
    structure = _structure_of(clean)
    if cmp_ > vwap and structure == "HH/HL":
        bias = "BULLISH"
    elif cmp_ < vwap and structure == "LH/LL":
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    row = {
        "symbol": symbol,
        "name": name or symbol,
        "cmp": round(cmp_, 2),
        "change_pct": round((cmp_ - first) / first * 100, 2) if first else 0.0,
        "bias": bias,
        "structure": structure,
        "vwap": round(vwap, 2),
        "atr": round(atr, 2),
        "volume_ratio": round(volumes[-1] / median_volume, 2),
        "day_high": round(max(highs), 2),
        "day_low": round(min(lows), 2),
        "ltp_source": "Yahoo Finance (live lookup)",
        "signals": [],
        "verdict": ("BUY" if bias == "BULLISH" else "SELL" if bias == "BEARISH" else None),
        "risk_reward": None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    plan = derive_plan(row)
    if plan:
        row["verdict"] = plan.get("verdict")
        row["risk_reward"] = plan.get("risk_reward")
        row["plan"] = plan
    row["bars"] = clean
    return row


def detect_symbol(question: str) -> dict[str, Any] | None:
    """Find the instrument a question is about, without hitting the network."""
    for token in re.findall(r"[A-Za-z0-9&][A-Za-z0-9&.\-]{1,23}", question or ""):
        if token.lower() in _STOPWORDS or len(token) < 2:
            continue
        try:
            matches = search_instruments(token, limit=1, wide=False)
        except Exception:  # noqa: BLE001
            matches = []
        if not matches:
            continue
        top = matches[0]
        ticker = str(top.get("symbol") or "").upper().replace(".NS", "")
        if ticker == token.upper().replace(".NS", "") or _norm(top.get("name")) .startswith(_norm(token)):
            return top
    return None


def _context_lines(context: dict[str, Any]) -> list[str]:
    rows = context.get("rows") or []
    lines = [
        f"Time (IST): {context.get('time') or 'unknown'}",
        f"Market phase: {context.get('phase') or 'unknown'}",
        f"Instruments scanned: {context.get('scanned') or 0}",
        f"Actionable setups: {context.get('actionable') or 0}",
    ]
    for row in rows[:12]:
        lines.append(
            f"- {row.get('name')} ({row.get('symbol')}): cmp {row.get('cmp')}, "
            f"{row.get('change_pct')}%, bias {row.get('bias')}, "
            f"structure {row.get('structure')}, vwap {row.get('vwap')}, "
            f"signals {', '.join(s.get('label', '') for s in (row.get('signals') or [])) or 'none'}, "
            f"verdict {row.get('verdict') or 'none'}"
        )
    if context.get("news"):
        lines.append("News: " + "; ".join(str(n) for n in context["news"][:5]))
    return lines


def _focus_answer(focus: dict[str, Any]) -> str:
    """A single-instrument read, used when the question names one stock."""
    plan = focus.get("plan") or derive_plan(focus)
    cmp_ = float(focus.get("cmp") or 0)
    vwap = float(focus.get("vwap") or 0)
    distance = ((cmp_ - vwap) / vwap * 100) if vwap else 0.0
    parts = [
        f"{focus.get('name')} ({focus.get('symbol')}) is at {_fmt(cmp_)} "
        f"({float(focus.get('change_pct') or 0):+.2f}%) with {focus.get('bias')} bias and "
        f"{focus.get('structure')} structure. It is {distance:+.2f}% versus session VWAP "
        f"({_fmt(vwap)}), ATR {_fmt(focus.get('atr'))}, volume "
        f"{_fmt(focus.get('volume_ratio'), 2)}x its median. "
        f"Source: {focus.get('ltp_source') or 'desk sweep'}."
    ]
    if plan and plan.get("verdict") in {"BUY", "SELL"}:
        parts.append(
            f"Desk read: {plan.get('verdict')} between {_fmt(plan.get('entry_low'))} and "
            f"{_fmt(plan.get('entry_high'))}, invalidated on a close beyond "
            f"{_fmt(plan.get('stop_loss'))}, first target {_fmt(plan.get('target_1'))}, "
            f"second {_fmt(plan.get('target_2'))} (about 1:{float(plan.get('risk_reward') or 0):.2f})."
        )
    else:
        parts.append(
            "Desk read: no trade. Price, structure and volume do not agree yet, so this stays on "
            "watch rather than becoming a position."
        )
    return " ".join(parts)


def _local_answer(question: str, context: dict[str, Any], *, focus: dict[str, Any] | None = None) -> str:
    rows = context.get("rows") or []
    scanned = context.get("scanned") or 0
    actionable = context.get("actionable") or 0
    bullish = [r for r in rows if str(r.get("bias", "")).upper() == "BULLISH"]
    bearish = [r for r in rows if str(r.get("bias", "")).upper() == "BEARISH"]
    above = [r for r in rows if (r.get("cmp") or 0) > (r.get("vwap") or 0)]
    header = (
        f"Desk snapshot at {context.get('time') or 'now'} IST: {scanned} instruments "
        f"scanned, {actionable} actionable. "
    )
    body = ""
    if rows:
        body = (
            f"{len(above)}/{len(rows)} trade above session VWAP. "
            f"Leaders: {', '.join(r.get('name', '') for r in bullish[:3]) or 'none'}. "
            f"Laggards: {', '.join(r.get('name', '') for r in bearish[:3]) or 'none'}. "
        )
        best = max(rows, key=lambda r: abs(float(r.get("change_pct") or 0)))
        body += (
            f"Largest move: {best.get('name')} at {best.get('change_pct')}%. "
        )
    calls = [r for r in rows if str(r.get("verdict") or "").upper() in {"BUY", "SELL"}]
    if calls:
        body += "Plans on the tape: " + "; ".join(
            f"{r.get('name')} {r.get('verdict')}"
            + (
                f" (R:R 1:{float(r['risk_reward']):.2f})"
                if isinstance(r.get("risk_reward"), (int, float))
                else ""
            )
            for r in calls[:5]
        ) + ". "
    if rows and (bullish or bearish):
        body += (
            "Read: "
            + ("demand is holding above VWAP " if len(above) > len(rows) / 2 else "supply is pressing price below VWAP ")
            + "and the strongest structure is "
            + ", ".join(r.get("name", "") for r in (bullish or bearish)[:2])
            + ". "
        )
    tail = (
        "Ask for the full report to get the charted PDF: hour-by-hour peaks, one page per "
        "instrument with entry bands, invalidation, targets, indicators and sources."
    )
    if focus:
        return _focus_answer(focus) + " " + header + body + tail
    return header + body + tail


def assistant_answer(
    question: str,
    context: dict[str, Any],
    *,
    api_key: str = "",
    model: str = "llama-3.3-70b-versatile",
    timeout: float = 40.0,
    history: Sequence[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Answer a desk question, grounded on the sweep plus a live lookup for the symbol.

    Groq is used when a key is present; the deterministic digest answers otherwise
    and always states honestly which one produced the text.
    """
    question = (question or "").strip()
    if not question:
        return {
            "answer": "Ask about the tape, a specific stock, the session so far, or ask for the "
                      "full report.",
            "source": "local",
        }

    context = dict(context or {})
    rows = [r for r in (context.get("rows") or []) if isinstance(r, dict)]
    focus: dict[str, Any] | None = None
    wanted = detect_symbol(question)
    if wanted:
        symbol = str(wanted.get("symbol") or "")
        for row in rows:
            if str(row.get("symbol") or "").upper() == symbol.upper():
                focus = row
                break
        if focus is None:
            try:
                payload = history_bars(symbol, "15m", 120)
                focus = snapshot_from_bars(symbol, str(wanted.get("name") or symbol),
                                           payload.get("bars") or [])
            except Exception as exc:  # noqa: BLE001 - lookup is best effort
                LOGGER.info("assistant lookup failed for %s: %s", symbol, exc)
                focus = None

    lines = _context_lines(context)
    if focus is not None:
        plan = focus.get("plan") or derive_plan(focus)
        lines.append(
            "Live lookup for the instrument the user asked about: "
            f"{focus.get('name')} ({focus.get('symbol')}): cmp {focus.get('cmp')}, "
            f"{focus.get('change_pct')}%, bias {focus.get('bias')}, "
            f"structure {focus.get('structure')}, vwap {focus.get('vwap')}, "
            f"atr {focus.get('atr')}, volume ratio {focus.get('volume_ratio')}, "
            f"day high {focus.get('day_high')}, day low {focus.get('day_low')}, "
            f"source {focus.get('ltp_source')}"
        )
        if plan:
            lines.append(
                "Desk plan for it: verdict "
                f"{plan.get('verdict')}, entry {plan.get('entry_low')} - {plan.get('entry_high')}, "
                f"invalidation {plan.get('stop_loss')}, targets {plan.get('target_1')} / "
                f"{plan.get('target_2')}, risk-reward 1:{plan.get('risk_reward')}"
            )
        history_peaks = hourly_peaks(focus.get("bars") or [])
        if history_peaks:
            loudest = max(history_peaks, key=lambda p: abs(float(p.get("move_pct") or 0)))
            lines.append(
                f"Peak hour for it: {loudest['label']} IST, move {float(loudest['move_pct']):+.2f}%"
            )

    messages: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM}]
    for turn in list(history or [])[-8:]:
        role = str(turn.get("role") or "").strip().lower()
        content = str(turn.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append(
        {
            "role": "user",
            "content": "Desk data:\n" + "\n".join(lines) + f"\n\nQuestion: {question}",
        }
    )

    base: dict[str, Any] = {"source": "local"}
    if focus is not None:
        base["focus"] = {
            "symbol": focus.get("symbol"),
            "name": focus.get("name"),
            "cmp": focus.get("cmp"),
            "change_pct": focus.get("change_pct"),
            "bias": focus.get("bias"),
            "structure": focus.get("structure"),
            "vwap": focus.get("vwap"),
            "verdict": focus.get("verdict"),
            "source": focus.get("ltp_source"),
        }

    if api_key:
        try:
            from groq import Groq

            client = Groq(api_key=api_key, timeout=timeout)
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.25,
                max_tokens=800,
            )
            text = (completion.choices[0].message.content or "").strip()
            if text:
                return {**base, "answer": text, "source": "groq", "model": model}
            raise ValueError("Groq returned an empty reply")
        except Exception as exc:  # noqa: BLE001 - never break the chamber
            LOGGER.warning("assistant Groq call failed: %s", exc)
            return {
                **base,
                "answer": _local_answer(question, context, focus=focus),
                "source": "local",
                "note": f"Groq did not answer ({type(exc).__name__}: {str(exc)[:160]}). "
                        "This answer came from the local desk engine. Use Test Groq in "
                        "Configuration to see the exact error.",
            }
    return {**base, "answer": _local_answer(question, context, focus=focus), "source": "local"}


# ---------------------------------------------------------------------------
# Export directory + shortcut
# ---------------------------------------------------------------------------
def resolve_export_dir(env_path: Path | None = None, default: Path | None = None) -> Path:
    """Where PDFs are written.  ``EXPORT_DIR`` in .env wins, else reports/."""
    fallback = Path(default or (BASE_DIR / "reports"))
    raw = ""
    try:
        import settings_store

        values = settings_store.read_env(env_path)
        raw = (values.get("EXPORT_DIR") or "").strip()
    except Exception:  # noqa: BLE001
        raw = ""
    if not raw:
        target = fallback
    else:
        expanded = Path(os.path.expanduser(os.path.expandvars(raw)))
        target = expanded if expanded.is_absolute() else (BASE_DIR / expanded)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.warning("export dir %s unavailable (%s) - using %s", target, exc, fallback)
        target = fallback
        target.mkdir(parents=True, exist_ok=True)
    return target


def ensure_shortcut(target: Path | None = None, name: str = "TradeVMx") -> dict[str, Any]:
    """Create a desktop shortcut so the app opens without hunting for the .bat."""
    target = Path(target or BASE_DIR)
    if os.name != "nt":
        return {"ok": False, "platform": os.name, "detail": "Shortcut creation is Windows-only."}
    launcher = target / "run_tradevmx.bat"
    if not launcher.exists():
        return {"ok": False, "detail": f"{launcher.name} not found next to the app."}
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    if not desktop.exists():
        desktop = target
    link = desktop / f"{name}.lnk"
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{link}'); "
        f"$s.TargetPath = '{launcher}'; "
        f"$s.WorkingDirectory = '{target}'; "
        "$s.Description = 'TradeVMx - NSE Market Desk'; "
        f"$s.IconLocation = '{(target / 'web' / 'static' / 'logo.ico')}'; "
        "$s.Save()"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0 and link.exists():
            return {"ok": True, "path": str(link), "detail": f"Shortcut created on the Desktop."}
        return {
            "ok": False,
            "detail": (completed.stderr or "PowerShell could not create the shortcut.").strip()[:220],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Shortcut failed: {exc}"}


# ---------------------------------------------------------------------------
# Branded daily brief PDF
# ---------------------------------------------------------------------------
def _fmt(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:,.{digits}f}"


def brief_filename(when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H%M")
    return f"TradeVMx_Daily_Brief_{stamp}.pdf"


def build_daily_brief(
    context: dict[str, Any],
    *,
    out_dir: Path,
    commentary: str = "",
    owner: str = "",
    licence: str = LICENCE_ID,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render a concise, branded PDF of the session.

    Returns ``{"path": str, "filename": str, "bytes": int}``.  Raises
    ``RuntimeError`` only when reportlab itself is unavailable.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            HRFlowable,
            Image,
            KeepTogether,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("reportlab is required for PDF export") from exc

    when = now or datetime.now()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = brief_filename(when)
    path = out_dir / filename

    ink = colors.HexColor("#0b1220")
    teal = colors.HexColor("#0f766e")
    slate = colors.HexColor("#64748b")
    line = colors.HexColor("#d7dee8")

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TVXTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19,
        leading=22, textColor=ink, alignment=TA_LEFT, spaceAfter=2,
    )
    sub = ParagraphStyle(
        "TVXSub", parent=styles["Normal"], fontName="Helvetica", fontSize=9.5,
        leading=13, textColor=slate,
    )
    h2 = ParagraphStyle(
        "TVXH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11.5,
        leading=15, textColor=teal, spaceBefore=12, spaceAfter=5,
    )
    body = ParagraphStyle(
        "TVXBody", parent=styles["Normal"], fontName="Helvetica", fontSize=9.2,
        leading=13.4, textColor=ink,
    )
    cell = ParagraphStyle(
        "TVXCell", parent=styles["Normal"], fontName="Helvetica", fontSize=8.2, leading=10.6,
        textColor=ink,
    )
    cell_head = ParagraphStyle(
        "TVXCellHead", parent=cell, fontName="Helvetica-Bold", textColor=colors.white,
    )
    foot = ParagraphStyle(
        "TVXFoot", parent=styles["Normal"], fontName="Helvetica", fontSize=7.4, leading=10,
        textColor=slate, alignment=TA_CENTER,
    )

    rows = context.get("rows") or []
    scanned = int(context.get("scanned") or len(rows) or 0)
    actionable = int(context.get("actionable") or 0)
    above = [r for r in rows if (r.get("cmp") or 0) > (r.get("vwap") or 0)]
    bullish = [r for r in rows if str(r.get("bias", "")).upper() == "BULLISH"]
    bearish = [r for r in rows if str(r.get("bias", "")).upper() == "BEARISH"]
    movers = sorted(rows, key=lambda r: abs(float(r.get("change_pct") or 0)), reverse=True)[:6]

    story: list[Any] = []

    logo = None
    if LOGO_PATH.exists():
        try:
            from PIL import Image as _PILImage

            with _PILImage.open(LOGO_PATH) as probe:
                probe.verify()
            logo = LOGO_PATH
        except Exception:  # noqa: BLE001 - an unusable logo is simply skipped
            LOGGER.warning("logo.png is not a readable image; the brief renders without it")
            logo = None
    header_bits: list[Any] = []
    header_text = [
        Paragraph(f"{PRODUCT} · {TAGLINE}", sub),
        Paragraph("Daily Market Brief", title),
        Paragraph(
            when.strftime("%A, %d %B %Y · %H:%M IST") + (f" · prepared for {owner}" if owner else ""),
            sub,
        ),
    ]
    if logo is not None:
        try:
            header_bits = [
                [Image(str(logo), width=26 * mm, height=26 * mm, kind="proportional"), header_text]
            ]
        except Exception:  # noqa: BLE001 - a broken logo must not kill the report
            header_bits = []
    if header_bits:
        table = Table(header_bits, colWidths=[32 * mm, 138 * mm])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(table)
    else:
        story.extend(header_text)
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1.1, color=teal, spaceAfter=8))

    tiles = [
        ["Instruments", "Actionable", "Above VWAP", "Breadth"],
        [
            str(scanned),
            str(actionable),
            f"{len(above)}/{len(rows)}" if rows else "—",
            f"{len(bullish)} up / {len(bearish)} down" if rows else "—",
        ],
    ]
    tile_table = Table(tiles, colWidths=[42 * mm, 42 * mm, 42 * mm, 44 * mm])
    tile_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4f3")),
        ("TEXTCOLOR", (0, 0), (-1, 0), slate),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 13),
        ("TEXTCOLOR", (0, 1), (-1, 1), ink),
        ("GRID", (0, 0), (-1, -1), 0.4, line),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tile_table)

    story.append(Paragraph("Session summary", h2))
    if commentary:
        for para in [p.strip() for p in str(commentary).split("\n") if p.strip()][:8]:
            story.append(Paragraph(para.replace("&", "&amp;"), body))
            story.append(Spacer(1, 3))
    else:
        story.append(
            Paragraph(
                f"{len(above)} of {len(rows) or 0} tracked instruments trade above session VWAP, "
                f"with {actionable} setups above the desk severity floor. "
                f"Leadership: {', '.join(r.get('name', '') for r in bullish[:4]) or 'none'}. "
                f"Laggards: {', '.join(r.get('name', '') for r in bearish[:4]) or 'none'}.",
                body,
            )
        )

    story.append(Paragraph("Largest moves", h2))
    if movers:
        data = [[
            Paragraph("Instrument", cell_head),
            Paragraph("CMP", cell_head),
            Paragraph("Chg %", cell_head),
            Paragraph("Bias", cell_head),
            Paragraph("Verdict", cell_head),
        ]]
        for row in movers:
            data.append([
                Paragraph(f"{row.get('name') or ''}<br/><font size=7 color='#64748b'>{row.get('symbol') or ''}</font>", cell),
                Paragraph(_fmt(row.get("cmp")), cell),
                Paragraph(_fmt(row.get("change_pct")), cell),
                Paragraph(str(row.get("bias") or "—"), cell),
                Paragraph(str(row.get("verdict") or "—"), cell),
            ])
        table = Table(data, colWidths=[62 * mm, 26 * mm, 24 * mm, 28 * mm, 30 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), teal),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
            ("GRID", (0, 0), (-1, -1), 0.35, line),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("No sweep data in this session yet — arm the desk and re-run.", body))

    signals = []
    for row in rows:
        for sig in (row.get("signals") or [])[:2]:
            signals.append(
                f"<b>{row.get('name')}</b> · {sig.get('label')} "
                f"({sig.get('timeframe')}, severity {sig.get('severity')}/5) — {sig.get('detail') or ''}"
            )
    story.append(Paragraph("Signals on the tape", h2))
    if signals:
        for line_text in signals[:10]:
            story.append(Paragraph("• " + line_text, body))
            story.append(Spacer(1, 2))
    else:
        story.append(Paragraph("No qualifying VWAP, volume or structure event is live right now.", body))

    story.append(Paragraph("Tomorrow's plan", h2))
    story.append(
        Paragraph(
            "Carry the strongest names above VWAP on the watchlist, respect the invalidation on "
            "each plan, and avoid mean-reversion entries into low-volume drift. Re-arm the desk "
            "before the 09:15 IST open so the first sweep lands with the opening range.",
            body,
        )
    )

    def _decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.6)
        canvas.line(18 * mm, 16 * mm, width - 18 * mm, 16 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(slate)
        canvas.drawString(18 * mm, 11.4 * mm, f"{PRODUCT} · {COPYRIGHT}")
        canvas.drawCentredString(width / 2, 11.4 * mm, f"Licence {licence}")
        canvas.drawRightString(width - 18 * mm, 11.4 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=f"{PRODUCT} Daily Brief {when:%Y-%m-%d}",
        author=owner or "TradeVMx",
        subject="NSE market desk daily brief",
    )
    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)

    size = path.stat().st_size if path.exists() else 0
    return {"path": str(path), "filename": filename, "bytes": size}


def env_owner(env_path: Path | None = None) -> str:
    """Report owner from ``.env`` (``OWNER_NAME``), empty when unset."""
    try:
        import settings_store

        return (settings_store.read_env(env_path).get("OWNER_NAME") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _sparkline(values: Sequence[float], width: float, height: float, colour: tuple) -> Any:
    """A tiny price polyline as a reportlab Drawing (no matplotlib)."""
    from reportlab.graphics.shapes import Drawing, Line, PolyLine

    drawing = Drawing(width, height)
    drawing.add(Line(0, 0, width, 0, strokeColor=None, strokeWidth=0))
    clean = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if len(clean) < 2:
        return drawing
    low, high = min(clean), max(clean)
    span = (high - low) or 1.0
    step = width / (len(clean) - 1)
    points: list[float] = []
    for index, value in enumerate(clean):
        points.extend([index * step, (value - low) / span * (height - 2) + 1])
    drawing.add(
        PolyLine(
            points,
            strokeColor=colour,
            strokeWidth=1.1,
            fillColor=None,
        )
    )
    return drawing


def build_watchlist_brief(
    rows: Sequence[dict[str, Any]],
    bars_by_symbol: dict[str, Sequence[dict[str, Any]]] | None = None,
    *,
    out_dir: Path,
    plans: dict[str, dict[str, Any]] | None = None,
    owner: str = "",
    commentary: str = "",
    licence: str = LICENCE_ID,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Branded watchlist digest: predictions, mini charts and sources.

    Designed to be short and readable: one page per dozen names at most, with a
    sparkline per instrument so the brief is genuinely visual.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            HRFlowable,
            Image,
            KeepTogether,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("reportlab is required for PDF export") from exc

    when = now or datetime.now()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"TradeVMx_Watchlist_Digest_{when:%Y-%m-%d_%H%M}.pdf"
    path = out_dir / filename

    ink = colors.HexColor("#0b1220")
    teal = colors.HexColor("#0f766e")
    green = colors.HexColor("#12805c")
    red = colors.HexColor("#b3261e")
    slate = colors.HexColor("#64748b")
    line = colors.HexColor("#d7dee8")

    styles = getSampleStyleSheet()
    title = ParagraphStyle("WTitle", parent=styles["Title"], fontName="Helvetica-Bold",
                           fontSize=18, leading=21, textColor=ink, alignment=TA_LEFT, spaceAfter=2)
    sub = ParagraphStyle("WSub", parent=styles["Normal"], fontName="Helvetica", fontSize=9.2,
                         leading=12.6, textColor=slate)
    h2 = ParagraphStyle("WH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11,
                        leading=14.5, textColor=teal, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("WBody", parent=styles["Normal"], fontName="Helvetica", fontSize=9,
                          leading=13, textColor=ink)
    cell = ParagraphStyle("WCell", parent=styles["Normal"], fontName="Helvetica", fontSize=7.8,
                          leading=10, textColor=ink)
    head_cell = ParagraphStyle("WHCell", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    foot = ParagraphStyle("WFoot", parent=styles["Normal"], fontName="Helvetica", fontSize=7.2,
                          leading=9.6, textColor=slate)

    plans = plans or {}
    bars_by_symbol = bars_by_symbol or {}
    listed = list(rows)[:14]
    setups = [r for r in listed if str(r.get("verdict") or "").upper() not in {"", "NO TRADE"}]
    above = [r for r in listed if (r.get("cmp") or 0) > (r.get("vwap") or 0)]

    story: list[Any] = []
    logo = None
    if LOGO_PATH.exists():
        try:
            from PIL import Image as _PIL

            with _PIL.open(LOGO_PATH) as probe:
                probe.verify()
            logo = LOGO_PATH
        except Exception:  # noqa: BLE001
            logo = None

    header_text = [
        Paragraph(f"{PRODUCT} · {TAGLINE}", sub),
        Paragraph("Watchlist Digest", title),
        Paragraph(when.strftime("%A, %d %B %Y · %H:%M IST") + (f" · {owner}" if owner else ""), sub),
    ]
    if logo is not None:
        try:
            table = Table([[Image(str(logo), width=24 * mm, height=24 * mm, kind="proportional"), header_text]],
                          colWidths=[30 * mm, 140 * mm])
            table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            story.append(table)
        except Exception:  # noqa: BLE001
            story.extend(header_text)
    else:
        story.extend(header_text)
    story.append(Spacer(1, 5))
    story.append(HRFlowable(width="100%", thickness=1.1, color=teal, spaceAfter=7))

    tiles = [["On watch", "Setups", "Above VWAP", "Window"],
             [str(len(listed)), str(len(setups)),
              f"{len(above)}/{len(listed)}" if listed else "—",
              when.strftime("%H:%M") + " IST"]]
    tile_table = Table(tiles, colWidths=[42 * mm, 42 * mm, 42 * mm, 44 * mm])
    tile_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4f3")),
        ("TEXTCOLOR", (0, 0), (-1, 0), slate),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 12.5),
        ("TEXTCOLOR", (0, 1), (-1, 1), ink),
        ("GRID", (0, 0), (-1, -1), 0.4, line),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tile_table)

    if commentary:
        story.append(Paragraph("Desk read", h2))
        for para in [p.strip() for p in str(commentary).split("\n") if p.strip()][:6]:
            story.append(Paragraph(para.replace("&", "&amp;"), body))

    story.append(Paragraph("Predictions and levels", h2))
    heads = ["Instrument", "CMP", "Chg", "Bias", "Plan", "Entry", "Stop", "T1 / T2", "Trend"]
    data = [[Paragraph(h, head_cell) for h in heads]]
    for row in listed:
        plan = plans.get(row.get("symbol") or "") or {}
        trend = [b.get("close") for b in (bars_by_symbol.get(row.get("symbol") or "") or [])][-40:]
        up = (row.get("change_pct") or 0) >= 0
        entry = "—"
        if plan.get("entry_low") is not None and plan.get("entry_high") is not None:
            entry = f"{_fmt(plan.get('entry_low'))}–{_fmt(plan.get('entry_high'))}"
        data.append([
            Paragraph(f"{row.get('name') or ''}<br/><font size=6.6 color='#64748b'>{row.get('symbol') or ''}</font>", cell),
            Paragraph(_fmt(row.get("cmp")), cell),
            Paragraph(_fmt(row.get("change_pct")), cell),
            Paragraph(str(row.get("bias") or "—"), cell),
            Paragraph(str(plan.get("verdict") or row.get("verdict") or "—"), cell),
            Paragraph(entry, cell),
            Paragraph(_fmt(plan.get("stop_loss")) if plan.get("stop_loss") is not None else "—", cell),
            Paragraph(
                (f"{_fmt(plan.get('target_1'))} / {_fmt(plan.get('target_2'))}")
                if plan.get("target_1") is not None else "—", cell),
            _sparkline(trend, 30 * mm, 8 * mm, green if up else red),
        ])
    if len(data) == 1:
        story.append(Paragraph("No sweep data yet — arm the desk to populate the digest.", body))
    else:
        widths = [34 * mm, 17 * mm, 13 * mm, 17 * mm, 16 * mm, 22 * mm, 15 * mm, 22 * mm, 32 * mm]
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), teal),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
            ("GRID", (0, 0), (-1, -1), 0.3, line),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(table)

    story.append(Paragraph("Signals worth acting on", h2))
    lines_out: list[str] = []
    for row in listed:
        for signal in (row.get("signals") or [])[:2]:
            if int(signal.get("severity") or 0) >= 3:
                lines_out.append(
                    f"<b>{row.get('name')}</b> · {signal.get('label')} "
                    f"({signal.get('timeframe')}, {signal.get('severity')}/5) — {signal.get('detail') or ''}"
                )
    if lines_out:
        for text_line in lines_out[:10]:
            story.append(Paragraph("• " + text_line, body))
            story.append(Spacer(1, 1.5))
    else:
        story.append(Paragraph("No setup at or above the severity floor right now.", body))

    story.append(Paragraph("Sources &amp; method", h2))
    feeds = sorted({str(r.get("ltp_source") or "feed") for r in listed})
    method = [
        f"Price feed: {', '.join(feeds)} (yfinance intraday OHLCV, NSE spot overlay where available).",
        f"Sweep: {len(listed)} instruments on the {when.strftime('%H:%M')} IST pass; "
        "VWAP is session-anchored, ATR is 14-bar, structure is HH/HL vs LH/LL.",
        "Plans are rule-based ATR targets, upgraded by the language model only when a Groq key is configured.",
        "Signal severities are 1-5; this digest lists 3 and above.",
        "Levels are computed from swing pivots and order blocks, not from third-party level services.",
    ]
    for item in method:
        story.append(Paragraph("• " + item.replace("&", "&amp;"), foot))
        story.append(Spacer(1, 1.5))

    def _decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.6)
        canvas.line(18 * mm, 16 * mm, width - 18 * mm, 16 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(slate)
        canvas.drawString(18 * mm, 11.4 * mm, f"{PRODUCT} · {COPYRIGHT}")
        canvas.drawCentredString(width / 2, 11.4 * mm, f"Licence {licence}")
        canvas.drawRightString(width - 18 * mm, 11.4 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=20 * mm,
        title=f"{PRODUCT} Watchlist Digest {when:%Y-%m-%d}",
        author=owner or PRODUCT, subject="NSE watchlist digest",
    )
    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return {"path": str(path), "filename": filename, "bytes": path.stat().st_size if path.exists() else 0}


# ---------------------------------------------------------------------------
# Long-form, charted session report
# ---------------------------------------------------------------------------
def _candle_drawing(bars: Sequence[dict[str, Any]], width: float = 480.0, height: float = 150.0) -> Any:
    """A candlestick + volume drawing for reportlab (no matplotlib needed)."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors

    drawing = Drawing(width, height)
    gh = colors.HexColor("#e3e9f1")
    slate = colors.HexColor("#64748b")
    up_colour = colors.HexColor("#12805c")
    down_colour = colors.HexColor("#b3261e")
    up_vol = colors.HexColor("#b7d8cd")
    down_vol = colors.HexColor("#e8c3c0")

    clean: list[tuple[float, float, float, float, float]] = []
    for bar in bars or []:
        try:
            o = float(bar.get("open"))
            h = float(bar.get("high"))
            l = float(bar.get("low"))
            c = float(bar.get("close"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c)):
            continue
        try:
            volume = float(bar.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        clean.append((o, h, l, c, volume))

    if len(clean) < 3:
        drawing.add(String(width / 2, height / 2 - 3, "not enough bars for a chart",
                           textAnchor="middle", fontSize=7.5, fillColor=slate))
        return drawing

    clean = clean[-80:]
    pad_left, pad_right, pad_top, vol_h = 4.0, 42.0, 6.0, 34.0
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - vol_h - 14
    high = max(item[1] for item in clean)
    low = min(item[2] for item in clean)
    span = (high - low) or 1.0
    max_volume = max((item[4] for item in clean), default=1.0) or 1.0
    slot = plot_w / len(clean)
    body_w = max(1.1, slot * 0.58)
    baseline = pad_top + plot_h

    def y_of(price: float) -> float:
        return pad_top + (price - low) / span * plot_h

    for step in range(5):
        price = low + span * step / 4
        y = y_of(price)
        drawing.add(Line(pad_left, y, pad_left + plot_w, y, strokeColor=gh, strokeWidth=0.4))
        drawing.add(String(width - pad_right + 3, y - 2.4, f"{price:,.0f}", fontSize=5.8, fillColor=slate))
    drawing.add(Line(pad_left, baseline + 8, pad_left + plot_w, baseline + 8, strokeColor=gh, strokeWidth=0.5))

    for index, (open_, high_, low_, close, volume) in enumerate(clean):
        x = pad_left + slot * index + slot / 2
        rising = close >= open_
        colour = up_colour if rising else down_colour
        drawing.add(Line(x, y_of(low_), x, y_of(high_), strokeColor=colour, strokeWidth=0.6))
        top = y_of(max(open_, close))
        bottom = y_of(min(open_, close))
        drawing.add(Rect(x - body_w / 2, bottom, body_w, max(0.6, top - bottom),
                         fillColor=colour, strokeColor=None))
        if volume > 0:
            bar_h = (volume / max_volume) * (vol_h - 4)
            drawing.add(Rect(x - body_w / 2, baseline + 8, body_w, max(0.4, bar_h),
                             fillColor=up_vol if rising else down_vol, strokeColor=None))
    return drawing


IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60


def hourly_peaks(
    bars: Sequence[dict[str, Any]],
    *,
    offset: int = IST_OFFSET_SECONDS,
) -> list[dict[str, Any]]:
    """Group OHLCV bars into IST clock hours, newest last.

    Returns the open/high/low/close, traded volume, the bar range and the move
    inside each hour, so the report can say *which hour* carried the session.
    """
    # Only the most recent session: the history window spans weeks, so mixing
    # days into "this hour" would be meaningless.
    stamped: list[tuple[dict[str, Any], int, int]] = []
    for bar in bars or []:
        try:
            stamp = float(bar.get("time"))
        except (TypeError, ValueError):
            continue
        ist = stamp + offset
        stamped.append((bar, int(ist // 86400), int((ist % 86400) // 3600)))
    if not stamped:
        return []
    latest_day = max(day for _, day, _ in stamped)

    buckets: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for bar, day, hour in stamped:
        if day != latest_day:
            continue
        try:
            high = float(bar.get("high"))
            low = float(bar.get("low"))
            close = float(bar.get("close"))
            open_ = float(bar.get("open") or close)
            volume = float(bar.get("volume") or 0.0)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (high, low, close, open_)):
            continue
        bucket = buckets.get(hour)
        if bucket is None:
            buckets[hour] = {
                "hour": hour,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "bars": 1,
            }
            order.append(hour)
        else:
            bucket["high"] = max(bucket["high"], high)
            bucket["low"] = min(bucket["low"], low)
            bucket["close"] = close
            bucket["volume"] += volume
            bucket["bars"] += 1

    peaks: list[dict[str, Any]] = []
    for hour in sorted(order):
        if not (9 <= hour <= 16):
            # Outside the 09:15-15:30 IST cash session: pre-open or a vendor
            # artefact, never a session peak.
            continue
        bucket = buckets[hour]
        opening = bucket["open"] or bucket["close"]
        move = (bucket["close"] - opening) / opening * 100 if opening else 0.0
        span = bucket["high"] - bucket["low"]
        peaks.append(
            {
                **bucket,
                "label": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00",
                "move_pct": move,
                "span": span,
                "span_pct": (span / opening * 100) if opening else 0.0,
            }
        )
    return peaks


def session_hourly_map(
    rows: Sequence[dict[str, Any]],
    bars_by_symbol: dict[str, Sequence[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Per IST hour across every instrument that has history."""
    names = {str(row.get("symbol")): row.get("name") or row.get("symbol") for row in rows}
    merged: dict[int, dict[str, Any]] = {}
    for symbol, bars in (bars_by_symbol or {}).items():
        peaks = hourly_peaks(bars)
        if not peaks:
            continue
        top = max(peaks, key=lambda p: abs(float(p.get("move_pct") or 0)))
        for peak in peaks:
            hour = int(peak["hour"])
            slot = merged.setdefault(
                hour,
                {
                    "hour": hour,
                    "label": peak["label"],
                    "volume": 0.0,
                    "span_pct": 0.0,
                    "count": 0,
                    "leader": None,
                },
            )
            slot["volume"] += float(peak.get("volume") or 0.0)
            slot["span_pct"] += float(peak.get("span_pct") or 0.0)
            slot["move_sum"] = slot.get("move_sum", 0.0) + float(peak.get("move_pct") or 0.0) * max(
                1.0, float(peak.get("volume") or 0.0)
            )
            slot["count"] += 1
            best = slot.get("leader")
            if best is None or abs(float(peak.get("move_pct") or 0)) > abs(float(best["move_pct"])):
                slot["leader"] = {
                    "symbol": symbol,
                    "name": names.get(symbol, symbol),
                    "move_pct": float(peak.get("move_pct") or 0),
                    "high": float(peak.get("high") or 0),
                }
        _ = top
    out = []
    for hour in sorted(merged):
        slot = merged[hour]
        count = max(1, int(slot["count"]))
        slot["span_pct"] = slot["span_pct"] / count
        slot["move_pct"] = (
            slot.get("move_sum", 0.0) / slot["volume"] if slot["volume"] else 0.0
        )
        out.append(slot)
    return out


def _session_map_drawing(rows: Sequence[dict[str, Any]], width: float = 480.0,
                         height: float = 120.0) -> Any:
    """Diverging bar chart of every instrument's change — needs no history."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors

    drawing = Drawing(width, height)
    ink = colors.HexColor("#0b1220")
    slate = colors.HexColor("#64748b")
    grid = colors.HexColor("#e3e9f1")
    up = colors.HexColor("#12805c")
    down = colors.HexColor("#b3261e")

    ranked = [r for r in rows if isinstance(r, dict)][:14]
    if not ranked:
        drawing.add(String(width / 2, height / 2, "no instruments in this sweep",
                           textAnchor="middle", fontSize=8, fillColor=slate))
        return drawing

    label_w, right_pad = 108.0, 46.0
    plot_w = width - label_w - right_pad
    row_h = min(14.0, (height - 12) / max(1, len(ranked)))
    centre = label_w + plot_w / 2
    biggest = max(1e-6, max(abs(float(r.get("change_pct") or 0)) for r in ranked))
    drawing.add(Line(centre, 4, centre, 8 + row_h * len(ranked), strokeColor=grid, strokeWidth=0.7))
    for index, row in enumerate(ranked):
        change = float(row.get("change_pct") or 0)
        y = 8 + row_h * (len(ranked) - index - 1)
        bar_w = (abs(change) / biggest) * (plot_w / 2 - 4)
        colour = up if change >= 0 else down
        x = centre if change >= 0 else centre - bar_w
        drawing.add(Rect(x, y, max(0.6, bar_w), max(2.0, row_h - 4), fillColor=colour, strokeColor=None))
        drawing.add(String(label_w - 6, y + 2, str(row.get("name") or row.get("symbol"))[:16],
                           textAnchor="end", fontSize=6.4, fillColor=ink))
        drawing.add(String(centre + plot_w / 2 + 4, y + 2, f"{change:+.2f}%",
                           fontSize=6.4, fillColor=colour))
    return drawing


def _hourly_drawing(peaks: Sequence[dict[str, Any]], width: float = 480.0,
                    height: float = 130.0) -> Any:
    """Columns: traded volume per IST hour, with the hour range drawn on top."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors

    drawing = Drawing(width, height)
    slate = colors.HexColor("#64748b")
    grid = colors.HexColor("#e3e9f1")
    teal = colors.HexColor("#0f766e")
    amber = colors.HexColor("#b45309")

    data = [p for p in peaks if float(p.get("volume") or 0) > 0][:10] or list(peaks)[:10]
    if not data:
        drawing.add(String(width / 2, height / 2, "no intraday history available",
                           textAnchor="middle", fontSize=8, fillColor=slate))
        return drawing

    pad_l, pad_r, pad_t, pad_b = 34.0, 16.0, 16.0, 16.0
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    max_volume = max((float(p.get("volume") or 0) for p in data), default=1.0) or 1.0
    max_span = max((float(p.get("span_pct") or 0) for p in data), default=1.0) or 1.0
    slot = plot_w / len(data)
    bar_w = max(6.0, slot * 0.5)
    axis = pad_t + 2.0

    drawing.add(Line(pad_l, axis, pad_l + plot_w, axis, strokeColor=grid, strokeWidth=0.6))
    points: list[tuple[float, float]] = []
    for index, peak in enumerate(data):
        x = pad_l + slot * index + slot / 2
        volume = float(peak.get("volume") or 0)
        bar_h = (volume / max_volume) * (plot_h - 10)
        drawing.add(Rect(x - bar_w / 2, axis, bar_w, max(0.8, bar_h), fillColor=teal, strokeColor=None))
        span_y = axis + (float(peak.get("span_pct") or 0) / max_span) * (plot_h - 10)
        points.append((x, span_y))
        drawing.add(String(x, 3, str(peak.get("label") or "")[:5], textAnchor="middle",
                           fontSize=6.2, fillColor=slate))
        drawing.add(String(x, axis + bar_h + 2, f"{float(peak.get('move_pct') or 0):+.2f}%",
                           textAnchor="middle", fontSize=5.8, fillColor=slate))
    for index in range(1, len(points)):
        x0, y0 = points[index - 1]
        x1, y1 = points[index]
        drawing.add(Line(x0, y0, x1, y1, strokeColor=amber, strokeWidth=1.2))
    for x, y in points:
        drawing.add(Rect(x - 1.6, y - 1.6, 3.2, 3.2, fillColor=amber, strokeColor=None))
    drawing.add(String(2, axis + plot_h - 14, "volume", fontSize=5.8, fillColor=teal))
    drawing.add(String(2, axis + plot_h - 24, "range %", fontSize=5.8, fillColor=amber))
    return drawing


def _reference_drawing(row: dict[str, Any], width: float = 480.0, height: float = 96.0) -> Any:
    """Price vs VWAP vs ATR band — drawn from the sweep when OHLCV is missing."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors

    drawing = Drawing(width, height)
    slate = colors.HexColor("#64748b")
    teal = colors.HexColor("#0f766e")
    band = colors.HexColor("#eef4f3")
    ink = colors.HexColor("#0b1220")

    cmp_ = float(row.get("cmp") or 0)
    vwap = float(row.get("vwap") or 0) or cmp_
    atr = float(row.get("atr") or 0) or max(cmp_ * 0.01, 0.01)
    if cmp_ <= 0:
        drawing.add(String(width / 2, height / 2, "no usable price for this instrument",
                           textAnchor="middle", fontSize=8, fillColor=slate))
        return drawing

    low = min(cmp_, vwap) - 1.6 * atr
    high = max(cmp_, vwap) + 1.6 * atr
    span = (high - low) or 1.0
    pad_l, pad_r = 10.0, 58.0
    plot_w = width - pad_l - pad_r

    def y_of(price: float) -> float:
        return 14 + (price - low) / span * (height - 32)

    drawing.add(Rect(pad_l, y_of(vwap - atr), plot_w, max(1.0, y_of(vwap + atr) - y_of(vwap - atr)),
                     fillColor=band, strokeColor=None))
    drawing.add(Line(pad_l, y_of(vwap), pad_l + plot_w, y_of(vwap), strokeColor=teal,
                      strokeWidth=1.0, strokeDashArray=[3, 2]))
    drawing.add(String(width - pad_r + 3, y_of(vwap) - 2, f"VWAP {vwap:,.2f}", fontSize=6.2,
                       fillColor=teal))
    marker = y_of(cmp_)
    drawing.add(Rect(pad_l + plot_w * 0.62, marker - 1.6, 8, 3.2, fillColor=ink, strokeColor=None))
    drawing.add(String(width - pad_r + 3, marker - 2, f"LTP {cmp_:,.2f}", fontSize=6.2, fillColor=ink))
    drawing.add(String(pad_l, 3, f"ATR band {vwap - atr:,.2f} – {vwap + atr:,.2f} (no intraday "
                                f"OHLCV for this instrument)", fontSize=6.0, fillColor=slate))
    return drawing


def derive_plan(row: dict[str, Any]) -> dict[str, Any]:
    """A plan from the sweep itself, used when the sentinel has no stored plan."""
    price = float(row.get("cmp") or 0)
    atr = float(row.get("atr") or (price * 0.01))
    verdict = str(row.get("verdict") or "").upper()
    bias = str(row.get("bias") or "").upper()
    if verdict not in {"BUY", "SELL"}:
        return {}
    sign = 1.0 if verdict == "BUY" else -1.0
    entry_low = price - sign * 0.10 * atr
    entry_high = price + sign * 0.10 * atr
    stop = max(price - 1.1 * atr, price - price * 0.03) if sign > 0 else min(
        price + 1.1 * atr, price + price * 0.03
    )
    risk = abs(price - stop) or atr or 1.0
    return {
        "verdict": verdict,
        "bias": bias,
        "entry_low": round(min(entry_low, entry_high), 2),
        "entry_high": round(max(entry_low, entry_high), 2),
        "stop_loss": round(stop, 2),
        "target_1": round(price + sign * 2 * risk, 2),
        "target_2": round(price + sign * 3 * risk, 2),
        "risk_reward": 2.0,
        "derived": True,
    }


def report_filename(when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H%M")
    return f"TradeVMx_Market_Report_{stamp}.pdf"


def build_session_report(
    context: dict[str, Any],
    *,
    out_dir: Path,
    bars_by_symbol: dict[str, Sequence[dict[str, Any]]] | None = None,
    plans: dict[str, dict[str, Any]] | None = None,
    commentary: str = "",
    owner: str = "",
    licence: str = LICENCE_ID,
    now: datetime | None = None,
    ai_source: str = "",
) -> dict[str, Any]:
    """A long-form, charted market report (typically 12-18 pages).

    Sections: cover, executive summary, breadth internals, prediction table, one
    charted page per instrument, indicator methodology, and data sources with the
    licence/disclaimer block.  Every page carries the logo footer, licence id and
    copyright line.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            HRFlowable,
            Image,
            KeepTogether,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("reportlab is required for PDF export") from exc

    when = now or datetime.now()
    rows = [r for r in (context.get("rows") or []) if isinstance(r, dict)]
    plans = plans or {}
    bars_by_symbol = bars_by_symbol or {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = report_filename(when)
    path = out_dir / filename

    ink = colors.HexColor("#0b1220")
    teal = colors.HexColor("#0f766e")
    slate = colors.HexColor("#64748b")
    line = colors.HexColor("#d7dee8")
    panel = colors.HexColor("#f5f8fb")
    green = colors.HexColor("#12805c")
    red = colors.HexColor("#b3261e")

    styles = getSampleStyleSheet()
    cover_title = ParagraphStyle("RTitle", parent=styles["Title"], fontName="Helvetica-Bold",
                                 fontSize=26, leading=30, textColor=ink, alignment=TA_LEFT, spaceAfter=4)
    cover_sub = ParagraphStyle("RSub", parent=styles["Normal"], fontName="Helvetica", fontSize=11,
                               leading=15, textColor=slate)
    h1 = ParagraphStyle("RH1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=14,
                        leading=18, textColor=teal, spaceBefore=4, spaceAfter=6)
    h2 = ParagraphStyle("RH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11,
                        leading=14.5, textColor=ink, spaceBefore=8, spaceAfter=4)
    body = ParagraphStyle("RBody", parent=styles["Normal"], fontName="Helvetica", fontSize=9.4,
                          leading=14, textColor=ink)
    small = ParagraphStyle("RSmall", parent=styles["Normal"], fontName="Helvetica", fontSize=8.2,
                           leading=11.6, textColor=slate)
    cell = ParagraphStyle("RCell", parent=styles["Normal"], fontName="Helvetica", fontSize=7.8,
                          leading=10.2, textColor=ink)
    cell_head = ParagraphStyle("RCellHead", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)

    def table_style(header_rows: int = 1) -> TableStyle:
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, header_rows - 1), teal),
            ("ROWBACKGROUNDS", (0, header_rows), (-1, -1), [colors.white, panel]),
            ("GRID", (0, 0), (-1, -1), 0.3, line),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ])

    scanned = int(context.get("scanned") or len(rows) or 0)
    actionable = int(context.get("actionable") or 0)
    above = [r for r in rows if float(r.get("cmp") or 0) > float(r.get("vwap") or 0)]
    bullish = [r for r in rows if str(r.get("bias", "")).upper() == "BULLISH"]
    bearish = [r for r in rows if str(r.get("bias", "")).upper() == "BEARISH"]
    ranked = sorted(rows, key=lambda r: abs(float(r.get("change_pct") or 0)), reverse=True)
    deep_dive = ranked[:10]
    hourly = session_hourly_map(rows, bars_by_symbol)
    peak_hour = max(hourly, key=lambda h: float(h.get("volume") or 0), default=None)

    def plan_for(row: dict[str, Any]) -> dict[str, Any]:
        """The stored sentinel plan, else one derived from the sweep row."""
        plan = plans.get(str(row.get("symbol"))) or {}
        if plan.get("verdict") and plan.get("stop_loss"):
            return plan
        derived = derive_plan(row)
        return {**derived, **{k: v for k, v in plan.items() if v}} if plan else derived
    when_label = when.strftime("%A, %d %B %Y · %H:%M IST")

    story: list[Any] = []

    # ---------------------------------------------------------------- cover
    logo = None
    if LOGO_PATH.exists():
        try:
            from PIL import Image as _PILImage

            with _PILImage.open(LOGO_PATH) as probe:
                probe.verify()
            logo = LOGO_PATH
        except Exception:  # noqa: BLE001
            logo = None
    story.append(Spacer(1, 16 * mm))
    if logo is not None:
        try:
            story.append(Image(str(logo), width=30 * mm, height=30 * mm, kind="proportional"))
            story.append(Spacer(1, 10))
        except Exception:  # noqa: BLE001
            pass
    story.append(Paragraph(f"{PRODUCT} · {TAGLINE}", cover_sub))
    story.append(Paragraph("Daily Market Report", cover_title))
    story.append(Paragraph(when_label + (f" · prepared for {owner}" if owner else ""), cover_sub))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1.2, color=teal, spaceAfter=10))

    kpis = Table(
        [
            ["Instruments", "Actionable", "Above VWAP", "Breadth"],
            [
                str(scanned),
                str(actionable),
                f"{len(above)}/{len(rows)}" if rows else "—",
                f"{len(bullish)} up / {len(bearish)} down",
            ],
        ],
        colWidths=[42 * mm, 42 * mm, 42 * mm, 44 * mm],
    )
    kpis.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4f3")),
        ("TEXTCOLOR", (0, 0), (-1, 0), slate),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.6),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 12.5),
        ("TEXTCOLOR", (0, 1), (-1, 1), ink),
        ("GRID", (0, 0), (-1, -1), 0.4, line),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpis)
    story.append(Spacer(1, 12))
    story.append(Paragraph("Inside this report", h2))
    contents = [
        "1. Executive summary — the session in one page",
        "2. Hour by hour — which hour carried the day, and where volume peaked",
        "3. Market internals — breadth, structure and volume for every tracked name",
        "4. Predictions — entry band, invalidation and targets per instrument",
        f"5. Charted deep dives — {len(deep_dive)} instruments, one page each",
        "6. Session playbook — risk rules, sizing and what would void the read",
        "7. Indicator methodology — how every number is computed",
        "8. Data sources, freshness and disclaimer",
    ]
    for item in contents:
        story.append(Paragraph(item, body))
    story.append(Spacer(1, 14))
    story.append(Paragraph(
        f"Licence {licence} · {COPYRIGHT}", small))
    story.append(Paragraph(
        "Research document, not investment advice. Prices are exchange-delayed or "
        "vendor-delayed snapshots; every figure is labelled with its source.", small))
    story.append(PageBreak())

    # ------------------------------------------------------ executive summary
    story.append(Paragraph("1. Executive summary", h1))
    if commentary:
        for para in [p.strip() for p in str(commentary).split("\n") if p.strip()][:10]:
            story.append(Paragraph(para.replace("&", "&amp;"), body))
            story.append(Spacer(1, 4))
    else:
        leader_names = ", ".join(r.get("name", "") for r in bullish[:4]) or "none"
        laggard_names = ", ".join(r.get("name", "") for r in bearish[:4]) or "none"
        setups = [r for r in rows if str(plan_for(r).get("verdict") or "").upper() in {"BUY", "SELL"}]
        story.append(Paragraph(
            f"The desk swept {scanned} instruments and found {actionable} setups above the "
            f"alert severity floor. {len(above)} of {len(rows)} names trade above session VWAP, "
            f"which is the cleanest single measure of intraday demand. Breadth reads "
            f"{len(bullish)} bullish against {len(bearish)} bearish.", body))
        story.append(Spacer(1, 5))
        story.append(Paragraph(
            f"<b>Leadership:</b> {leader_names}. <b>Laggards:</b> {laggard_names}. "
            f"Instruments carrying an explicit directional verdict: "
            f"{', '.join(r.get('name', '') for r in setups[:6]) or 'none in this sweep'}.", body))
        if ranked:
            top = ranked[0]
            story.append(Paragraph(
                f"<b>Largest move:</b> {top.get('name')} ({top.get('symbol')}) printing "
                f"{_fmt(top.get('change_pct'))}% at {_fmt(top.get('cmp'))}, "
                f"structure {top.get('structure') or '—'}, "
                f"VWAP {_fmt(top.get('vwap'))}.", body))
    story.append(Paragraph("What matters today", h2))
    observations = []
    if rows:
        observations.append(
            f"{len(above)}/{len(rows)} instruments are holding above VWAP; sustained breadth "
            "above 60% is the desk's filter for trend days rather than chop.")
    if actionable:
        observations.append(
            f"{actionable} setups cleared the severity floor. Only these qualify for a written "
            "plan with entry, invalidation and targets.")
    if deep_dive:
        observations.append(
            f"Charted deep dives cover {len(deep_dive)} names, chosen by absolute move so the "
            "report leads with where the risk actually moved.")
    observations.append(
        "Risk into the next session is defined by the invalidation level on each plan, not by "
        "the direction of the index print.")
    for note in observations:
        story.append(Paragraph("• " + note, body))
        story.append(Spacer(1, 3))
    if ranked:
        story.append(Paragraph("Top movers at a glance", h2))
        movers = [[
            Paragraph("Instrument", cell_head), Paragraph("CMP", cell_head),
            Paragraph("Move", cell_head), Paragraph("vs VWAP", cell_head),
            Paragraph("Structure", cell_head), Paragraph("Verdict", cell_head),
        ]]
        for row in ranked[:6]:
            cmp_ = float(row.get("cmp") or 0)
            vwap = float(row.get("vwap") or 0)
            gap = ((cmp_ - vwap) / vwap * 100) if vwap else 0.0
            movers.append([
                Paragraph(str(row.get("name") or row.get("symbol") or ""), cell),
                Paragraph(_fmt(cmp_), cell),
                Paragraph(f"{float(row.get('change_pct') or 0):+.2f}%", cell),
                Paragraph(f"{gap:+.2f}%", cell),
                Paragraph(str(row.get("structure") or "—"), cell),
                Paragraph(str(row.get("verdict") or (plan_for(row).get("verdict")) or "watch"), cell),
            ])
        movers_table = Table(movers, colWidths=[46 * mm, 26 * mm, 24 * mm, 26 * mm, 28 * mm, 24 * mm],
                             repeatRows=1)
        movers_table.setStyle(table_style())
        story.append(movers_table)
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "The movers table ranks by absolute change, because that is where risk and opportunity "
            "were actually repriced this session. A name can lead the table and still be a poor "
            "trade: check its VWAP position and structure in section 3 and its plan in section 4 "
            "before acting on any of it.", small))
    story.append(Spacer(1, 10))

    # ------------------------------------------------------- hourly peaks
    story.append(Paragraph("2. Hour by hour — where the session peaked", h1))
    story.append(Paragraph(
        "Each column aggregates every instrument's bars into IST clock hours: the teal column "
        "is traded volume, the amber column is the average high-low range. Read it to see which "
        "hour carried the day, then check the table for the instrument that made the move.", small))
    story.append(Spacer(1, 6))
    if hourly:
        story.append(_hourly_drawing(hourly, width=174 * mm, height=72 * mm))
        story.append(Spacer(1, 8))
        hour_table = [[
            Paragraph("IST hour", cell_head), Paragraph("Traded volume", cell_head),
            Paragraph("Avg range %", cell_head), Paragraph("Biggest mover", cell_head),
            Paragraph("Move", cell_head), Paragraph("High", cell_head),
        ]]
        for slot in hourly:
            leader = slot.get("leader") or {}
            hour_table.append([
                Paragraph(slot.get("label") or "—", cell),
                Paragraph(f"{float(slot.get('volume') or 0):,.0f}", cell),
                Paragraph(f"{float(slot.get('span_pct') or 0):.2f}%", cell),
                Paragraph(str(leader.get("name") or "—"), cell),
                Paragraph(f"{float(leader.get('move_pct') or 0):+.2f}%" if leader else "—", cell),
                Paragraph(_fmt(leader.get("high")) if leader else "—", cell),
            ])
        table = Table(hour_table, colWidths=[26 * mm, 34 * mm, 26 * mm, 42 * mm, 22 * mm, 24 * mm],
                      repeatRows=1)
        table.setStyle(table_style())
        story.append(table)
        if peak_hour:
            story.append(Spacer(1, 8))
            story.append(Paragraph(
                f"<b>Peak hour:</b> {peak_hour.get('label')} IST traded the most volume "
                f"({float(peak_hour.get('volume') or 0):,.0f} shares/contracts across the tracked "
                f"names) with the widest average range "
                f"({float(peak_hour.get('span_pct') or 0):.2f}%). "
                + (
                    f"The move inside that hour belonged to "
                    f"{(peak_hour.get('leader') or {}).get('name')} at "
                    f"{float((peak_hour.get('leader') or {}).get('move_pct') or 0):+.2f}%."
                    if peak_hour.get("leader") else ""
                ), body))
    else:
        story.append(Paragraph(
            "Intraday OHLCV was not returned for any instrument when this report was built, so "
            "the hour-by-hour profile is unavailable. The session map below still shows every "
            "instrument's move, and the deep dives fall back to a VWAP/ATR reference chart. "
            "Rebuild the report while the market is open to get the hourly detail.", body))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Session map — every instrument's move", h2))
    story.append(_session_map_drawing(rows, width=174 * mm, height=68 * mm))
    story.append(Paragraph(
        "Green bars are advancing instruments, red bars declining; length is the size of the move. "
        "This chart is drawn from the sweep itself and needs no vendor history.", small))
    story.append(Spacer(1, 10))

    # ------------------------------------------------------- market internals
    story.append(Paragraph("3. Market internals", h1))
    story.append(Paragraph(
        "Every tracked instrument with the readings the desk uses: last price, change, "
        "directional bias, swing structure, distance from VWAP, ATR and volume versus its "
        "own median.", small))
    story.append(Spacer(1, 6))
    if rows:
        internals = [[
            Paragraph("Instrument", cell_head), Paragraph("CMP", cell_head),
            Paragraph("Chg %", cell_head), Paragraph("Bias", cell_head),
            Paragraph("Structure", cell_head), Paragraph("VWAP", cell_head),
            Paragraph("vs VWAP", cell_head), Paragraph("ATR", cell_head),
            Paragraph("Vol x", cell_head),
        ]]
        for row in rows:
            vwap = float(row.get("vwap") or 0)
            cmp_ = float(row.get("cmp") or 0)
            gap = ((cmp_ - vwap) / vwap * 100) if vwap else 0.0
            internals.append([
                Paragraph(f"{row.get('name') or ''}<br/><font size=6.4 color='#64748b'>{row.get('symbol') or ''}</font>", cell),
                Paragraph(_fmt(row.get("cmp")), cell),
                Paragraph(_fmt(row.get("change_pct")), cell),
                Paragraph(str(row.get("bias") or "—"), cell),
                Paragraph(str(row.get("structure") or "—"), cell),
                Paragraph(_fmt(row.get("vwap")), cell),
                Paragraph(f"{gap:+.2f}%", cell),
                Paragraph(_fmt(row.get("atr")), cell),
                Paragraph(_fmt(row.get("volume_ratio"), 2), cell),
            ])
        internals_table = Table(
            internals,
            colWidths=[40 * mm, 19 * mm, 16 * mm, 20 * mm, 21 * mm, 19 * mm, 17 * mm, 15 * mm, 13 * mm],
            repeatRows=1,
        )
        internals_table.setStyle(table_style())
        story.append(internals_table)
    else:
        story.append(Paragraph("No sweep is stored yet — arm the desk and re-run.", body))
    story.append(PageBreak())

    # ----------------------------------------------------------- predictions
    story.append(Paragraph("4. Predictions and trade plans", h1))
    story.append(Paragraph(
        "Plans are produced by the desk engine: ATR-based entry bands, invalidation at the "
        "swing that would break the thesis, and two targets. Risk-reward is measured against "
        "target 1. The AI narrative source, when one was used, is named in section 8.", small))
    story.append(Spacer(1, 6))
    plan_rows = [r for r in rows if plan_for(r)]
    if plan_rows:
        pred = [[
            Paragraph("Instrument", cell_head), Paragraph("Verdict", cell_head),
            Paragraph("Entry band", cell_head), Paragraph("Stop", cell_head),
            Paragraph("Target 1", cell_head), Paragraph("Target 2", cell_head),
            Paragraph("R:R", cell_head),
        ]]
        for row in plan_rows:
            plan = plan_for(row)
            entry = "—"
            if plan.get("entry_low") is not None and plan.get("entry_high") is not None:
                entry = f"{_fmt(plan.get('entry_low'))} – {_fmt(plan.get('entry_high'))}"
            rr = plan.get("risk_reward")
            pred.append([
                Paragraph(f"{row.get('name') or ''}<br/><font size=6.4 color='#64748b'>{row.get('symbol') or ''}</font>", cell),
                Paragraph(str(plan.get("verdict") or row.get("verdict") or "—"), cell),
                Paragraph(entry, cell),
                Paragraph(_fmt(plan.get("stop_loss")), cell),
                Paragraph(_fmt(plan.get("target_1")), cell),
                Paragraph(_fmt(plan.get("target_2")), cell),
                Paragraph(f"1:{float(rr):.2f}" if isinstance(rr, (int, float)) else "—", cell),
            ])
        pred_table = Table(
            pred,
            colWidths=[46 * mm, 20 * mm, 34 * mm, 22 * mm, 22 * mm, 22 * mm, 16 * mm],
            repeatRows=1,
        )
        pred_table.setStyle(table_style())
        story.append(pred_table)
        story.append(Spacer(1, 10))
        story.append(Paragraph("Why these plans", h2))
        for row in plan_rows[:6]:
            plan = plan_for(row)
            vwap = float(row.get("vwap") or 0)
            cmp_ = float(row.get("cmp") or 0)
            gap = ((cmp_ - vwap) / vwap * 100) if vwap else 0.0
            side = str(plan.get("verdict") or "").upper()
            reason = (
                f"<b>{row.get('name')}</b> — {side} because price is "
                f"{'above' if gap >= 0 else 'below'} VWAP by {abs(gap):.2f}% with "
                f"{row.get('structure') or 'range'} structure and "
                f"{_fmt(row.get('volume_ratio'), 2)}x median volume. "
                f"Invalidation sits at {_fmt(plan.get('stop_loss'))}, which is "
                f"{abs(cmp_ - float(plan.get('stop_loss') or cmp_)):.2f} points and "
                f"{(abs(cmp_ - float(plan.get('stop_loss') or cmp_)) / cmp_ * 100) if cmp_ else 0:.2f}% "
                f"away; target 1 pays {abs(float(plan.get('target_1') or cmp_) - cmp_):.2f} points."
            )
            story.append(Paragraph(reason, body))
            story.append(Spacer(1, 4))
    else:
        story.append(Paragraph(
            "No plan cleared the severity floor in this sweep. The internals table above still "
            "shows structure and VWAP position for every name.", body))
    story.append(PageBreak())

    # ------------------------------------------------------------- deep dives    story.append(Paragraph("5. Charted deep dives", h1))
    story.append(Paragraph(
        "One page per instrument: the intraday candles the desk actually read, the indicator "
        "panel behind the verdict, the live signals, and the plan with its invalidation. "
        "Where the vendor returned no OHLCV, the page falls back to a VWAP/ATR reference "
        "chart drawn from the sweep itself, so the report never ships a blank chart.", small))
    story.append(Spacer(1, 6))
    if not deep_dive:
        story.append(Paragraph(
            "No instruments were tracked when this report was built. Arm the desk, run a scan, "
            "then rebuild the report.", body))
        story.append(PageBreak())
    for row in deep_dive:
        symbol = str(row.get("symbol"))
        plan = plan_for(row)
        bars = bars_by_symbol.get(symbol) or []
        story.append(Paragraph(
            f"{row.get('name') or symbol} <font size=9 color='#64748b'>({symbol})</font>", h1))
        story.append(Paragraph(
            f"{_fmt(row.get('cmp'))} · {_fmt(row.get('change_pct'))}% · bias "
            f"{row.get('bias') or '—'} · structure {row.get('structure') or '—'} · "
            f"source {row.get('ltp_source') or 'desk sweep'}", small))
        story.append(Spacer(1, 6))
        if len(bars) >= 3:
            story.append(_candle_drawing(bars, width=174 * mm, height=84 * mm))
            story.append(Spacer(1, 4))
            story.append(Paragraph(
                f"{len(bars)} bars · timeframe {context.get('timeframe') or '15m'} · "
                "candles with the desk's volume panel below price.", small))
            peaks = hourly_peaks(bars)
            if peaks:
                loudest = max(peaks, key=lambda p: abs(float(p.get("move_pct") or 0)))
                busiest = max(peaks, key=lambda p: float(p.get("volume") or 0))
                story.append(Paragraph(
                    f"<b>Peak hour in this instrument:</b> {loudest['label']} IST moved "
                    f"{float(loudest['move_pct']):+.2f}% between "
                    f"{_fmt(loudest['low'])} and {_fmt(loudest['high'])}; volume peaked at "
                    f"{busiest['label']} IST. That is the hour the desk would have transacted "
                    f"this name.", body))
        else:
            story.append(_reference_drawing(row, width=174 * mm, height=64 * mm))
            story.append(Paragraph(
                "No intraday OHLCV was returned for this instrument at build time, so this page "
                "shows the price/VWAP/ATR reference band from the live sweep instead of candles. "
                "The indicator panel and plan below are unchanged.", small))
        story.append(Spacer(1, 8))
        story.append(Paragraph("Indicator panel", h2))
        indicator_rows = [
            ["Reading", "Value", "Reading", "Value"],
            ["Last price", _fmt(row.get("cmp")), "Change", f"{_fmt(row.get('change_pct'))}%"],
            ["Bias", str(row.get("bias") or "—"), "Structure", str(row.get("structure") or "—")],
            ["Session VWAP", _fmt(row.get("vwap")),
             "Distance to VWAP", f"{((float(row.get('cmp') or 0) - float(row.get('vwap') or 0)) / float(row.get('vwap') or 1) * 100):+.2f}%"],
            ["ATR (14)", _fmt(row.get("atr")), "Volume vs median", _fmt(row.get("volume_ratio"), 2) + "x"],
            ["Verdict", str(plan.get("verdict") or row.get("verdict") or "—"),
             "Risk-reward", f"1:{float(plan.get('risk_reward')):.2f}" if isinstance(plan.get("risk_reward"), (int, float)) else "—"],
        ]
        indicator_table = Table(indicator_rows, colWidths=[34 * mm, 36 * mm, 34 * mm, 36 * mm])
        indicator_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef4f3")),
            ("TEXTCOLOR", (0, 0), (-1, 0), slate),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.3, line),
            ("TEXTCOLOR", (0, 1), (0, -1), slate),
            ("TEXTCOLOR", (2, 1), (2, -1), slate),
            ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
            ("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(indicator_table)

        signals = row.get("signals") or []
        story.append(Paragraph("Live signals", h2))
        if signals:
            for sig in signals[:4]:
                story.append(Paragraph(
                    f"• <b>{sig.get('label')}</b> ({sig.get('timeframe')}, severity "
                    f"{sig.get('severity')}/5) — {sig.get('detail') or ''}", body))
                story.append(Spacer(1, 2))
        else:
            story.append(Paragraph("No qualifying VWAP, volume or structure event is live.", body))

        story.append(Paragraph("Plan", h2))
        if plan:
            entry = "—"
            if plan.get("entry_low") is not None and plan.get("entry_high") is not None:
                entry = f"{_fmt(plan.get('entry_low'))} – {_fmt(plan.get('entry_high'))}"
            story.append(Paragraph(
                f"Verdict <b>{plan.get('verdict') or '—'}</b> · entry band <b>{entry}</b> · "
                f"invalidation <b>{_fmt(plan.get('stop_loss'))}</b> · "
                f"targets <b>{_fmt(plan.get('target_1'))}</b> / <b>{_fmt(plan.get('target_2'))}</b>. "
                f"The plan is void on a close beyond the invalidation level, and position size is "
                f"set from that distance, not from conviction.", body))
        else:
            story.append(Paragraph(
                "No written plan in this sweep: the instrument did not clear the desk severity "
                "floor, so it is monitored rather than traded.", body))
        story.append(PageBreak())

    # -------------------------------------------------------------- playbook
    story.append(Paragraph("6. Session playbook and risk rules", h1))
    story.append(Paragraph(
        "The report is only useful if the desk follows the same rules every day. These are the "
        "rules this document was produced under.", small))
    rules = [
        ("Risk per idea", "The invalidation distance defines risk. Size the position so a full "
                          "stop costs the same small fraction of the account on every trade; "
                          "never widen the stop to avoid being stopped."),
        ("Entry discipline", "Plans are written as a band, not a price. If price never trades "
                             "into the band, there is no trade - chasing a missed band is not a "
                             "setup."),
        ("Invalidation", "A close beyond the stated stop voids the thesis, not a wick. Re-entry "
                         "requires a fresh structure and a fresh plan."),
        ("Confirmation", "VWAP position and structure must agree with the verdict. When they "
                         "disagree, the instrument is monitored, not traded."),
        ("Event risk", "No new intraday position is carried into a scheduled event, an earnings "
                       "print, or a gap through the invalidation level."),
        ("Review", "At the close, every plan is marked against the session: did it trigger, did "
                   "it hit target or stop, and what would have been done differently."),
    ]
    for name, text in rules:
        story.append(Paragraph(name, h2))
        story.append(Paragraph(text, body))
    story.append(Paragraph("What would change this read", h2))
    change_notes = []
    if rows:
        change_notes.append(
            f"A decisive close below VWAP for the {len(above)} of {len(rows)} names currently "
            "above it would flip the breadth read within an hour.")
    if bearish:
        change_notes.append(
            "The laggards are " + ", ".join(r.get("name", "") for r in bearish[:3]) +
            "; a squeeze there against the invalidation levels would signal short-covering "
            "rather than fresh demand.")
    change_notes.append(
        "A session that prints outside the projected ATR band means the volatility estimate was "
        "too low: re-check the ATR before honouring any target in this document.")
    for note in change_notes:
        story.append(Paragraph("• " + note, body))
        story.append(Spacer(1, 3))
    story.append(PageBreak())

    # ------------------------------------------------------------ methodology
    story.append(Paragraph("7. Indicator methodology", h1))
    method = [
        ("VWAP", "Session volume-weighted average price, anchored at the 09:15 IST open. "
                  "Price above VWAP means buyers have paid up on volume; the desk treats the "
                  "VWAP reclaim or rejection as the primary intraday signal."),
        ("ATR (14)", "Average true range over 14 bars. It sets the width of the entry band and "
                     "the stop distance, so a volatile name automatically gets a wider plan."),
        ("Structure", "Swing labelling from fractal highs and lows: HH/HL is an uptrend, LH/LL a "
                      "downtrend, and mixed labels mean a range. Structure is what invalidates a "
                      "bias, not a single candle."),
        ("Volume ratio", "Last bar volume divided by the median volume of the lookback window. "
                          "Above 1.5x counts as participation; below 0.7x is drift the desk ignores."),
        ("Severity (1-5)", "How many independent conditions agree at once: VWAP position, structure, "
                            "volume and momentum. The alert floor defaults to 3, so a headline only "
                            "fires when more than one measure agrees."),
        ("Bias", "The directional read after combining VWAP position, structure and momentum. "
                 "Bias is a filter, not an entry: the entry comes from the plan."),
        ("Verdict", "BUY or SELL only when a plan exists with a defined invalidation; otherwise the "
                    "instrument stays on watch."),
        ("Risk-reward", "Distance to target 1 divided by distance to invalidation. The desk prefers "
                         "1:2 or better and rejects plans that need a stop inside normal noise."),
    ]
    for name, description in method:
        story.append(Paragraph(name, h2))
        story.append(Paragraph(description, body))
    story.append(Spacer(1, 10))

    # ---------------------------------------------------------------- sources
    story.append(Paragraph("8. Data sources, freshness and disclaimer", h1))
    story.append(Paragraph("Sources used to build this report", h2))
    ai_line = (
        f"<b>AI narrative</b> — {ai_source} supplied the written commentary for this document "
        "(the label comes from the desk, so this is what actually answered). No number in this "
        "report comes from the model: it only explains the data supplied to it."
        if ai_source else
        "<b>AI narrative</b> — not used. No Groq key was invokable when this report was built, so "
        "the deterministic rule engine wrote every sentence. Add a working GROQ_API_KEY in "
        "Configuration to get model-written commentary."
    )
    sources = [
        "<b>NSE India</b> (via nsepython) — live quotes, index levels and traded volume for "
        "NSE-listed instruments.",
        "<b>Yahoo Finance</b> (via yfinance) — OHLCV history for the charts and every derived "
        "indicator; the fallback when the exchange endpoint is rate-limited.",
        "<b>Desk engine</b> — VWAP, ATR, swing structure, volume ratio, severity scoring, entry "
        "bands, invalidation and targets, computed locally from the bars above.",
        ai_line,
        "<b>Report engine</b> — reportlab, rendered locally on this machine. Nothing is uploaded.",
    ]
    for item in sources:
        story.append(Paragraph("• " + item, body))
        story.append(Spacer(1, 3))
    story.append(Paragraph("Freshness", h2))
    story.append(Paragraph(
        f"Sweep timestamp: {context.get('time') or 'not recorded'} IST · market phase "
        f"{context.get('phase') or 'unknown'} · instruments in this document {len(rows)}. "
        "Vendor and exchange feeds can lag by seconds to minutes; a stale flag in the Studio "
        "means the last print could not be refreshed and the reading should be treated as "
        "indicative.", body))
    story.append(Paragraph("Disclaimer", h2))
    story.append(Paragraph(
        "This document is research output from a personal desk. It is not investment advice, "
        "not a recommendation to buy or sell, and it places no orders. Simulated fills in the "
        "paper account are not real trades. Markets can gap through any stated invalidation "
        "level. Verify every price against your broker before acting.", body))
    story.append(Paragraph(f"Licence {licence} · {COPYRIGHT}", small))

    def _decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.6)
        canvas.line(18 * mm, 16 * mm, width - 18 * mm, 16 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(slate)
        canvas.drawString(18 * mm, 11.4 * mm, f"{PRODUCT} v{_version()} · {COPYRIGHT}")
        canvas.drawCentredString(width / 2, 11.4 * mm, f"Licence {licence}")
        owner_bit = f"{owner} · " if owner else ""
        canvas.drawRightString(width - 18 * mm, 11.4 * mm, f"{owner_bit}Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=f"{PRODUCT} Daily Market Report {when:%Y-%m-%d}",
        author=owner or PRODUCT,
        subject="NSE market report",
    )
    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return {"path": str(path), "filename": filename,
            "bytes": path.stat().st_size if path.exists() else 0}


def ensure_autostart(enabled: bool, target: Path | None = None) -> dict[str, Any]:
    """Add or remove TradeVMx from the Windows startup folder."""
    target = Path(target or BASE_DIR)
    if os.name != "nt":
        return {
            "ok": False,
            "enabled": False,
            "platform": os.name,
            "detail": "Auto-start is Windows-only here; on Linux use install-autostart.sh.",
        }
    appdata = os.environ.get("APPDATA") or str(Path(os.path.expanduser("~") ) / "AppData" / "Roaming")
    startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    link = startup / "TradeVMx.lnk"
    try:
        startup.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "enabled": not enabled, "detail": f"Startup folder unavailable: {exc}"}

    if not enabled:
        try:
            if link.exists():
                link.unlink()
                return {"ok": True, "enabled": False, "detail": "Removed from Windows startup."}
        except OSError as exc:
            return {"ok": False, "enabled": True, "detail": f"Could not remove: {exc}"}
        return {"ok": True, "enabled": False, "detail": "Was not in Windows startup."}

    silent = target / "run_tradevmx_silent.vbs"
    launcher = target / "run_tradevmx.bat"
    if silent.exists():
        exe, args = "wscript.exe", f'"{silent}"'
    elif launcher.exists():
        exe, args = str(launcher), ""
    else:
        return {"ok": False, "enabled": False, "detail": "run_tradevmx.bat not found next to the app."}

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{link}'); "
        f"$s.TargetPath = '{exe}'; "
        f"$s.Arguments = '{args}'; "
        f"$s.WorkingDirectory = '{target}'; "
        "$s.Description = 'TradeVMx - start the desk at login'; "
        f"$s.IconLocation = '{(target / 'web' / 'static' / 'logo.ico')}'; "
        "$s.Save()"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0 and link.exists():
            return {"ok": True, "enabled": True, "path": str(link),
                    "detail": "TradeVMx will start automatically at Windows login."}
        return {"ok": False, "enabled": False,
                "detail": (completed.stderr or "PowerShell could not write the startup shortcut.").strip()[:220]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "enabled": False, "detail": f"Auto-start failed: {exc}"}


def licence_text() -> dict[str, str]:
    return {
        "product": PRODUCT,
        "tagline": TAGLINE,
        "licence": LICENCE_ID,
        "copyright": COPYRIGHT,
        "version": _version(),
    }


def _version() -> str:
    try:
        from version import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "0.0.0"
