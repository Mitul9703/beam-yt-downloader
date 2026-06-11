Beam Downloader — Install Guide (Apple Silicon Mac: M1/M2/M3/M4)
=======================================================================

You do NOT need GitHub, git, or any account to use this. Pick ONE of the two
ways below.


OPTION A — One Terminal command (smoothest, no security pop-ups)
----------------------------------------------------------------
1. Open the "Terminal" app:
   press Cmd-Space, type  Terminal , press Return.
2. Copy this entire line, paste it into the Terminal window, and press Return:

   curl -fsSL https://raw.githubusercontent.com/Mitul9703/beam-yt-downloader/main/install.sh | bash

3. The app downloads, installs into your Applications folder, and opens in
   your web browser. Done — no security pop-ups.


OPTION B — Manual (if you'd rather not use Terminal)
----------------------------------------------------
1. UNZIP: Double-click the zip file you downloaded (it's usually in your
   "Downloads" folder). It turns into a folder called
   "Beam Downloader (Mac)". Open that folder by double-clicking it.
   Inside you'll see the app "Beam Downloader" and this README.

2. MOVE IT TO APPLICATIONS:
   a. Open a new Finder window (click the blue smiley "Finder" icon in the Dock,
      the bar at the bottom of your screen).
   b. Find the "Applications" folder. It is in the left-hand sidebar of every
      Finder window. If you don't see it, click "Go" in the menu bar at the very
      top of the screen, then click "Applications".
   c. Drag the "Beam Downloader" app icon into that Applications folder.
   (This step is optional — you can also just run it from where it unzipped —
    but Applications is the tidy place for it.)

3. OPEN IT THE FIRST TIME:
   Double-click "Beam Downloader".
   macOS will pop up a warning that it "cannot be verified." This is expected —
   it's an in-house tool, not from the App Store. Click "Done" (NOT "Move to
   Trash").

4. ALLOW IT (one time only):
   a. Open "System Settings" (the grey gear icon in the Dock, or click the Apple
      logo at the top-left of the screen, then "System Settings").
   b. In the left list, click "Privacy & Security".
   c. Scroll down to the "Security" section. You'll see a line that says
      "Beam Downloader was blocked..." with an "Open Anyway" button.
      Click "Open Anyway".
   d. Confirm with your Mac password or Touch ID, then click "Open".
   (On older versions of macOS you won't see "Open Anyway" — instead, find the
    app in your Applications folder, RIGHT-click it, choose "Open", then "Open".)

5. DONE: It opens in your web browser — that browser page IS the app.
   From now on you can just double-click it normally; no more warnings.


HOW TO USE IT
-------------
- Paste a YouTube video or playlist link.
- Choose Audio, Video, or both, and pick a folder to save into.
- Optional: tick "Upload to Trint after download" and choose a Trint folder.
  (You add your own personal Trint key once, under the gear/Settings icon.)
- To QUIT the app: right-click its icon in the Dock (bottom bar of the screen)
  and choose "Quit". Just closing the browser tab does NOT stop it.


OPENING IT AGAIN LATER
----------------------
Once it's installed, it's a normal app — you do NOT download it or run any
command again. To open it any time:
 - Press Cmd-Space, type "Beam", press Return  (this is Spotlight search), OR
 - Open your Applications folder and double-click it, OR
 - Find it in Launchpad (the rocket icon).

(Only re-run the install command / re-download if your colleague tells you a
newer version is available.)


Trouble opening it? As a last resort, paste this one line into Terminal:
   xattr -dr com.apple.quarantine "/Applications/Beam Downloader.app"
then double-click the app again.

Questions? Ask the colleague who sent you this.
