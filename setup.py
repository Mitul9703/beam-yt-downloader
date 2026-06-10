from __future__ import annotations

from pathlib import Path

from setuptools import setup


APP = ["app.py"]
ROOT = Path(__file__).resolve().parent
BIN_DIR = ROOT / "bin"

# Bundle the self-contained binaries from ./bin (populated by fetch-binaries.sh).
# Using these instead of whatever is on PATH is what makes the .app portable to a
# Mac with no Homebrew / conda / Node installed.
BUNDLED_NAMES = ["yt-dlp", "ffmpeg", "ffprobe", "node"]
_bundled = [str(BIN_DIR / name) for name in BUNDLED_NAMES if (BIN_DIR / name).exists()]

_missing = [name for name in BUNDLED_NAMES if not (BIN_DIR / name).exists()]
if _missing:
    raise SystemExit(
        "Missing bundled binaries in ./bin: "
        + ", ".join(_missing)
        + "\nRun ./fetch-binaries.sh first to download self-contained copies."
    )

resource_entries = [("bin", _bundled)]

# Bake in the team's shared-queue endpoint (gitignored, so the token never lands
# in the public repo). Landing it in Resources/ means resource_root() finds it at
# runtime. Optional: if absent, the app simply ships with the queue off.
_queue_cfg = ROOT / "queue_config.json"
if _queue_cfg.exists():
    resource_entries.append(str(_queue_cfg))

OPTIONS = {
    "argv_emulation": False,
    # Do NOT strip bundled binaries: stripping destroys the appended archive
    # inside the standalone (PyInstaller-built) yt-dlp and breaks it.
    "strip": False,
    "plist": {
        "CFBundleName": "Beam YouTube Downloader",
        "CFBundleDisplayName": "Beam YouTube Downloader",
        "CFBundleIdentifier": "org.beam.ytdownloader",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
    },
    "resources": resource_entries,
    "packages": [],
}


setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
