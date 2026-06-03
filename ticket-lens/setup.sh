#!/usr/bin/env bash
#
# setup.sh — one-time prerequisite setup for CSTA Copilot (Ticket Lens).
#
# Runs a checklist, prints a status line for each, and fixes what it safely can:
# creates the venv, installs deps, seeds .env, installs the browser the cookie
# capture flow needs. It does NOT start the app — use ./run.sh for that.
#
#   ./setup.sh
#
set -uo pipefail

# --- cosmetics -------------------------------------------------------------
if [ -t 1 ]; then
  GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; BOLD=""; DIM=""; RESET=""
fi

PASS="${GREEN}✔${RESET}"
FAIL="${RED}✘${RESET}"
WARN="${YELLOW}!${RESET}"

FAILED=0
note()  { printf "  %s %s\n" "$1" "$2"; }
detail(){ printf "      ${DIM}%s${RESET}\n" "$1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=9

printf "\n${BOLD}CSTA Copilot — setup${RESET}\n"
printf "${DIM}%s${RESET}\n\n" "$SCRIPT_DIR"

# --- 1. Python -------------------------------------------------------------
PYTHON_BIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; break; fi
done

if [ -z "$PYTHON_BIN" ]; then
  note "$FAIL" "Python not found"
  detail "Install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+ — https://www.python.org/downloads/ or 'brew install python'"
  FAILED=1
else
  PY_VER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  if "$PYTHON_BIN" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($PYTHON_MIN_MAJOR, $PYTHON_MIN_MINOR) else 1)"; then
    note "$PASS" "Python $PY_VER ($PYTHON_BIN)"
  else
    note "$FAIL" "Python $PY_VER is too old — need ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+"
    detail "Install a newer Python — https://www.python.org/downloads/ or 'brew install python'"
    FAILED=1
  fi
fi

# --- 2. Claude Code CLI ----------------------------------------------------
# The app shells out to this to generate briefs. Honour CLAUDE_CMD from .env.
CLAUDE_CMD="claude"
if [ -f .env ]; then
  ENV_CLAUDE="$(grep -E '^[[:space:]]*CLAUDE_CMD=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' \t\r')"
  [ -n "$ENV_CLAUDE" ] && CLAUDE_CMD="$ENV_CLAUDE"
fi

if command -v "$CLAUDE_CMD" >/dev/null 2>&1; then
  CLAUDE_VER="$("$CLAUDE_CMD" --version 2>/dev/null | head -1)"
  note "$PASS" "Claude Code CLI found (${CLAUDE_CMD}${CLAUDE_VER:+ — $CLAUDE_VER})"
else
  note "$FAIL" "Claude Code CLI '$CLAUDE_CMD' not found on PATH"
  detail "Install it — https://docs.claude.com/en/docs/claude-code/setup"
  detail "If it's installed elsewhere, set CLAUDE_CMD to its full path in .env"
  FAILED=1
fi

# --- 3. .env config --------------------------------------------------------
if [ -f .env ]; then
  note "$PASS" ".env present"
  SUBDOMAIN="$(grep -E '^[[:space:]]*ZENDESK_SUBDOMAIN=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' \t\r')"
  if [ -z "$SUBDOMAIN" ] || [ "$SUBDOMAIN" = "yourcompany" ]; then
    note "$WARN" "ZENDESK_SUBDOMAIN not set (still default/empty)"
    detail "Edit .env and set ZENDESK_SUBDOMAIN to your company's Zendesk subdomain"
  else
    note "$PASS" "ZENDESK_SUBDOMAIN=$SUBDOMAIN"
  fi
else
  # Create .env with ZENDESK_SUBDOMAIN preset to confluent. Seed from
  # .env.example if present (swapping the placeholder), else write a minimal file.
  if [ -f .env.example ]; then
    sed -E 's/^([[:space:]]*ZENDESK_SUBDOMAIN[[:space:]]*=).*/\1confluent/' .env.example > .env
    # If .env.example had no ZENDESK_SUBDOMAIN line, append one.
    grep -qE '^[[:space:]]*ZENDESK_SUBDOMAIN[[:space:]]*=' .env || printf '\nZENDESK_SUBDOMAIN=confluent\n' >> .env
  else
    printf 'ZENDESK_SUBDOMAIN=confluent\n' > .env
  fi
  note "$PASS" ".env created with ZENDESK_SUBDOMAIN=confluent"
fi

# --- 4. Virtual environment ------------------------------------------------
VENV_DIR=".venv"
if [ -n "$PYTHON_BIN" ]; then
  if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ]; then
    note "$PASS" "Virtual env present ($VENV_DIR)"
  else
    printf "  ${DIM}…creating virtual env${RESET}\n"
    if "$PYTHON_BIN" -m venv "$VENV_DIR" >/dev/null 2>&1; then
      note "$PASS" "Virtual env created ($VENV_DIR)"
    else
      note "$FAIL" "Failed to create virtual env"
      detail "Try: $PYTHON_BIN -m venv $VENV_DIR"
      FAILED=1
    fi
  fi
fi

VENV_PY="$VENV_DIR/bin/python"

# --- 5. Python dependencies ------------------------------------------------
if [ -x "$VENV_PY" ] && [ -f requirements.txt ]; then
  if "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    # Already satisfied?
    if "$VENV_PY" -c "import flask, requests, dotenv, markdown, playwright" >/dev/null 2>&1; then
      note "$PASS" "Dependencies installed"
    else
      printf "  ${DIM}…installing dependencies${RESET}\n"
      if "$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 \
         && "$VENV_PY" -m pip install --quiet -r requirements.txt; then
        note "$PASS" "Dependencies installed"
      else
        note "$FAIL" "Dependency install failed"
        detail "Try: $VENV_PY -m pip install -r requirements.txt"
        FAILED=1
      fi
    fi
  else
    note "$FAIL" "pip not available in venv"
    FAILED=1
  fi
elif [ ! -f requirements.txt ]; then
  note "$FAIL" "requirements.txt not found"
  FAILED=1
fi

# --- 6. Playwright browser (for cookie capture) ----------------------------
# capture_cookies.py launches a headed Chromium; install it once here.
if [ -x "$VENV_PY" ] && "$VENV_PY" -c "import playwright" >/dev/null 2>&1; then
  if "$VENV_PY" -c "
import sys
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        import os
        sys.exit(0 if os.path.exists(p.chromium.executable_path) else 1)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; then
    note "$PASS" "Playwright Chromium installed"
  else
    printf "  ${DIM}…installing Playwright Chromium (one-time ~150MB)${RESET}\n"
    if "$VENV_PY" -m playwright install chromium >/dev/null 2>&1; then
      note "$PASS" "Playwright Chromium installed"
    else
      note "$WARN" "Couldn't install Playwright Chromium"
      detail "Cookie capture won't work until you run: $VENV_PY -m playwright install chromium"
    fi
  fi
fi

# --- verdict ---------------------------------------------------------------
printf "\n"
if [ "$FAILED" -ne 0 ]; then
  printf "${RED}${BOLD}Setup incomplete — fix the items marked %s above, then re-run ./setup.sh${RESET}\n\n" "$FAIL"
  exit 1
fi

printf "${GREEN}${BOLD}Setup complete.${RESET}\n"
printf "Start the app with ${BOLD}./run.sh${RESET}\n\n"
