# TradeVMx

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

**Getting it:**

* **As a desktop app (recommended):** download `tradevmx-X.Y.Z.zip` from the
  Releases page of this repository, unzip it, and double-click `run_tradevmx.bat`
  (Windows) or run `./run_tradevmx.sh` (macOS/Linux). The desktop app opens a
  single local window with the full dashboard, the live chart, the AI prediction,
  the news/expectations feed, the reports, and in-app notifications. The app
  starts like any other program — no terminal, no browser, no command line.

* **As source:** clone the repository, create a virtual environment and install
  the requirements (see Section 3).

Licensed under the [MIT License](LICENSE).

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
app.py              local Flask console + JSON API (loopback, token-guarded)
desktop.py          launcher: free port, browser, clean shutdown
sentinel.py         shared APScheduler engine: sweeps, brief, EOD, event bus
settings_store.py   .env form schema, validation, atomic writes, secret masks
notify.py           optional desktop toasts / voice (plyer, pyttsx3)
web/                dashboard UI (vanilla HTML/CSS/JS, no build step)
version.py          the single version string every surface reports
updater.py          GitHub release check for the in-app update banner
release.py          bump version, commit, tag (CI builds the zip)
bot.py              Telegram front-end over the same sentinel engine
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

Prefer a GUI? Section 7 gives you the same engine in one local window.

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

## 5. Search and chart usage (desktop app)

The desktop app's dashboard is driven by the search box and the chart panel.

1. **Open the app** (double-click `run_tradevmx.bat` or run `./run_tradevmx.sh`).
2. **Configure** (first launch opens the setup guide): enter your Telegram bot token
   and chat ids, optionally Gmail app password, and your Groq API key (the free
   tier is enough for AI verdicts).
3. **Search a stock**: type a ticker or company name in the **Symbol** search box
   and press **Enter**. The box shows an autocomplete dropdown so you can pick the
   exact trading symbol. The chart loads the live candlestick + volume panel for
   that symbol.
4. **Choose a timeframe**: use the dropdown (1m, 5m, 15m, 30m, 1h) to change the
   chart granularity.
5. **Read the prediction**: the Bias & prediction panel shows the live bias,
   signal chips, and the Groq-written trade plan (entry zone, invalidation,
   T1/T2, reward:risk). With no Groq key the desk still shows the deterministic
   ATR plan so the desk is never silent.
6. **Open a report**: each tape row has a **PDF** button for the full research note.
7. **Arm the sentinel**: the desk runs automatically (`AUTO_ARM=true`) and sweeps
   every 15 minutes during market hours, pushing alerts to Telegram/email and the
   in-app feed.

Any syntactically valid NSE ticker works (RELIANCE, TCS.NS, NIFTY, BANKNIFTY, etc.),
even if it is not on the watchlist.

## 6. Risk contract

The desk does not publish a plan it would not trade:

* entry zone must bracket CMP (otherwise it is recentred on live price),
* invalidation must sit on the correct side of entry,
* stop distance must be 0.05%–3.0% of price (noise and swing-trade risk are both rejected),
* T1 must yield at least 1:`MIN_RISK_REWARD` (default 2.0) against that stop,
* T2 must sit beyond T1,
* anything else is downgraded to **NO TRADE** with the reason recorded in the PDF.

Compliant plans only are alerted; `NO TRADE` verdicts are still rendered in the
report so the reasoning is auditable.

## 7. Operations

* **Session gate** — every job calls `Settings.is_market_open()`; cron ticks at
  09:00, 15:45, weekends and holidays are no-ops. Set `MARKET_HOLIDAYS`
  (comma-separated `YYYY-MM-DD`) for each calendar year.
* **Staleness** — if the newest intraday print is older than 25 minutes during
  session, or belongs to a previous session, the snapshot is flagged and the
  warning is printed in the PDF and the alert footer.
* **Startup guard** — `bot.py` exits with code 2 and a clear log line when
  `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_IDS` is missing.
