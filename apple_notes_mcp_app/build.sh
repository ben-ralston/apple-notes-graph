#!/bin/bash
# Build AppleNotesMCP.app from Swift source.
#
# Usage: ./apple_notes_mcp_app/build.sh [output-dir]
#
# Output: <output-dir>/AppleNotesMCP.app  (default output-dir: dist/)
#
# Signing identity is controlled by NOTES_GRAPH_SIGN_IDENTITY env var.
# If unset, falls back to ad-hoc signing (-) and prints a warning;
# ad-hoc-signed builds will re-prompt for FDA on every rebuild.
# Run apple_notes_mcp_app/sign-setup.sh once to create a stable local cert.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd)
OUTPUT_DIR=${1:-"$PROJECT_ROOT/dist"}
APP_NAME="AppleNotesMCP.app"
APP_PATH="$OUTPUT_DIR/$APP_NAME"
EXECUTABLE_NAME="AppleNotesMCPRunner"

mkdir -p "$OUTPUT_DIR"
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"

echo "Compiling Swift runner..."
swiftc -O \
    -target arm64-apple-macos13.0 \
    -o "$APP_PATH/Contents/MacOS/$EXECUTABLE_NAME" \
    "$SCRIPT_DIR/AppleNotesMCPRunner.swift"

echo "Writing Info.plist..."
cp "$SCRIPT_DIR/Info.plist.template" "$APP_PATH/Contents/Info.plist"

SIGN_IDENTITY=${NOTES_GRAPH_SIGN_IDENTITY:-}
if [ -z "$SIGN_IDENTITY" ]; then
    echo "WARNING: NOTES_GRAPH_SIGN_IDENTITY not set — using ad-hoc signing."
    echo "         FDA grants will not survive rebuilds. Run apple_notes_mcp_app/sign-setup.sh"
    echo "         to create a stable local signing identity."
    SIGN_IDENTITY="-"
fi

echo "Codesigning with identity: $SIGN_IDENTITY"
codesign --sign "$SIGN_IDENTITY" --force --timestamp=none --options runtime \
    --entitlements "$SCRIPT_DIR/AppleNotesMCP.entitlements" "$APP_PATH"
codesign --verify --verbose=2 "$APP_PATH"

echo ""
echo "Built: $APP_PATH"
