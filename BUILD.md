# Building the Beam Downloader macOS app

The shipped app **is `app.py`** (the local web app). py2app wraps it into a `.app`
that, when double-clicked, starts a local server on a free port and opens the UI
in the browser automatically. `desktop_app.py` is the old Tkinter prototype and is
no longer the build target.

## One-time setup

```bash
# 1. Build virtualenv with py2app (the app itself is pure stdlib).
python3 -m venv .buildvenv
.buildvenv/bin/python -m pip install --upgrade pip py2app

# 2. Download self-contained helper binaries into ./bin
#    (standalone yt-dlp + static ffmpeg/ffprobe + standalone node).
./fetch-binaries.sh
```

## Build

```bash
rm -rf build dist
.buildvenv/bin/python setup.py py2app
# Result: "dist/Beam Downloader.app"  (~270 MB)
```

## Quick local sanity check

```bash
EXE="dist/Beam Downloader.app/Contents/MacOS/Beam Downloader"
YT_DOWNLOADER_PORT=8770 YT_DOWNLOADER_NO_BROWSER=1 "$EXE" &
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8770/   # expect 200
```

Logs/settings/temp for the packaged app live in
`~/Library/Application Support/BeamYTDownloader/`.

## Important gotchas

- **`strip: False` is required** in `setup.py`. py2app strips Mach-O files by
  default, which destroys the appended archive inside the standalone (PyInstaller)
  `yt-dlp` and silently breaks it. Leave stripping off.
- **Bundle self-contained binaries, never Homebrew/conda ones.** Homebrew `ffmpeg`
  links dylibs under `/opt/homebrew`, and conda `yt-dlp` is a shebang script tied
  to a conda Python — both fail on a teammate's clean Mac. `fetch-binaries.sh`
  pulls portable builds instead.
- **Architecture.** This builds for the architecture of the Python you build with.
  On Apple Silicon you get an **arm64** app. Intel Macs need an x86_64 build
  (run the same steps under an x86_64 Python; `fetch-binaries.sh` auto-selects the
  matching binaries). A universal2 build needs a universal2 Python.

## Distribution to teammates (Gatekeeper)

The `.app` is **unsigned**, so on another Mac macOS will block it
("unidentified developer"). Options:

- **Quick/free:** teammate right-clicks the app → **Open** → **Open** (only needed
  once). Tell them to do this; a normal double-click will be blocked.
- **Proper:** code-sign + notarize with an Apple Developer account ($99/yr):
  `codesign --deep --force --options runtime --sign "Developer ID Application: <you>" "dist/Beam Downloader.app"`
  then submit with `xcrun notarytool` and `xcrun stapler staple`. This gives a
  clean double-click experience and is recommended if more than a couple of people
  will use it.
