#!/usr/bin/env bash
#
# Publishes the repo + a GitHub Release with the shareable zip.
# Requires: you have run `gh auth login` once (see chat / RELEASE.md).
#
# Usage:
#   ./publish.sh            # publishes tag v1.0 (or next, see below)
#   ./publish.sh v1.1       # publishes a specific tag
#
set -euo pipefail
cd "$(dirname "$0")"

REPO="Mitul9703/beam-yt-downloader"
TAG="${1:-v1.0}"
ZIP="dist/BeamYouTubeDownloader-AppleSilicon.zip"

command -v gh >/dev/null || { echo "gh not installed."; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run 'gh auth login' first."; exit 1; }
[ -f "$ZIP" ] || { echo "Missing $ZIP — run ./package-app.sh first."; exit 1; }

# Commit any pending changes.
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "Publish ${TAG}

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
fi
git branch -M main

# Create the repo (public) on first run, otherwise just ensure the remote exists.
if ! git remote get-url origin >/dev/null 2>&1; then
  if gh repo view "$REPO" >/dev/null 2>&1; then
    git remote add origin "https://github.com/${REPO}.git"
  else
    gh repo create "$REPO" --public --source=. --remote=origin \
      --description "Beam YouTube Downloader — local macOS app for journalists"
  fi
fi
git push -u origin main

# Create or refresh the release and attach the zip.
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release upload "$TAG" "$ZIP" --repo "$REPO" --clobber
else
  gh release create "$TAG" "$ZIP" --repo "$REPO" \
    --title "Beam YouTube Downloader ${TAG}" \
    --notes "Apple Silicon (M1/M2/M3/M4) build. Install instructions are in INSTALL-README.txt inside the zip, or run the one-liner from the README."
fi

echo
echo "Published. Share ONE of these with teammates:"
echo "  curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | bash"
echo "  https://github.com/${REPO}/releases/latest"
