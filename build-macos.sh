#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script must be run on macOS." >&2
  exit 1
fi

for required_file in dictionary-server.json dictionary-server-ca.pem; do
  if [[ ! -s "$required_file" ]]; then
    echo "Missing required server configuration: $required_file" >&2
    exit 1
  fi
done

ICON="assets/icons/hanzilab.icns"
MACOS_ICON_SOURCE="assets/icons/hanzilab-macos.png"
# Recent macOS releases can reject otherwise valid hand-built iconsets. Let
# the system image service create the ICNS container directly instead. Its
# ICNS converter expects a 256 px source even though we keep a 1024 px master.
ICON_TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$ICON_TEMP_DIR"' EXIT
sips -z 256 256 "$MACOS_ICON_SOURCE" --out "$ICON_TEMP_DIR/hanzilab.png" >/dev/null
sips -s format icns "$ICON_TEMP_DIR/hanzilab.png" --out "$ICON" >/dev/null

python3 -m PyInstaller --noconfirm --clean HanziLab.spec

APP="dist/HanziLab.app"
[[ -d "$APP" ]] || { echo "Build completed without $APP" >&2; exit 1; }

"$APP/Contents/MacOS/HanziLab" --smoke-test
"$APP/Contents/MacOS/HanziLab" --server-check
"$APP/Contents/MacOS/HanziLab" --download-check

SIGNING_IDENTITY="${MACOS_SIGNING_IDENTITY:--}"
if [[ "$SIGNING_IDENTITY" == "-" ]]; then
  # Hardened runtime enables library validation. That rejects the Python.org
  # framework embedded by PyInstaller because it carries a different Team ID
  # from a local ad-hoc signature. Developer ID builds should remain hardened;
  # local builds must omit the runtime flag so they can actually launch.
  codesign --force --deep --sign "$SIGNING_IDENTITY" "$APP"
else
  codesign --force --deep --options runtime --sign "$SIGNING_IDENTITY" "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"

mkdir -p release
rm -f release/HanziLab-macOS.dmg
DMG_TEMP_DIR="$(mktemp -d)"
hdiutil create -volname HanziLab -srcfolder "$APP" -ov -format UDZO \
  "$DMG_TEMP_DIR/HanziLab-macOS.dmg"
mv "$DMG_TEMP_DIR/HanziLab-macOS.dmg" release/HanziLab-macOS.dmg
rmdir "$DMG_TEMP_DIR"

echo "Ready: release/HanziLab-macOS.dmg"
