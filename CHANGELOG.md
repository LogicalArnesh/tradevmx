# Changelog

## v1.3.0 — layout fix and full market report

* **Fixed the overlapping logo and chart.** `.brand-logo` had no CSS rule, so the
  512px PNG rendered at full size and covered the window; it is now a fixed 36px
  tile. The chart SVG had no height constraint either — the panel is now a fixed
  420px (320px on narrow windows) with the SVG filling it. Panes also get
  `min-width: 0` so grid children can no longer overflow the window.
* **New long-form market report** (`/api/report/session`, `studio.build_session_report`):
  a cover page, executive summary, market-internals table, a predictions table with
  entry band/stop/T1/T2/R:R, one charted page per instrument (real candlesticks drawn
  server-side, indicator panel, live signals, plan), an indicator-methodology
  section, and a sources/freshness/disclaimer page. A typical sweep yields 12–16
  pages, and every page keeps the logo footer, licence id and copyright.
* **Assistant hands you the report.** Asking for today's summary, a report, a brief
  or a PDF returns the answer plus a one-click button for the full charted document.
  The local (no-key) answer now names the plans on the tape, the VWAP breadth and
  the risk-reward of each setup.

## v1.2.0 — always-on digest, auto-start, updates

* **Always-on watchlist digest.** A scheduler job (`watchlist_digest`) runs every
  `DIGEST_INTERVAL_MINUTES` (default 60) between 09:15 and 15:30 IST on trading
  days. It re-scans the watchlist, builds a branded PDF and pushes it to every
  ready channel: Gmail with the PDF attached, plus a Telegram summary.
  Toggle with `ENABLE_DIGEST`; "Email digest now" in Configuration does the same
  on demand, and "Watchlist digest PDF" downloads it without sending anything.
* **Digest PDF.** `studio.build_watchlist_brief` — logo, KPI strip, desk view,
  a prediction table (entry band, stop, T1/T2, R:R), a mini price chart per
  instrument and a sources page. Licence id and copyright on every page.
* **Run at Windows startup.** `/api/autostart` adds or removes a Startup-folder
  shortcut pointing at the new silent launcher `run_tradevmx_silent.vbs`, so the
  desk is active from login with no console window. Non-Windows hosts get a clear
  message instead of an error.
* **Update check that is visible.** `/api/update?force=1` for an immediate check,
  a "Check for updates" button, the running version shown in Configuration, the
  current version written into `version.py`, and an automatic poll every 6 hours
  on top of the background daily check.
* New settings group **Digest**: `ENABLE_DIGEST`, `DIGEST_INTERVAL_MINUTES`.

## v1.1.1 — install fix

* `requirements.txt` pinned the deprecated `webview` package, which needs GTK
  headers and fails to build on a clean machine (and on CI). It now pins
  `pywebview>=5.0`, the package that actually provides the `webview` module, so a
  fresh `run_tradevmx.bat` install succeeds on Windows and Linux.
* Added `pillow` explicitly, used for the PDF header logo.

## v1.1.0 — Studio release

* **Fixed the dead UI.** `web/static/app.js` had a duplicated `let` declaration and
  template literals mangled by a patch script; the script died on the syntax error,
  so no click handler was ever attached. Rewritten as one syntax-checked file.
* **Four-pane studio:** chart with crosshair and adjustable timeline, expected range /
  best hour window, desk stats, and a simulated demat book.
* **Any stock:** `/api/search` matches tickers and company names across ~130 NSE names.
* **Real history:** `/api/history` returns OHLCV so the chart is no longer one bar.
* **AI assistant chamber:** `/api/assistant`, Groq-backed with a deterministic digest.
* **Branded daily brief PDF:** `/api/report/daily` with logo, KPI strip, movers,
  signals and a licence/copyright footer on every page.
* **Paper portfolio:** `/api/portfolio` with cash, positions, realised/unrealised P&L.
* **Reports & export settings:** `EXPORT_DIR`, `OWNER_NAME`; licence gate
  (`LICENSE_ACCEPTED`); desktop shortcut (`/api/shortcut`).
* **Logo repaired:** `logo.png` was an SVG mislabelled `.png`; now a real PNG plus `.ico`.
* **Native window hardening:** a backend failure explains what to install instead of
  handing the session to a browser.

Versions follow `MAJOR.MINOR.PATCH` and every release is a git tag `vX.Y.Z`, a
GitHub release with a downloadable zip, and (if `UPDATE_REPO` is set) a banner
inside the app.

## 1.0.1 — update checks start clean

- Fresh downloads point at the upstream repository by default, so the update
  banner works without filling in `UPDATE_REPO` first (a fork only needs to
  override it).
- A GitHub rate limit on shared or corporate connections now reads "rate limit
  reached, will check again later" instead of a bare HTTP error.

## 1.0.0 — first public release

The autonomous NSE desk, running entirely on your own PC.

- **Market engine** — NIFTY 50, BANK NIFTY and a configurable NSE watchlist on
  5m/15m. VWAP reclaim/loss, abnormal-volume confirmation, Open=High / Open=Low
  and SMC order-block retests, scored into a bias and a severity.
- **Groq plans** — entry zone, invalidation, T1/T2 and reward:risk, validated
  server-side against the live price and the minimum R:R; deterministic ATR
  plans when no key is configured.
- **PDF research note** — executive summary, price table, AI verdict, levels.
- **Telegram + Gmail** — severity-gated push alerts with de-duplication, an
  opening brief, and an end-of-day wrap with the PDF attached.
- **Desktop console** — local Flask UI on `127.0.0.1` with live tape, signal
  feed, activity log, per-channel tests and an in-app `.env` editor.
- **Setup wizard** — asks for the Telegram bot token, email and Groq key on
  first launch, then arms the desk automatically.
- **Always-on** — `AUTO_ARM` sweeps without a click; `install-autostart.*`
  registers the desk at login (Task Scheduler, systemd user unit, launchd).
- **Updates** — in-app version banner driven by GitHub releases, plus
  `release.py` to bump, commit and tag.
