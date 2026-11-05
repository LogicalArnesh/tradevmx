"""TradeVMx :: Groq intelligence engine.

Turns a :class:`market_scanner.MarketSnapshot` into a disciplined institutional
trade plan (entry zone, invalidation, T1/T2, R:R) and enforces the desk's
risk rules *after* the model answers:

* a plan whose first target yields less than ``MIN_RISK_REWARD`` is voided,
* stops on the wrong side of the entry, or absurd stop distances, are voided,
* if Groq is unavailable or rate-limited, a deterministic ATR plan is produced
  so the desk never goes silent.

The prompt is intentionally terse and schema-bound: JSON only, no prose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from market_scanner import MarketSnapshot

LOGGER = logging.getLogger(__name__)

MAX_STOP_DISTANCE_PCT = 3.0  # invalidation further than this is not intraday
MIN_STOP_DISTANCE_PCT = 0.05  # tighter than this is noise, not risk

SYSTEM_PROMPT = """You are the head of an institutional intraday desk covering Indian cash equities and index futures (NSE). You write terse, tradeable notes. No greetings, no hedging language, no filler, no markdown.

Rules:
1. Use ONLY the supplied metrics. Never invent prices, news, fundamentals or option data.
2. Respect VWAP: longs require price above VWAP or a confirmed reclaim; shorts require price below VWAP or a confirmed rejection. If price is pinned to VWAP, prefer NO TRADE.
3. Structure and the signal tape decide direction; Open=High is supply, Open=Low is demand.
4. Invalidation must be a real structural level (order block edge, session extreme, pivot), not an arbitrary percentage.
5. Target 1 must give at least 1:2 reward-to-risk against the invalidation distance. Target 2 must be beyond Target 1.
6. If the evidence is contradictory, say NO TRADE. Capital preservation outranks activity.
7. Confidence is honest: 0.9+ only for clean multi-factor confluence.

