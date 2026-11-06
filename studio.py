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


def search_instruments(
    query: str,
    *,
    limit: int = 12,
    watchlist: Sequence[Any] = (),
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
    "You are TradeVMx, a disciplined Indian equities desk assistant. Answer in "
    "short, factual paragraphs. Use only the supplied desk data for numbers; if "
    "something is not in the data, say so. No greetings, no markdown headings, "
    "no investment advice disclaimers longer than one sentence."
)


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


def _local_answer(question: str, context: dict[str, Any]) -> str:
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
    tail = (
        "Open a symbol from the search box for its chart, expected range and best "
        "entry window. Ask for a daily brief to export the full PDF."
    )
    return header + body + tail


def assistant_answer(
    question: str,
    context: dict[str, Any],
    *,
    api_key: str = "",
    model: str = "llama-3.3-70b-versatile",
    timeout: float = 40.0,
) -> dict[str, Any]:
    """Answer a desk question.  Groq first, deterministic digest as fallback."""
    question = (question or "").strip()
    if not question:
        return {
            "answer": "Ask about the tape, a symbol, the session so far, or request a daily brief.",
            "source": "local",
        }
    if api_key:
        try:
            from groq import Groq

            client = Groq(api_key=api_key, timeout=timeout)
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": "Desk data:\n"
                        + "\n".join(_context_lines(context))
                        + f"\n\nQuestion: {question}",
                    },
                ],
                temperature=0.2,
                max_tokens=700,
            )
            text = (completion.choices[0].message.content or "").strip()
            if text:
                return {"answer": text, "source": "groq", "model": model}
        except Exception as exc:  # noqa: BLE001 - never break the chamber
            LOGGER.warning("assistant Groq call failed: %s", exc)
            return {
                "answer": _local_answer(question, context),
                "source": "local",
                "note": f"Groq unavailable ({str(exc)[:120]}). Local digest used.",
            }
    return {"answer": _local_answer(question, context), "source": "local"}


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
