# Publishing on GitHub so teammates can install (no git needed for them)

Your teammates need **no GitHub account, no git, nothing technical** — they either
download from the Releases page in a browser, or paste one Terminal command.

You only do the steps below once to set it up, then re-do steps 5–6 for updates.

> **The repo must be PUBLIC** for the browser download and the one-liner to work
> without logins. There are no secrets in the repo — Trint keys are entered per
> user and the settings file + `bin/` + `dist/` are git-ignored. (If you must keep
> it private, the simple one-liner won't work for people without access.)

---

## 1. Create the GitHub repo (website, no commands)
1. Go to https://github.com/new
2. Name it e.g. `beam-yt-downloader`, set **Public**, click **Create repository**.
3. Copy the repo's `owner/name` (e.g. `janedoe/beam-yt-downloader`).

## 2. Point the installer at your repo
Edit `install.sh`, line near the top:
```
REPO="Mitul9703/beam-yt-downloader"        # e.g. REPO="janedoe/beam-yt-downloader"
```

## 3. Push the source code (you have git already)
```bash
cd "/Users/mitulkrishna/Documents/BEAM/YTDownloader"
git add -A
git commit -m "Beam YouTube Downloader app + installer"
git branch -M main
git remote add origin https://github.com/Mitul9703/beam-yt-downloader.git
git push -u origin main
```
(Only source is pushed — the big `bin/`, `dist/`, and `.buildvenv/` are ignored.)

## 4. Build the shareable zip (if not already done)
```bash
./fetch-binaries.sh                              # once
.buildvenv/bin/python setup.py py2app            # build the .app
./package-app.sh                                 # -> dist/BeamYouTubeDownloader-AppleSilicon.zip
```

## 5. Create a Release and attach the zip (website, drag-and-drop)
1. On your repo page: **Releases** (right side) → **Draft a new release**.
2. **Choose a tag** → type `v1.0` → "Create new tag on publish".
3. Title: `Beam YouTube Downloader v1.0`.
4. **Drag `dist/BeamYouTubeDownloader-AppleSilicon.zip` into the "Attach binaries" box.**
   Wait for the 123 MB upload to finish.
5. Click **Publish release**.

## 6. Give teammates ONE of these

**Easiest (smooth, no pop-ups)** — send them this exact line to paste in Terminal:
```
curl -fsSL https://raw.githubusercontent.com/Mitul9703/beam-yt-downloader/main/install.sh | bash
```

**Or browser download** — send them this link:
```
https://github.com/Mitul9703/beam-yt-downloader/releases/latest
```
They click the `.zip`, unzip, and follow the `INSTALL-README.txt` inside
(Option B: drag to Applications → System Settings → Privacy & Security → Open Anyway).

---

## Updating later (new yt-dlp / fixes)
```bash
./fetch-binaries.sh                              # refresh yt-dlp etc. (optional)
rm -rf build dist
.buildvenv/bin/python setup.py py2app
./package-app.sh
git add -A && git commit -m "update" && git push
```
Then make a new Release (step 5) with a new tag (e.g. `v1.1`) and attach the new zip.
The `latest` link and the one-liner automatically point at the newest release, so
teammates just re-run the same command to update.

## Notes
- This build is **Apple Silicon only**. For an Intel teammate, build on/for x86_64
  and attach a second zip named for Intel; ask and I'll set that up.
- The app is **unsigned / ad-hoc signed** (no paid Apple account). The one-liner
  avoids Gatekeeper because `curl` downloads aren't quarantined. The browser route
  hits the one-time "Open Anyway" step, which is covered in `INSTALL-README.txt`.
