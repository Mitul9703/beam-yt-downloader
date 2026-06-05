# YTDownloader

A local browser-based YouTube downloader for staff use.

## Why this version

Tkinter proved unreliable in packaged macOS builds on this machine, so this project now uses the browser as the UI shell while keeping all downloads local on the user's own Mac.

- No Terminal workflow for staff
- No shared download server or shared queue
- Easier to test quickly
- Easy to wrap in a desktop shell later if we want

## What it does

- Paste a YouTube video or playlist link
- Toggle between `Single video` and `Playlist`
- Auto-detect when the pasted link does not match the selected mode
- Preview the title, channel name, detected type, item count, and video embed
- Choose `Video`, `Audio`, or `Audio + Video`
- Queue jobs locally on one machine
- Process playlist items sequentially with an automatic built-in pause
- Show live status updates with percentage and size/speed information when available
- For `Audio + Video`, download the video, extract MP3 audio, and create a zip bundle
- Show downloadable output links when a job completes

## Requirements

- Python 3.10+
- `yt-dlp` installed and available on your PATH

Optional:

- `node` so `yt-dlp` can handle modern YouTube JavaScript challenges more reliably
- `ffmpeg` for MP3 conversion

## Run it

```bash
python3 app.py
```

Then open:

```text
http://127.0.0.1:8765
```

If port `8765` is already in use:

```bash
YT_DOWNLOADER_PORT=8766 python3 app.py
```

## Install helpers

If `yt-dlp` is not installed:

```bash
brew install yt-dlp
```

If you want best format merging and richer media handling:

```bash
brew install ffmpeg
```

If `node` is missing:

```bash
brew install node
```

## Notes

- Files are saved into the local `downloads/` folder in this repo.
- The browser version does not offer an arbitrary folder picker. That can come back if we later wrap this UI in a desktop shell.
- A local app remains safer than a shared hosted website because it avoids concentrating traffic through one server/IP.

## Possible next steps

- Wrap this web UI into a desktop shell
- Restore folder picking in the wrapped desktop build
- Add cancel and retry controls
- Add thumbnail preview