Return a single JSON object, no code fences, exactly these keys:
{"bias": "BULLISH|BEARISH|NEUTRAL",
 "verdict": "BUY|SELL|NO TRADE",
 "entry_low": number,
 "entry_high": number,
 "stop_loss": number,
 "target_1": number,
 "target_2": number,
 "confidence": number,
 "horizon": "intraday",
 "rationale": ["max 4 short clauses"],
 "invalidation": "one sentence naming the level that kills the idea"}"""


@dataclass(frozen=True, slots=True)
class TradePlan:
    """Validated, desk-ready plan."""

    symbol: str
    display_name: str
    bias: str
    verdict: str
    entry_low: float
    entry_high: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_reward: float
    confidence: float
    horizon: str = "intraday"
    rationale: tuple[str, ...] = ()
    invalidation: str = ""
    model: str = "rule-engine"
    generated_at: str = ""
    is_fallback: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def actionable(self) -> bool:
        return self.verdict in {"BUY", "SELL"} and self.risk_reward >= 1.99

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rationale"] = list(self.rationale)
        payload["notes"] = list(self.notes)
        payload["actionable"] = self.actionable
        return payload


class GroqBrain:
    """Async Groq client with deterministic fallback."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Any | None = None
        self._client_error: str | None = None

    # ------------------------------------------------------------- client
    def _get_client(self) -> Any | None:
        if self._client is not None or self._client_error:
            return self._client
        if not self.settings.groq_api_key:
            self._client_error = "GROQ_API_KEY not configured"
            return None
        try:
            from groq import AsyncGroq

            self._client = AsyncGroq(
                api_key=self.settings.groq_api_key,
                timeout=self.settings.groq_timeout,
                max_retries=1,
            )
        except Exception as exc:  # pragma: no cover - import/config failure
            self._client_error = str(exc)
            LOGGER.error("Groq client unavailable: %s", exc)
        return self._client

    @property
    def available(self) -> bool:
        return bool(self.settings.groq_api_key)

    # ------------------------------------------------------------- prompt
    @staticmethod
    def _user_prompt(snapshot: "MarketSnapshot", min_rr: float) -> str:
        payload = snapshot.to_prompt_payload()
        return (
            f"Desk risk floor: reward-to-risk must be at least 1:{min_rr:.1f}.\n"
            f"Instrument metrics:\n{json.dumps(payload, separators=(',', ':'))}\n"
            "Produce the JSON trade plan now."
        )

    # --------------------------------------------------------------- plan
    async def plan(self, snapshot: "MarketSnapshot") -> TradePlan:
        """Return a validated plan; never raises."""
        client = self._get_client()
        if client is None:
            return self.fallback_plan(snapshot)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_prompt(snapshot, self.settings.min_risk_reward)},
        ]
        try:
            response = await client.chat.completions.create(
                model=self.settings.groq_model,
                messages=messages,
                temperature=0.15,
                max_tokens=700,
                top_p=0.9,
                response_format={"type": "json_object"},
            )
            content = (response.choices[0].message.content or "").strip()
            raw = _extract_json(content)
            plan = self._parse(raw, snapshot)
            LOGGER.info(
                "Groq plan for %s: %s RR=%.2f", snapshot.symbol, plan.verdict, plan.risk_reward
            )
            return plan
        except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the desk
            LOGGER.warning("Groq plan failed for %s (%s); using rule engine.", snapshot.symbol, exc)
            fallback = self.fallback_plan(snapshot)
            return TradePlan(**{**asdict(fallback), "notes": ("Groq unavailable: " + str(exc)[:160],)})

    # -------------------------------------------------------------- parse
    def _parse(self, raw: dict[str, Any], snapshot: "MarketSnapshot") -> TradePlan:
        def num(key: str, default: float) -> float:
            try:
                value = float(raw.get(key))
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) else default

        verdict = str(raw.get("verdict", "NO TRADE")).upper().strip()
        bias = str(raw.get("bias", snapshot.bias)).upper().strip()
        if verdict not in {"BUY", "SELL", "NO TRADE"}:
            verdict = "NO TRADE"
        if bias not in {"BULLISH", "BEARISH", "NEUTRAL"}:
            bias = snapshot.bias

        rationale_raw = raw.get("rationale") or []
        if isinstance(rationale_raw, str):
            rationale_raw = [rationale_raw]
        rationale = tuple(str(item).strip() for item in rationale_raw if str(item).strip())[:4]

        notes: list[str] = []
        entry_low = num("entry_low", snapshot.cmp)
        entry_high = num("entry_high", snapshot.cmp)
        if entry_low > entry_high:
            entry_low, entry_high = entry_high, entry_low
        stop = num("stop_loss", 0.0)
        t1 = num("target_1", 0.0)
        t2 = num("target_2", 0.0)
        confidence = min(max(num("confidence", 0.5), 0.0), 1.0)

        # --- coherence: entry zone must straddle price
        if not (entry_low * 0.97 <= snapshot.cmp <= entry_high * 1.03):
            notes.append("Entry zone did not bracket CMP; recentred on live price.")
            entry_low = round(snapshot.cmp - 0.1 * snapshot.atr, 2)
            entry_high = round(snapshot.cmp + 0.05 * snapshot.atr, 2)

        plan = TradePlan(
            symbol=snapshot.symbol,
            display_name=snapshot.display_name,
            bias=bias,
            verdict=verdict,
            entry_low=round(entry_low, 2),
            entry_high=round(entry_high, 2),
            stop_loss=round(stop, 2),
            target_1=round(t1, 2),
            target_2=round(t2, 2),
            risk_reward=0.0,
            confidence=round(confidence, 2),
            horizon=str(raw.get("horizon", "intraday"))[:20] or "intraday",
            rationale=rationale,
            invalidation=str(raw.get("invalidation", ""))[:200],
            model=self.settings.groq_model,
            generated_at=snapshot.as_of.isoformat(),
            is_fallback=False,
            notes=tuple(notes),
        )
        return self.enforce_discipline(plan, snapshot)

    # ---------------------------------------------------------- discipline
    def enforce_discipline(self, plan: TradePlan, snapshot: "MarketSnapshot") -> TradePlan:
        """Void or annotate plans that breach the risk contract."""
        entry_mid = plan.entry_mid
        notes = list(plan.notes)
        verdict = plan.verdict
        stop, t1, t2 = plan.stop_loss, plan.target_1, plan.target_2

        if verdict == "NO TRADE":
            return TradePlan(**{**asdict(plan), "risk_reward": 0.0, "notes": tuple(notes)})

        if entry_mid <= 0 or stop <= 0 or t1 <= 0 or t2 <= 0:
            notes.append("Rejected: plan contained non-positive levels.")
            return TradePlan(**{**asdict(plan), "verdict": "NO TRADE", "risk_reward": 0.0, "notes": tuple(notes)})

        is_long = verdict == "BUY"
        correct_sides = (stop < entry_mid and t1 > entry_mid and t2 > t1) if is_long else (
            stop > entry_mid and t1 < entry_mid and t2 < t1
        )
        if not correct_sides:
            notes.append("Rejected: stop/target on the wrong side of the entry.")
            return TradePlan(**{**asdict(plan), "verdict": "NO TRADE", "risk_reward": 0.0, "notes": tuple(notes)})

        risk = abs(entry_mid - stop)
        risk_pct = risk / entry_mid * 100.0
        if risk_pct < MIN_STOP_DISTANCE_PCT:
            notes.append(f"Rejected: invalidation only {risk_pct:.2f}% away (noise, not risk).")
            return TradePlan(**{**asdict(plan), "verdict": "NO TRADE", "risk_reward": 0.0, "notes": tuple(notes)})
        if risk_pct > MAX_STOP_DISTANCE_PCT:
            notes.append(f"Rejected: invalidation {risk_pct:.2f}% away exceeds intraday limit.")
            return TradePlan(**{**asdict(plan), "verdict": "NO TRADE", "risk_reward": 0.0, "notes": tuple(notes)})

        reward = abs(t1 - entry_mid)
        rr = reward / risk if risk else 0.0
        if rr < self.settings.min_risk_reward:
            notes.append(
                f"Rejected: T1 yields 1:{rr:.2f} against a {risk:.2f} point stop "
                f"(desk floor 1:{self.settings.min_risk_reward:.1f})."
            )
            return TradePlan(**{**asdict(plan), "verdict": "NO TRADE", "risk_reward": round(rr, 2), "notes": tuple(notes)})

        if plan.bias == "BULLISH" and not is_long:
            notes.append("Bias/verdict mismatch: bullish context but SELL plan.")
        if plan.bias == "BEARISH" and is_long:
            notes.append("Bias/verdict mismatch: bearish context but BUY plan.")

        return TradePlan(
            **{
                **asdict(plan),
                "verdict": verdict,
                "risk_reward": round(rr, 2),
                "notes": tuple(notes),
            }
        )

    # ----------------------------------------------------------- fallback
    def fallback_plan(self, snapshot: "MarketSnapshot") -> TradePlan:
        """Deterministic ATR plan used when the LLM is unavailable."""
        price = snapshot.cmp
        atr = snapshot.atr if snapshot.atr > 0 else price * 0.006
        bias = snapshot.bias
        bullish = bias == "BULLISH"
        bearish = bias == "BEARISH"

        if not (bullish or bearish) or price <= 0:
            return TradePlan(
                symbol=snapshot.symbol,
                display_name=snapshot.display_name,
                bias=bias,
                verdict="NO TRADE",
                entry_low=round(price, 2),
                entry_high=round(price, 2),
                stop_loss=0.0,
                target_1=0.0,
                target_2=0.0,
                risk_reward=0.0,
                confidence=0.0,
                rationale=(
                    "Structure and volume are not aligned; no edge worth risking capital.",
                    snapshot.bias_reason,
                ),
                invalidation="No position - reassess on a decisive VWAP break.",
                model="rule-engine",
                generated_at=snapshot.as_of.isoformat(),
                is_fallback=True,
                notes=("Deterministic fallback: no directional bias.",),
            )

        sign = 1.0 if bullish else -1.0
        entry_low = price - sign * 0.1 * atr
        entry_high = price + sign * 0.1 * atr
        stop = min(snapshot.day_low, price - 1.1 * atr) if bullish else max(
            snapshot.day_high, price + 1.1 * atr
        )
        # keep invalidation within the desk's intraday envelope
        max_risk = price * MAX_STOP_DISTANCE_PCT / 100.0
        if abs(price - stop) > max_risk:
            stop = price - sign * max_risk
        risk = abs(price - stop)
        target_1 = price + sign * 2.0 * risk
        target_2 = price + sign * 3.0 * risk

        rationale = [
            f"{'Bullish' if bullish else 'Bearish'} bias with price "
            f"{'above' if price > snapshot.vwap else 'below'} VWAP ({snapshot.vwap:.2f}).",
            f"Structure: {snapshot.structure}; ATR(14) {atr:.2f}.",
        ]
        if snapshot.signals:
            top = max(snapshot.signals, key=lambda s: s.severity)
            rationale.append(f"Primary trigger: {top.label} - {top.detail}.")

        plan = TradePlan(
            symbol=snapshot.symbol,
            display_name=snapshot.display_name,
            bias=bias,
            verdict="BUY" if bullish else "SELL",
            entry_low=round(min(entry_low, entry_high), 2),
            entry_high=round(max(entry_low, entry_high), 2),
            stop_loss=round(stop, 2),
            target_1=round(target_1, 2),
            target_2=round(target_2, 2),
            risk_reward=2.0,
            confidence=0.4,
            rationale=tuple(rationale),
            invalidation=(
                f"{'Close below' if bullish else 'Close above'} {stop:.2f} negates the setup."
            ),
            model="rule-engine",
            generated_at=snapshot.as_of.isoformat(),
            is_fallback=True,
            notes=("Deterministic ATR plan (Groq unavailable).",),
        )
        return self.enforce_discipline(plan, snapshot)

    # ------------------------------------------------------------- brief
    async def desk_note(self, snapshots: Sequence["MarketSnapshot"]) -> str:
        """Short institutional wrap-up for the EOD email; deterministic fallback."""
        if not snapshots:
            return "No instruments were scanned in this session."
        payload = [
            {
                "symbol": s.display_name,
                "change_pct": s.change_pct,
                "cmp": s.cmp,
                "vwap": s.vwap,
                "bias": s.bias,
                "structure": s.structure,
                "signals": [sig.label for sig in s.signals],
            }
            for s in snapshots
        ]
        client = self._get_client()
        if client is not None:
            try:
                response = await client.chat.completions.create(
                    model=self.settings.groq_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are an Indian market desk strategist. Write a tight "
                                "closing note in 4-6 sentences: breadth, leadership, VWAP "
                                "behaviour and what to watch tomorrow. No greetings, no "
                                "markdown, no disclaimers."
                            ),
                        },
                        {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
                    ],
                    temperature=0.2,
                    max_tokens=400,
                )
                text = (response.choices[0].message.content or "").strip()
                if text:
                    return text
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Groq desk note failed: %s", exc)

        bullish = [s.display_name for s in snapshots if s.bias == "BULLISH"]
        bearish = [s.display_name for s in snapshots if s.bias == "BEARISH"]
        above_vwap = sum(1 for s in snapshots if s.cmp > s.vwap)
        return (
            f"{above_vwap}/{len(snapshots)} instruments closed above session VWAP. "
            f"Leadership: {', '.join(bullish) if bullish else 'none'}. "
            f"Laggards: {', '.join(bearish) if bearish else 'none'}. "
            "Carry the strongest names on the desk watchlist tomorrow; avoid mean-reversion "
            "entries into low-volume drift."
        )


def _extract_json(content: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response, tolerating code fences."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("model response contained no JSON object") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model response was not a JSON object")
    return parsed