* **Logs** — console + `logs/tradevmx.log` (5 MB × 5 rotations).
* **Reports** — written to `reports/` and also held in memory for Telegram.
* **Tests** — `.venv/bin/python -m pytest tests -q` (offline, no credentials).
* **Long-running** — run under systemd (`Restart=always`) or a process manager;
  one long-polling bot instance per token only.

## 8. Desktop app (single local window)

TradeVMx opens as a real desktop app: one local window with every feature — the
live candlestick + volume chart, the AI prediction, the news/expectations feed,
the reports, and in-app notifications. It starts exactly like a normal program:
you double-click it, the app opens, and configuration happens inside. There is no
terminal, no browser, and no command line to run it.

### Download and run (Windows)

1. Download `tradevmx-X.Y.Z.zip` from the [Releases page](https://github.com/LogicalArnesh/tradevmx/releases).
2. Unzip the folder.
3. Double-click `run_tradevmx.bat`.

A window opens to the desktop console. The app prints a banner with the console
URL (`http://127.0.0.1:8765` on first launch) and arms the sentinel automatically.
Closing the window stops the desk.

### Download and run (macOS / Linux)

1. Download `tradevmx-X.Y.Z.zip` from the [Releases page](https://github.com/LogicalArnesh/tradevmx/releases).
2. Unzip the folder.
3. Run `./run_tradevmx.sh` (or double-click it).

### What the app contains

| Item | What it does |
|---|---|
| **App name + version** | Shown at the top of the console (`TradeVMx · NSE Market Desk 1.0.5`) and in the settings summary. |
| **Dashboard** | IST clock, market phase, Arm/Disarm sentinel, Scan now, live tape (CMP, change, bias, VWAP distance, signal chips), signal feed, one-click PDF per symbol. |
| **Live chart** | Self-contained candlestick + volume panel. No TradingView key needed. Search a symbol by name or ticker in the **Symbol** box, pick a timeframe (1m/5m/15m/30m/1h) from the dropdown, and the chart renders the live OHLCV bars with support/resistance levels. |
| **Bias & prediction** | Groq-written trade plan (entry zone, invalidation, T1/T2, reward:risk) with the deterministic ATR fallback when no key is configured. |
| **Signal feed** | Real-time alerts from the sentinel sweep. |
| **Reports** | One-click PDF per symbol — executive summary, price table, AI verdict, levels. |
| **Setup / Configuration** | Paste your Telegram bot token and chat ids, Gmail app password, and Groq API key. **Send test** verifies each channel. Saving rewrites `.env` atomically and reloads the engine without a restart. |
| **Activity log** | Tail of `logs/tradevmx.log`. |

Everything is local: the server binds `127.0.0.1`, every launch generates a
random token that all mutating requests must present, cross-origin POSTs are
refused, and the page runs under a strict Content-Security-Policy. Secrets are
never sent back to the browser - only a short mask.

### Setup flow inside the app

1. Launch the app. A **Setup guide** opens by itself asking for the Telegram bot
   token, chat ids, then the optional Gmail app password and Groq key. (Use the
   **Configuration** tab at any time to re-open it.)
2. Enter the **Telegram** group: `TELEGRAM_BOT_TOKEN` from [@BotFather](https://t.me/BotFather)
   and your chat id from [@userinfobot](https://t.me/userinfobot).
3. Enter the **Groq** group: `GROQ_API_KEY` from [console.groq.com/keys] (the free
   tier is enough). Without a key the desk still runs on the deterministic ATR
   engine — the search, chart and prediction will all work, just without an AI
   verdict.
4. Enter the **Email** group (optional): `GMAIL_USER` + a 16-character Gmail App
   Password and `EMAIL_RECIPIENTS`.
5. Click **Save & arm the desk**.

### Everyday flow in the desktop app

* The desk arms itself (`AUTO_ARM=true`) and sweeps every 15 minutes during
  market hours, pushing alerts to Telegram/email and the in-app feed.
* **Search a stock**: type a name or ticker (e.g. `reliance`, `RELIANCE`, `^NSEI`,
  `NIFTY`) into the search box and press **Enter**. The app shows an autocomplete
  dropdown so you can pick the exact symbol, then loads the live candlestick +
  volume chart with current price, bias and signal levels.
* Use the **timeframe** selector to change the chart granularity.
* Press **Scan now** to force a fresh sweep at any time.
* Each tape row has a **PDF** button for the full research note.

## 9. Desktop console (the same window, scoped to one app)

This is the original local console: one local window with every feature, and
your API keys editable inside the app.

```bash
python desktop.py                       # or double-click run_tradevmx.bat
                                        # macOS/Linux: ./run_tradevmx.sh
```

The launcher starts the console on `http://127.0.0.1:8765` (it moves to the next
free port if that one is taken), opens your browser, and shuts the desk down when
you close the window.

| Panel | What it does |
|---|---|
| **Dashboard** | IST clock and market phase, Arm/Disarm sentinel, Scan now, live tape (CMP, change, bias, VWAP distance, signal chips), signal feed, one-click PDF per symbol. |
| **Configuration** | Every key and threshold as a form. Secrets are masked and never leave the machine; **Send test** verifies Telegram, Gmail and Groq independently; saving rewrites `.env` atomically and reloads the engine without a restart. |
| **Activity** | Tail of `logs/tradevmx.log`. |

Everything is local: the server binds `127.0.0.1`, every launch generates a
random token that all mutating requests must present, cross-origin POSTs are
refused, and the page runs under a strict Content-Security-Policy. Secrets are
never sent back to the browser - only a short mask.

Optional extras, not installed by default and safe to skip:

```bash
.venv/bin/pip install plyer pyttsx3     # Windows: .venv\Scripts\pip.exe install plyer pyttsx3
```

Then enable *Desktop toasts* / *Voice announcements* in Configuration. Without
those packages the toggles simply stay silent; the in-app feed and Telegram
alerts are unaffected.

On first launch a **setup guide** opens by itself: it asks for the Telegram bot
token and chat ids, then the optional Gmail app password and Groq key, and arms
the desk when you save. It is the same form as Configuration, limited to the
three channel groups. Skip it and the guide returns on the next launch.

**Everyday flow:** start the app -> the desk arms itself (`AUTO_ARM=true`) and
sweeps every 15 minutes during market hours -> press **Scan now** if you want a
sweep immediately -> open the feed or Telegram for alerts -> each tape row has a
PDF button for the full research note.

## 10. Versions & updates

Every release is a git tag `vX.Y.Z`, a GitHub Release with a downloadable zip,
and an entry in [CHANGELOG.md](CHANGELOG.md). The sidebar shows the running
version; the app also checks the repository named in `UPDATE_REPO`
(`owner/repo`, empty disables the check) and shows a download banner above the
dashboard when a newer tag exists. Results are cached for six hours, and an
offline machine or a repo with no releases is treated as "nothing to do".

Cut a release:

```bash
python release.py 1.0.1 "Tighten the opening-brief window"
git push origin HEAD --tags
```

`release.py` bumps `version.py`, commits and tags. Pushing the tag runs
`.github/workflows/release.yml`, which zips the tracked files (no `.env`, no
logs, no reports) and attaches them to the release; `tests.yml` runs the offline
suite on every push.

Updating by hand is dropping the new files over the old folder: `.env`, `logs/`
and `reports/` are not part of the bundle, so your keys and history survive.

**Publish your own copy.** With the [GitHub CLI](https://cli.github.com) installed,
run `./push-to-github.sh` (or `./push-to-github.sh my-desk` for a different repo
name). It asks for a token with `repo` scope using a hidden prompt — nothing is
echoed or stored — creates the private repository, pushes `main` and the tags,
sets the repository description and topics, and points `UPDATE_REPO` at it. The
pushed `vX.Y.Z` tag then builds the downloadable release.

## 11. Run it 24/7

The desk only works while it is awake, and a sleeping laptop silently misses
sweeps. Two ways to keep it alive.

**Your own PC (simplest).** Run `install-autostart.bat` on Windows, or
`./install-autostart.sh` on macOS/Linux. It registers the console as a login item
(Task Scheduler, launchd, or a systemd user unit) that restarts if it dies, runs
headless, and arms the sentinel on boot because `AUTO_ARM=true`. Open
`http://127.0.0.1:8765` whenever you want the panel.

**A dedicated Linux box.** One vCPU and 1 GB RAM is plenty.

```bash
# on the server
sudo apt update && sudo apt install -y python3-venv git
git clone <your-repo-url> tradevmx && cd tradevmx
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env          # fill in credentials
.venv/bin/python bot.py                    # smoke test, then Ctrl-C
```

`/etc/systemd/system/tradevmx.service`:

```ini
[Unit]
Description=TradeVMx market desk
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tradevmx
WorkingDirectory=/home/tradevmx/tradevmx
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/tradevmx/tradevmx/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -m -s /bin/bash tradevmx     # skip if the user exists
sudo systemctl daemon-reload
sudo systemctl enable --now tradevmx
journalctl -u tradevmx -f                 # live logs
```

Run exactly **one** bot process per token — Telegram permits a single
long-polling consumer, so two instances will steal updates from each other.
After a code change: `git pull && sudo systemctl restart tradevmx`.

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `bot.py` exits with code 2 | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_IDS` missing — see the log line naming both. |
| Commands answer "Not authorised" | That chat id is not in `TELEGRAM_CHAT_IDS`. Add it (negative for groups) and restart. |
| No email arrives | `GMAIL_APP_PASSWORD` is not a 16-character App Password, or `ENABLE_EMAIL=false`. Check `logs/tradevmx.log` for the SMTP error. |
| PDF renders but the verdict says "rule engine" | No `GROQ_API_KEY`, or Groq rejected the call — the deterministic ATR plan is being used. |
| Alerts never fire | No setup reached `ALERT_MIN_SEVERITY`, or the same setup is inside its `ALERT_COOLDOWN_MINUTES` window. Run `/scan`. |
| Every snapshot flagged stale | Market is closed (weekend/holiday/pre-open), or a provider is delayed. The PDF states which session the data belongs to. |
| `TATAMOTORS.NS` returns no rows | Ticker changed after the demerger; replace it in `WATCHLIST` (the default now uses `MARUTI.NS`). |
| Console will not open | The launcher prints the real URL; if the port is taken it silently moves to the next free one. Or pass `--port 8770`. |
| "could not write .env" when saving | The project folder is read-only (common under `Program Files`). Move it to your home folder. |
| No toasts or voice | Optional extras are missing: `pip install plyer pyttsx3`, then save and re-arm. |
| Settings save says "validation failed" | The offending input is highlighted; chat ids must be numeric, emails valid, holidays `YYYY-MM-DD`, severities 1-5. |
| No update banner ever appears | `UPDATE_REPO` is empty, or that repo has no published release yet. Set `owner/repo` and push a `vX.Y.Z` tag. |
| Autostart does nothing | Windows: `schtasks /Query /TN TradeVMx`. Linux: `systemctl --user status tradevmx`. Re-run the installer as the same user that owns the folder. |
| It never arms by itself | `AUTO_ARM=false`, or a `.env` copied from an older version without the key. Open Configuration once and save. |

## 13. Known limits

* Yahoo intraday data is delayed (~15 min on many symbols); the nsepython
  overlay is used when available, but NSE endpoints rate-limit aggressively and
  fail silently by design.
* NSE publishes no index volume through Yahoo, so index VWAP is a proxy — the
  PDF and alert both say so.
* `nsepython` is unofficial; treat its payloads as best-effort. The scanner
  falls back to yfinance-only data without raising.
* Groq is called per actionable instrument, keeping the free tier comfortable;
  rate limits degrade to the rule engine rather than failing the sweep.

## 12. License

MIT — see [LICENSE](LICENSE). Use it, fork it, sell your own alerts with it. No
warranty: this is research tooling, not investment advice, and the author is not
responsible for what the market does to your capital.
