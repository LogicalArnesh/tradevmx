# Changelog

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
