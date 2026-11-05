# TradeXVm

An autonomous, Jarvis-style market desk for Indian markets (NSE/BSE, NIFTY 50,
BANK NIFTY and liquid large caps). It watches the tape on two timeframes, finds
the setups that actually matter, writes a disciplined Groq verdict, renders an
institutional PDF and pushes everything to Telegram and email — with no broker
credentials required.

```
09:15-15:30 IST, Mon-Fri   scan every 15 min  ->  alerts on confirmed setups
09:16                      opening bell brief ->  Telegram + email
15:40                      end-of-day wrap    ->  PDF + email attachment
on demand                  /live /report /scan /status /wrap
```

> Research tooling. Not investment advice. Paper-trade it before risking capital.

---

## 1. What it detects

| Setup | Logic |
|---|---|
| **VWAP cross** | 15m close crossing session-anchored VWAP within the last 3 bars, with the % penetration recorded. Indices have no volume on Yahoo, so the VWAP falls back to a session typical-price mean and is labelled as a proxy. |
| **Volume surge** | Latest bar volume ÷ SMA(volume, 10) ≥ `VOLUME_SURGE_MULTIPLE` (default 1.5x). Severity 4 at ≥ 2x. |
| **Open = High / Open = Low** | Session has not traded above / below the first print (tick-tolerant), i.e. distribution or accumulation. |
| **SMC order block retest** | Last opposing candle before an ATR-displacement leg; state tracked as fresh → tested → invalidated, plus rejection-close confirmation. |
| **Structure + bias** | Fractal swing highs/lows classified as HH/HL, LH/LL, expanding, contracting or ranging, then combined with VWAP side and the signal tape into BULLISH / BEARISH / NEUTRAL. |
| **Key levels** | Classic floor-trader pivots from the previous session, developing swing levels and session extremes, ranked around CMP. |

Signals carry a 1–5 severity. Anything at or above `ALERT_MIN_SEVERITY` is
pushed; at or above `EMAIL_MIN_SEVERITY` it also goes out by mail. A
per-symbol/per-setup cooldown stops the 15-minute sweep from repeating itself.

## 2. Architecture

```
config.py           env parsing, instruments, session gate, logging
market_scanner.py   yfinance OHLCV + nsepython spot overlay, indicators,
                    detectors, immutable MarketSnapshot / ScanReport
ai_engine.py        Groq client, JSON plan parsing, risk-contract enforcement,
                    deterministic ATR fallback
report_engine.py    ReportLab A4 research note (path + in-memory buffer)
alerts.py           Telegram send_message / send_document, Gmail SMTP with
                    attachments, de-duplication, HTML formatting
bot.py              python-telegram-bot v20+ app + APScheduler jobs
tests/              offline suite (synthetic OHLC, no network, no creds)
```

Data flows one way: `market_scanner` → `ai_engine` → `report_engine` / `alerts`
→ `bot`. Every provider call has retries; one bad symbol never aborts a sweep.

## 3. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`:

