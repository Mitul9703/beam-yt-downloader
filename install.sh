#!/bin/bash
#
# Beam Downloader — one-command installer for Apple Silicon Macs.
#
# Teammates run (nothing to install, no git, no GitHub account):
#
#   curl -fsSL https://github.com/OWNER/REPO/releases/latest/download/install.sh | bash
#
# Because curl (not a browser) fetches the app, macOS does not quarantine it,
# so it opens with no Gatekeeper pop-up.
#
REPO="Mitul9703/beam-yt-downloader"

set -euo pipefail

ASSET="BeamDownloader-AppleSilicon.zip"
URL="https://github.com/${REPO}/releases/latest/download/${ASSET}"
APP_NAME="Beam Downloader.app"
DEST="/Applications/${APP_NAME}"

if [ "$(uname -m)" != "arm64" ]; then
  echo "This build is for Apple Silicon Macs (M1/M2/M3/M4)."
  echo "Your Mac reports: $(uname -m). Ask for the Intel build."
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading Beam Downloader..."
curl -L --fail --progress-bar -o "$TMP/app.zip" "$URL"

echo "Installing into Applications..."
ditto -x -k "$TMP/app.zip" "$TMP/unzipped"
SRC_APP="$(/usr/bin/find "$TMP/unzipped" -maxdepth 2 -name '*.app' -print -quit)"
if [ -z "$SRC_APP" ]; then
  echo "Could not find the app inside the download. Aborting."
  exit 1
fi

rm -rf "$DEST"
ditto "$SRC_APP" "$DEST"
# Belt-and-suspenders: clear any quarantine flag so it opens cleanly.
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

echo "Done. Opening the app..."
open "$DEST"
echo
echo "Installed to: $DEST"
echo "To quit later: right-click its icon in the Dock > Quit."
