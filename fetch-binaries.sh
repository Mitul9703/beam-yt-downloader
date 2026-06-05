#!/usr/bin/env bash
#
# Downloads self-contained (statically linked / standalone) copies of the
# helper binaries the app shells out to, into ./bin. These are what get bundled
# into the packaged .app so it runs on a teammate's Mac that has no Homebrew,
# conda, or Node installed.
#
# Re-run this whenever you want to refresh yt-dlp (YouTube changes often).
#
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p bin

ARCH="$(uname -m)"
case "$ARCH" in
  arm64)  FF_ARCH="arm64"; NODE_ARCH="arm64" ;;
  x86_64) FF_ARCH="x64";   NODE_ARCH="x64" ;;
  *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

FF_TAG="b6.1.1"
NODE_VER="v22.13.1"

echo "Architecture: $ARCH"

echo "==> yt-dlp (standalone, universal macOS build)"
curl -L --fail --progress-bar -o bin/yt-dlp \
  "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"

echo "==> ffmpeg ($FF_ARCH)"
curl -L --fail --progress-bar -o bin/ffmpeg.gz \
  "https://github.com/eugeneware/ffmpeg-static/releases/download/${FF_TAG}/ffmpeg-darwin-${FF_ARCH}.gz"
gunzip -f bin/ffmpeg.gz

echo "==> ffprobe ($FF_ARCH)"
curl -L --fail --progress-bar -o bin/ffprobe.gz \
  "https://github.com/eugeneware/ffmpeg-static/releases/download/${FF_TAG}/ffprobe-darwin-${FF_ARCH}.gz"
gunzip -f bin/ffprobe.gz

echo "==> node ($NODE_ARCH)"
curl -L --fail --progress-bar -o bin/node.tar.gz \
  "https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-darwin-${NODE_ARCH}.tar.gz"
tar -xzf bin/node.tar.gz -C bin
cp "bin/node-${NODE_VER}-darwin-${NODE_ARCH}/bin/node" bin/node
rm -rf "bin/node-${NODE_VER}-darwin-${NODE_ARCH}" bin/node.tar.gz

chmod +x bin/yt-dlp bin/ffmpeg bin/ffprobe bin/node

# Remove the quarantine flag so the binaries run during local (unsigned) testing.
xattr -dr com.apple.quarantine bin 2>/dev/null || true

echo
echo "Done. Self-contained binaries in ./bin:"
ls -lh bin
