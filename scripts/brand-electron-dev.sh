#!/usr/bin/env bash
# Brand the dev Electron.app so `electron .` shows "Notes Graph" (name + icon)
# in the Dock and menu bar instead of "Electron". The bundle name and icon live
# in the binary's Info.plist/Resources, which no runtime API can change.
#
# Idempotent; wired to npm postinstall so an electron reinstall/upgrade
# re-applies it. macOS only. CFBundleIdentifier is left untouched so any
# TCC (Full Disk Access) grants keyed to it survive.
set -euo pipefail

[ "$(uname)" = "Darwin" ] || exit 0

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/node_modules/electron/dist/Electron.app"
PLIST="$APP/Contents/Info.plist"
ICON_SRC="$ROOT/assets/icon.icns"
ICON_DST="$APP/Contents/Resources/electron.icns"

[ -f "$PLIST" ] || exit 0  # electron not installed yet
[ -f "$ICON_SRC" ] || { echo "brand-electron-dev: assets/icon.icns missing, skipping"; exit 0; }

current_name="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleName' "$PLIST" 2>/dev/null || echo '')"
if [ "$current_name" = "Notes Graph" ] && cmp -s "$ICON_SRC" "$ICON_DST"; then
  exit 0  # already branded
fi

/usr/libexec/PlistBuddy -c 'Set :CFBundleName "Notes Graph"' "$PLIST"
/usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName "Notes Graph"' "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c 'Add :CFBundleDisplayName string "Notes Graph"' "$PLIST"
cp "$ICON_SRC" "$ICON_DST"

# Editing the bundle invalidates its ad-hoc signature; re-sign or macOS
# (Apple Silicon) refuses to launch it.
codesign --force --deep --sign - "$APP"

echo "brand-electron-dev: Electron.app branded as Notes Graph"
