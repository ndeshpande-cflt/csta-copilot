#!/bin/bash
#
# start.command — runs Ticket Lens in THIS Terminal window.
#
# Opened by Ticket Lens.app (via `open -a Terminal`). The app runs in the
# foreground here, so you see live logs and Ctrl+C stops it. When the server
# stops (Ctrl+C, or killed when you Quit the app from the Dock), this window
# closes itself.

cd "$(dirname "$0")" || exit 1

# Close this Terminal window when the script exits. Done from inside Terminal
# (Terminal controlling itself), so it needs no Automation permission. If it
# can't (e.g. unusual setup), the window simply stays open — harmless.
MY_TTY="$(tty 2>/dev/null)"
cleanup() {
  if [ -n "$MY_TTY" ]; then
    osascript -e "tell application \"Terminal\" to close (every window whose tty is \"$MY_TTY\")" >/dev/null 2>&1
  fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM

echo "Starting Ticket Lens…  (Ctrl+C to stop)"
echo

# First run / not set up yet → run setup, visible here.
if [ ! -x ".venv/bin/python" ]; then
  ./setup.sh || {
    echo
    echo "Setup failed — see above. Press Return to close."
    read -r _
    exit 1
  }
fi

# Run (not exec) so we regain control to close the window when the app stops.
./run.sh