1. **Telegram** — create a bot with [@BotFather](https://t.me/BotFather), put the
   token in `TELEGRAM_BOT_TOKEN`, send your bot a message, then set
   `TELEGRAM_CHAT_IDS` (comma separated; groups/channels are negative).
2. **Groq** — key from <https://console.groq.com/keys> in `GROQ_API_KEY`
   (default model `llama-3.3-70b-versatile`). Without a key the desk still runs
   on the deterministic ATR engine.
3. **Email** — `GMAIL_USER` plus a 16-character Google **App Password** in
   `GMAIL_APP_PASSWORD` (Google Account → Security → 2-Step Verification → App
   passwords). Your account password will not work. `EMAIL_RECIPIENTS`
   defaults to the sender.

Then:

```bash
.venv/bin/python bot.py
```

## 4. Commands

| Command | Result |
|---|---|
| `/start`, `/help` | Capability card and command reference |
| `/live` | NIFTY 50 + BANK NIFTY pulse with bias and lead setup |
| `/live RELIANCE` | Full snapshot card: VWAP, range, ATR, volume ratio, signals, S/R |
| `/report BANKNIFTY` | AI verdict + PDF research note delivered as a Telegram document |
| `/scan` | Full watchlist sweep with actionable signatures |
| `/status` | Market phase, scheduler next-run times, engine readiness |
| `/watchlist` | Instruments under surveillance |
| `/wrap` | Trigger the end-of-day wrap immediately (PDF + email) |

Any syntactically valid NSE ticker works with `/live` and `/report`, even if it
is not on the watchlist (`RELIANCE`, `TCS.NS`, `M&M`).

## 5. Risk contract

The desk does not publish a plan it would not trade:

* entry zone must bracket CMP (otherwise it is recentred on live price),
* invalidation must sit on the correct side of entry,
* stop distance must be 0.05%–3.0% of price (noise and swing-trade risk are both rejected),
* T1 must yield at least 1:`MIN_RISK_REWARD` (default 2.0) against that stop,
* T2 must sit beyond T1,
* anything else is downgraded to **NO TRADE** with the reason recorded in the PDF.

Compliant plans only are alerted; `NO TRADE` verdicts are still rendered in the
report so the reasoning is auditable.

## 6. Operations

* **Session gate** — every job calls `Settings.is_market_open()`; cron ticks at
  09:00, 15:45, weekends and holidays are no-ops. Set `MARKET_HOLIDAYS`
  (comma-separated `YYYY-MM-DD`) for each calendar year.
* **Staleness** — if the newest intraday print is older than 25 minutes during
  session, or belongs to a previous session, the snapshot is flagged and the
  warning is printed in the PDF and the alert footer.
* **Startup guard** — `bot.py` exits with code 2 and a clear log line when
  `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_IDS` is missing.
* **Logs** — console + `logs/tradexvm.log` (5 MB × 5 rotations).
* **Reports** — written to `reports/` and also held in memory for Telegram.
* **Tests** — `.venv/bin/python -m pytest tests -q` (offline, no credentials).
* **Long-running** — run under systemd (`Restart=always`) or a process manager;
  one long-polling bot instance per token only.

## 7. Run it 24/7

The desk only works while it is awake, and a sleeping laptop silently misses
sweeps. Put it on any always-on Linux box — one vCPU and 1 GB RAM is plenty.

```bash
# on the server
sudo apt update && sudo apt install -y python3-venv git
git clone <your-repo-url> tradexvm && cd tradexvm
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env          # fill in credentials
.venv/bin/python bot.py                    # smoke test, then Ctrl-C
```

`/etc/systemd/system/tradexvm.service`:

```ini
[Unit]
Description=TradeXVm market desk
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tradexvm
WorkingDirectory=/home/tradexvm/tradexvm
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/tradexvm/tradexvm/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -m -s /bin/bash tradexvm     # skip if the user exists
sudo systemctl daemon-reload
sudo systemctl enable --now tradexvm
journalctl -u tradexvm -f                 # live logs
```

Run exactly **one** bot process per token — Telegram permits a single
long-polling consumer, so two instances will steal updates from each other.
After a code change: `git pull && sudo systemctl restart tradexvm`.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `bot.py` exits with code 2 | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_IDS` missing — see the log line naming both. |
| Commands answer "Not authorised" | That chat id is not in `TELEGRAM_CHAT_IDS`. Add it (negative for groups) and restart. |
| No email arrives | `GMAIL_APP_PASSWORD` is not a 16-character App Password, or `ENABLE_EMAIL=false`. Check `logs/tradexvm.log` for the SMTP error. |
| PDF renders but the verdict says "rule engine" | No `GROQ_API_KEY`, or Groq rejected the call — the deterministic ATR plan is being used. |
| Alerts never fire | No setup reached `ALERT_MIN_SEVERITY`, or the same setup is inside its `ALERT_COOLDOWN_MINUTES` window. Run `/scan`. |
| Every snapshot flagged stale | Market is closed (weekend/holiday/pre-open), or a provider is delayed. The PDF states which session the data belongs to. |
| `TATAMOTORS.NS` returns no rows | Ticker changed after the demerger; replace it in `WATCHLIST` (the default now uses `MARUTI.NS`). |

## 9. Known limits

* Yahoo intraday data is delayed (~15 min on many symbols); the nsepython
  overlay is used when available, but NSE endpoints rate-limit aggressively and
  fail silently by design.
* NSE publishes no index volume through Yahoo, so index VWAP is a proxy — the
  PDF and alert both say so.
* `nsepython` is unofficial; treat its payloads as best-effort. The scanner
  falls back to yfinance-only data without raising.
* Groq is called per actionable instrument, keeping the free tier comfortable;
  rate limits degrade to the rule engine rather than failing the sweep.
