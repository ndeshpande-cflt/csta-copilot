#!/usr/bin/env bash
#
# run.sh — start CSTA Copilot (Ticket Lens).
#
# Flow:
#   1. Verify the Zendesk cookie in .env is non-empty AND valid.
#   2. If it's missing/invalid, run the capture flow (opens a browser, you log
#      in, the cookie is written to .env).
#   3. Start the app and open it in your browser.
#
# Prerequisites (Python venv, deps, Playwright browser) are handled by
# ./setup.sh — run that once first.
#
#   ./run.sh
#
set -uo pipefail

if [ -t 1 ]; then
  GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; BOLD=""; DIM=""; RESET=""
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Dock/LaunchServices launches get a minimal PATH that omits where CLI tools
# like `claude` usually live, so the app would report "Claude Code CLI not found
# on PATH". Prepend the common locations so it's visible to the app.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.claude/local:$PATH"

PORT=5001
VENV_PY=".venv/bin/python"
OPEN_URL="http://localhost:$PORT/tickets"

# --- guard: prerequisites must be set up -----------------------------------
if [ ! -x "$VENV_PY" ]; then
  printf "${RED}Not set up yet.${RESET} Run ${BOLD}./setup.sh${RESET} first.\n"
  exit 1
fi

# --- 1. validate the cookie ------------------------------------------------
printf "${BOLD}Checking Zendesk cookie…${RESET}\n"
"$VENV_PY" capture_cookies.py --check
COOKIE_STATUS=$?

# --- 2. capture if missing/invalid -----------------------------------------
# --check exit codes: 0 = valid, 1 = missing/invalid, 2 = couldn't reach Zendesk.
if [ "$COOKIE_STATUS" -eq 2 ]; then
  printf "\n${YELLOW}Couldn't verify the cookie (network/Zendesk issue).${RESET} Starting anyway —\n"
  printf "the app will show Disconnected if it's not usable.\n"
elif [ "$COOKIE_STATUS" -ne 0 ]; then
  printf "\n${YELLOW}No valid cookie — launching capture.${RESET} A browser will open; just log in.\n\n"
  "$VENV_PY" capture_cookies.py
  CAPTURE_STATUS=$?
  if [ "$CAPTURE_STATUS" -ne 0 ]; then
    printf "\n${RED}Cookie capture didn't complete — not starting the app.${RESET}\n"
    printf "Re-run ${BOLD}./run.sh${RESET} to try again.\n"
    exit 1
  fi
fi

# --- 3. open the browser once the server is up, then start the app ---------
if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  printf "\n${YELLOW}Port %s is already in use${RESET} — the app may already be running.\n" "$PORT"
  printf "Opening ${BOLD}%s${RESET} in your browser.\n" "$OPEN_URL"
  open "$OPEN_URL" 2>/dev/null || true
  exit 0
fi

( for _ in $(seq 1 40); do
    if curl -s -o /dev/null "http://localhost:$PORT" 2>/dev/null; then break; fi
    sleep 0.5
  done
  open "$OPEN_URL" 2>/dev/null || true ) &

printf "\nStarting app → ${BOLD}%s${RESET}  ${DIM}(Ctrl-C to stop)${RESET}\n\n" "$OPEN_URL"
exec "$VENV_PY" app.py
