#!/usr/bin/env bash
#
# build_app.sh — (re)build "Resource Lens.app" from "Resource Lens.applescript".
#
# Produces a stay-open AppleScript applet (a real app, so it shows the Dock
# running indicator), then restores the custom icon and bundle metadata.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP="Resource Lens.app"
SRC="Resource Lens.applescript"
ICON_SRC=""
for cand in "assets/AppIcon.icns" "/tmp/RL-AppIcon.icns" "$APP/Contents/Resources/applet.icns"; do
  [ -f "$cand" ] && ICON_SRC="$cand" && break
done

# Compile the applet. -s = stay-open (process stays alive → Dock dot persists).
rm -rf "$APP"
osacompile -s -o "$APP" "$SRC"

# Restore the custom icon (osacompile writes its own applet.icns).
if [ -n "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$APP/Contents/Resources/applet.icns"
fi

# Friendly name + identifier.
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName Resource Lens" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string Resource Lens" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName Resource Lens" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.confluent.csta.resourcelens" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.confluent.csta.resourcelens" "$PLIST" 2>/dev/null || true

# Refresh LaunchServices + icon cache so the change is picked up.
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true

echo "Built $APP"
