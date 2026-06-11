#!/usr/bin/env bash
#
# Builds the shareable zip: ad-hoc signs the .app, bundles INSTALL-README.txt
# next to it, and zips with ditto so the bundle survives transfer.
#
# Prereqs: you've already run  .buildvenv/bin/python setup.py py2app  so that
# "dist/Beam Downloader.app" exists.
#
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/Beam Downloader.app"
ZIP="dist/BeamDownloader-AppleSilicon.zip"
STAGE="dist/Beam Downloader (Mac)"

[ -d "$APP" ] || { echo "Build first: .buildvenv/bin/python setup.py py2app"; exit 1; }

echo "Ad-hoc signing (no Apple Developer account needed)..."
codesign --force --deep --sign - "$APP"
codesign --verify --verbose "$APP" >/dev/null && echo "  signature OK"

echo "Staging app + README..."
rm -rf "$STAGE"; mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Beam Downloader.app"
cp INSTALL-README.txt "$STAGE/INSTALL-README.txt"

echo "Zipping..."
rm -f "$ZIP"
ditto -c -k --keepParent "$STAGE" "$ZIP"
rm -rf "$STAGE"

echo
echo "Created: $ZIP"
ls -lh "$ZIP"
echo
echo "Upload this zip AND install.sh as assets on a GitHub Release (see RELEASE.md)."
