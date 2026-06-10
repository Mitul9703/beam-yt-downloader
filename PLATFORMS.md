# Platforms, releases, and migration playbook

This is the operational playbook for releasing Beam YouTube Downloader, adding
new platforms later, moving the project to a different GitHub account, and
shipping a **required** ("you must update to keep using the app") release.

Read this before renaming anything or changing accounts — a couple of small
rules keep the in-app auto-updater working for everyone.

---

## 1. The golden rules

1. **Never rename the Apple Silicon asset.** Every installed Mac app's updater
   downloads a file named exactly:

   ```
   BeamYouTubeDownloader-AppleSilicon.zip
   ```

   from the *latest* release. Rename or remove it and existing apps can't
   auto-update (they fail safe — they keep working but stop updating, and those
   users would have to re-run the installer once).

2. **One version number across all platforms.** A release tag (e.g. `v1.6`) is a
   single version, built from the same source. Different platforms get different
   **files** attached to that one release, never different version numbers.

3. **Only tag a release once every supported platform's asset is built and
   tested.** If `v1.6` is "latest" but is missing a platform's zip, apps on that
   platform will try to fetch a file that isn't there and show "update failed."

4. **Adding a platform is purely additive.** Attaching `…-Windows.zip` to a
   release does not touch Mac users in any way, as long as rule #1 holds.

---

## 2. Asset naming scheme

One release, one zip per supported platform:

| Platform                     | Asset filename                          | Built today? |
|------------------------------|------------------------------------------|--------------|
| macOS Apple Silicon (arm64)  | `BeamYouTubeDownloader-AppleSilicon.zip` | ✅ yes        |
| macOS Intel (x86_64)         | `BeamYouTubeDownloader-Intel.zip`        | ❌ not yet    |
| Windows                      | `BeamYouTubeDownloader-Windows.zip`      | ❌ not yet    |

These names are a commitment. If you add Intel/Windows builds later, use exactly
these names so the convention stays stable.

> We deliberately do **not** ship empty/placeholder assets. An asset that exists
> is a promise that it works — the updater will download and install whatever is
> there. Only attach a platform's zip when it's a real, tested build.

---

## 3. Cutting a normal release (current: macOS Apple Silicon)

From a clean checkout on Apple Silicon:

```bash
# 1. Bump APP_VERSION in app.py (e.g. "1.5" -> "1.6").
# 2. Build, package, publish:
rm -rf build dist
.buildvenv/bin/python setup.py py2app          # builds the .app
./package-app.sh                               # ad-hoc signs + zips to dist/
./publish.sh v1.6                              # pushes + creates the GitHub release
```

Then set human-readable release notes (these show up in the app's **"What's
new"** screen):

```bash
gh release edit v1.6 --repo <OWNER>/<REPO> --notes "$(cat <<'EOF'
### New in v1.6
- ...
EOF
)"
```

The "seed release" rule: any change to the **updater itself** (or to
`GITHUB_REPO`) only takes effect one version *after* it ships. The version that
introduces a change updates people *using the old logic*; the new logic drives
the *next* update.

---

## 4. Required ("forced") updates

Some releases must be installed before the app can be used again — most
importantly the **repo-migration** release (section 5). The app supports this:

- A release is marked required by putting this exact marker **anywhere in the
  GitHub release notes** (it's an HTML comment, so it's invisible to readers):

  ```
  <!--beam:mandatory-->
  ```

- When an installed app sees that the *latest* release is newer than itself
  **and** carries that marker, it shows a full-screen **"Update required"** gate
  and won't let the user download until they install it.

- **Fail-open by design:** the gate only appears when the app successfully
  reached GitHub and positively confirmed a required update. If GitHub is
  unreachable (offline), the app is **not** blocked — we never lock anyone out
  because of a network problem.

**Important timing:** the enforcement code lives *in the installed app*. So a
required update only works if the app the user already has was built with this
feature (v1.5+). That's why it was added before the team's first install.

To make a release required, just include the marker in its notes. To make a
normal optional release, leave the marker out.

---

## 5. Moving to the team's GitHub account (Path B migration)

Today the project lives under a personal account. When the team org is approved,
migrate like this — existing Mac users do **not** need to re-run any command.

**Where the repo is wired in (4 spots):**

- `app.py` → `GITHUB_REPO = "<OWNER>/<REPO>"` (the updater reads this)
- `install.sh` → `REPO="<OWNER>/<REPO>"`
- `publish.sh` → `REPO="<OWNER>/<REPO>"`
- the git `origin` remote

**Migration steps:**

1. Create/seed the new team repo (push the code there).
2. In a branch, change `GITHUB_REPO` in `app.py` to the **new** repo, plus the
   `REPO` vars in `install.sh` and `publish.sh`.
3. Cut a new version (e.g. `v1.6`) and **publish it to the OLD repo**
   (`./publish.sh` while `origin` still points at the old repo), and put the
   `<!--beam:mandatory-->` marker in its notes so it's a required update.
4. Installed apps check the OLD repo, see the required `v1.6`, and force-install
   it. After installing, they now point at the NEW repo.
5. Going forward, publish all releases to the **new** repo.
6. **Keep the OLD repo public and intact** until you're confident everyone has
   updated past the seed release. Do not delete or privatize it before then —
   stragglers still check it. (GitHub's transfer-redirect can help, but don't
   rely on it long-term.)

After migration, update the `origin` remote and the `<OWNER>/<REPO>` references
everywhere to the new repo for future releases.

---

## 6. Adding Intel Macs or Windows (when a real user shows up)

- **Intel Macs:** either a `universal2` Mac build (one app that runs on both
  Apple Silicon and Intel — needs a universal Python + universal ffmpeg/node) or
  a separate `x86_64` build attached as `…-Intel.zip`. The updater's swap logic
  is already macOS-correct, so this is mostly a build-toolchain task.
- **Windows:** a separate project — different packaging (`PyInstaller` `.exe`),
  Windows builds of yt-dlp/ffmpeg/node, and a **Windows** equivalent of the
  swap-and-relaunch updater (the current one uses `ditto`/`/Applications`/`open`
  and is macOS-only). Build and test it on an actual Windows machine before
  attaching `…-Windows.zip`.

In both cases: build + test + attach the correctly-named asset to a release that
also still has the Apple Silicon asset. Mac users are unaffected.
