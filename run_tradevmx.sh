#!/usr/bin/env bash
# TradeVMx desktop launcher (macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "[TradeVMx] Creating virtual environment..."
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[TradeVMx] Python 3.11+ is required (https://www.python.org/downloads/)." >&2
    exit 1
  fi
  python3 -m venv .venv
  "$PY" -m pip install --quiet --upgrade pip
fi

if ! "$PY" -c "import flask, reportlab, yfinance, telegram, apscheduler, groq" >/dev/null 2>&1; then
  echo "[TradeVMx] Installing dependencies (first run only)..."
  "$PY" -m pip install --quiet -r requirements.txt
fi

echo "[TradeVMx] Starting the desk..."
exec "$PY" desktop.py "$@"
