# Changelog

Versions follow `MAJOR.MINOR.PATCH` and every release is a git tag `vX.Y.Z`, a
GitHub release with a downloadable zip, and (if `UPDATE_REPO` is set) a banner
inside the app.

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
