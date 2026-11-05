#!/usr/bin/env bash
# TradeVMx :: publish this folder to your own GitHub account.
#
#   ./push-to-github.sh              # creates/updates <you>/tradevmx
#   ./push-to-github.sh my-desk      # custom repository name
#
# The token is read silently (never echoed, never written to disk, never in
# shell history) and handed to `gh` on stdin. Requires the GitHub CLI:
# https://cli.github.com
set -euo pipefail
cd "$(dirname "$0")"

REPO="${1:-tradevmx}"
DESCRIPTION="TradeVMx - autonomous NSE/BSE market desk: multi-timeframe scans, Groq trade plans, PDF research notes, Telegram + email alerts and a local desktop console."

command -v gh >/dev/null 2>&1 || {
  echo "Install the GitHub CLI first: https://cli.github.com" >&2
  exit 1
}
git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "This folder is not a git repository." >&2
  exit 1
}

if ! gh auth status >/dev/null 2>&1; then
  printf 'Paste a GitHub token with "repo" scope (input is hidden): '
  read -rs TOKEN
  echo
  [ -n "${TOKEN:-}" ] || { echo "No token entered." >&2; exit 1; }
  printf '%s' "$TOKEN" | gh auth login --with-token
  unset TOKEN
fi

LOGIN="$(gh api user --jq .login)"
echo "[TradeVMx] Signed in as $LOGIN"

if ! gh repo view "$LOGIN/$REPO" >/dev/null 2>&1; then
  echo "[TradeVMx] Creating the private repository $REPO..."
  gh repo create "$REPO" --private --description "$DESCRIPTION"
fi

echo "[TradeVMx] Pushing branches and tags..."
git -c credential.helper='!gh auth git-credential' \
  push "https://github.com/$LOGIN/$REPO.git" HEAD:refs/heads/main --tags

# Repository page: description and topics.
gh api -X PATCH "repos/$LOGIN/$REPO" -f description="$DESCRIPTION" >/dev/null 2>&1 || true
gh api -X PUT "repos/$LOGIN/$REPO/topics" \
  -f "names[]=nse" -f "names[]=trading" -f "names[]=market-data" \
  -f "names[]=python" -f "names[]=telegram-bot" >/dev/null 2>&1 || true

# Point the in-app updater at the new repository.
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
"$PY" - <<PY
import settings_store
settings_store.write_env({"UPDATE_REPO": "$LOGIN/$REPO"})
print("[TradeVMx] UPDATE_REPO now points at $LOGIN/$REPO")
PY

echo
echo "[TradeVMx] Published: https://github.com/$LOGIN/$REPO"
echo "[TradeVMx] The v* tag you pushed runs the release workflow and attaches the download zip."
