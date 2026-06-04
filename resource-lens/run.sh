#!/usr/bin/env bash
#
# run.sh — start CSTA Copilot (Resource Lens).
#
# Flow:
#   1. Verify the Confluent Cloud cookie in .env is non-empty AND valid.
#   2. If it's missing/invalid, run the capture flow (opens a browser, you log
#      in to admin.confluent.cloud, the cookie is written to .env).
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

# Dock/LaunchServices launches get a minimal PATH; prepend the common locations
# so tools used here (lsof, curl, open) and any user-installed binaries resolve.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

PORT=5002
VENV_PY=".venv/bin/python"
OPEN_URL="http://localhost:$PORT/"
SETUP_MARKER=".setup-complete"

# --- guard: setup must have completed successfully -------------------------
# setup.sh writes .setup-complete only when every required check passes (Python,
# deps, …) and clears it otherwise. No marker → setup wasn't done or didn't
# finish cleanly, so refuse to launch.
if [ ! -f "$SETUP_MARKER" ]; then
  printf "${RED}Setup not complete.${RESET} Run ${BOLD}./setup.sh${RESET} first (it must finish with \"Setup complete\").\n"
  exit 1
fi
if [ ! -x "$VENV_PY" ]; then
  printf "${RED}Setup looks incomplete${RESET} (no virtualenv). Re-run ${BOLD}./setup.sh${RESET}.\n"
  exit 1
fi

# --- 1. validate the cookie ------------------------------------------------
printf "${BOLD}Checking Confluent Cloud cookie…${RESET}\n"
"$VENV_PY" capture_cookies.py --check
COOKIE_STATUS=$?

# --- 2. capture if missing/invalid -----------------------------------------
# --check exit codes: 0 = valid, 1 = missing/invalid, 2 = couldn't verify.
if [ "$COOKIE_STATUS" -eq 2 ]; then
  printf "\n${YELLOW}Couldn't verify the cookie.${RESET} Starting anyway —\n"
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
