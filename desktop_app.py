#!/usr/bin/env python3

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ModuleNotFoundError:
    tk = None
    filedialog = None
    messagebox = None


APP_TITLE = "Beam YouTube Downloader"
DEFAULT_DELAY_SECONDS = 3
WINDOW_BG = "#2b2f31"
CARD_BG = "#3a3f41"
TEXT_PRIMARY = "#f4f4f4"
TEXT_MUTED = "#d0d0d0"
ACCENT = "#bc5f2c"
INPUT_BG = "#ffffff"
INPUT_FG = "#111111"
BORDER = "#545b5e"


def app_resource_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root)
    if getattr(sys, "frozen", False):
        executable_path = Path(sys.executable).resolve()
        return executable_path.parent.parent / "Resources"
    return Path(__file__).resolve().parent


def resolve_binary(name: str) -> str | None:
    resource_root = app_resource_root()
    candidates = [
        resource_root / "bin" / name,
        resource_root / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


YT_DLP = resolve_binary("yt-dlp")
NODE = resolve_binary("node")
FFMPEG = resolve_binary("ffmpeg")


def app_support_dir() -> Path:
    support_dir = Path.home() / "Library" / "Application Support" / "BeamYTDownloader"
    support_dir.mkdir(parents=True, exist_ok=True)
    return support_dir


def startup_log_path() -> Path:
    return app_support_dir() / "startup.log"


def write_startup_log(message: str) -> None:
    with startup_log_path().open("a", encoding="utf-8") as handle:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(f"[{timestamp}] {message}\n")


@dataclass
class PreviewData:
    input_url: str
    detected_kind: str
    title: str
    channel: str
    item_count: int
    first_video_url: str | None = None
    warning: str = ""


@dataclass
class DownloadJob:
    url: str
    requested_mode: str
    media_type: str
    output_dir: str
    preview: PreviewData
    id: int = field(default=0)


class DownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(900, 700)

        self.mode_var = tk.StringVar(value="single")
        self.media_var = tk.StringVar(value="video")
        self.output_dir_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.delay_var = tk.IntVar(value=DEFAULT_DELAY_SECONDS)
        self.status_var = tk.StringVar(value="Ready")
        self.preview_title_var = tk.StringVar(value="No preview loaded yet")
        self.preview_channel_var = tk.StringVar(value="-")
        self.preview_kind_var = tk.StringVar(value="-")
        self.preview_count_var = tk.StringVar(value="-")
        self.preview_url_var = tk.StringVar(value="-")
        self.preview_warning_var = tk.StringVar(value="")
        self.queue_var = tk.StringVar(value="No queued jobs")
        self.active_job: DownloadJob | None = None
        self.current_preview: PreviewData | None = None
        self.job_counter = 1
        self.job_queue: queue.Queue[DownloadJob] = queue.Queue()
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.preview_lock = threading.Lock()
        self.download_worker_started = False

        self._build_ui()
        self._start_download_worker()
        self._pump_ui_queue()

    def _build_ui(self) -> None:
        self.root.configure(bg=WINDOW_BG)

        outer = tk.Frame(self.root, bg=WINDOW_BG, padx=20, pady=20)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer,
            text=APP_TITLE,
            bg=WINDOW_BG,
            fg=TEXT_PRIMARY,
            font=("Helvetica", 28, "bold"),
        ).pack(anchor="w")
        tk.Label(
            outer,
            text="Paste a YouTube link, preview what it is, choose where files should go, and download sequentially with visible status updates.",
            bg=WINDOW_BG,
            fg=TEXT_MUTED,
            font=("Helvetica", 12),
            wraplength=900,
            justify="left",
        ).pack(anchor="w", pady=(4, 18))

        input_card = tk.LabelFrame(
            outer,
            text="Input",
            bg=CARD_BG,
            fg=TEXT_PRIMARY,
            padx=16,
            pady=16,
            font=("Helvetica", 12, "bold"),
            bd=1,
            relief="groove",
        )
        input_card.pack(fill="x", pady=(0, 14))

        tk.Label(input_card, text="YouTube link", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).grid(row=0, column=0, sticky="w")
        self.url_input = tk.Text(
            input_card,
            height=3,
            font=("Helvetica", 13),
            relief="sunken",
            bd=2,
            bg=INPUT_BG,
            fg=INPUT_FG,
            insertbackground=INPUT_FG,
            wrap="word",
        )
        self.url_input.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 12))

        tk.Label(input_card, text="Mode", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).grid(row=2, column=0, sticky="w")
        mode_frame = tk.Frame(input_card, bg=CARD_BG)
        mode_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 12))
        tk.Radiobutton(mode_frame, text="Single video", value="single", variable=self.mode_var, bg=CARD_BG, fg=TEXT_PRIMARY, activebackground=CARD_BG, activeforeground=TEXT_PRIMARY, selectcolor=ACCENT).pack(side="left")
        tk.Radiobutton(mode_frame, text="Playlist", value="playlist", variable=self.mode_var, bg=CARD_BG, fg=TEXT_PRIMARY, activebackground=CARD_BG, activeforeground=TEXT_PRIMARY, selectcolor=ACCENT).pack(side="left", padx=(12, 0))

        tk.Label(input_card, text="Download format", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).grid(row=2, column=2, sticky="w")
        media_frame = tk.Frame(input_card, bg=CARD_BG)
        media_frame.grid(row=3, column=2, sticky="w", pady=(6, 12))
        tk.Radiobutton(media_frame, text="Video", value="video", variable=self.media_var, bg=CARD_BG, fg=TEXT_PRIMARY, activebackground=CARD_BG, activeforeground=TEXT_PRIMARY, selectcolor=ACCENT).pack(side="left")
        tk.Radiobutton(media_frame, text="Audio", value="audio", variable=self.media_var, bg=CARD_BG, fg=TEXT_PRIMARY, activebackground=CARD_BG, activeforeground=TEXT_PRIMARY, selectcolor=ACCENT).pack(side="left", padx=(12, 0))
        tk.Radiobutton(media_frame, text="Audio + Video", value="both", variable=self.media_var, bg=CARD_BG, fg=TEXT_PRIMARY, activebackground=CARD_BG, activeforeground=TEXT_PRIMARY, selectcolor=ACCENT).pack(side="left", padx=(12, 0))

        tk.Label(input_card, text="Pause between playlist items (seconds)", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).grid(row=2, column=3, sticky="w")
        delay_spinbox = tk.Spinbox(
            input_card,
            from_=0,
            to=30,
            textvariable=self.delay_var,
            width=6,
            font=("Helvetica", 12),
            relief="solid",
            bd=1,
            bg=INPUT_BG,
            fg=INPUT_FG,
            insertbackground=INPUT_FG,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        delay_spinbox.grid(row=3, column=3, sticky="w", pady=(6, 12))

        tk.Label(input_card, text="Download folder", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).grid(row=4, column=0, sticky="w")
        self.folder_display = tk.Label(
            input_card,
            textvariable=self.output_dir_var,
            font=("Helvetica", 12),
            relief="sunken",
            bd=2,
            bg=INPUT_BG,
            fg=INPUT_FG,
            anchor="w",
            justify="left",
            padx=8,
            pady=8,
        )
        self.folder_display.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        tk.Button(input_card, text="Choose Folder", command=self._choose_folder, bg=ACCENT, fg="white", activebackground="#8e431b", activeforeground="white").grid(row=5, column=3, sticky="e", padx=(8, 0))

        action_row = tk.Frame(input_card, bg=CARD_BG)
        action_row.grid(row=6, column=0, columnspan=4, sticky="w", pady=(16, 0))
        tk.Button(action_row, text="Load Preview", command=self._load_preview, bg=ACCENT, fg="white", activebackground="#8e431b", activeforeground="white").pack(side="left")
        tk.Button(action_row, text="Add To Queue", command=self._queue_current_input, bg=ACCENT, fg="white", activebackground="#8e431b", activeforeground="white").pack(side="left", padx=(12, 0))

        for column in range(4):
            input_card.columnconfigure(column, weight=1)

        middle = tk.Frame(outer, bg=WINDOW_BG)
        middle.pack(fill="both", expand=True)
        middle.columnconfigure(0, weight=1)
        middle.columnconfigure(1, weight=1)
        middle.rowconfigure(0, weight=1)

        preview_card = tk.LabelFrame(
            middle,
            text="Preview",
            bg=CARD_BG,
            fg=TEXT_PRIMARY,
            padx=16,
            pady=16,
            font=("Helvetica", 12, "bold"),
            bd=1,
            relief="groove",
        )
        preview_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))

        self._add_preview_row(preview_card, 0, "Title", self.preview_title_var)
        self._add_preview_row(preview_card, 1, "Channel", self.preview_channel_var)
        self._add_preview_row(preview_card, 2, "Detected type", self.preview_kind_var)
        self._add_preview_row(preview_card, 3, "Items", self.preview_count_var)
        self._add_preview_row(preview_card, 4, "Preview link", self.preview_url_var)

        tk.Label(preview_card, textvariable=self.preview_warning_var, bg=CARD_BG, fg=TEXT_MUTED, font=("Helvetica", 11), wraplength=400, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        preview_card.columnconfigure(1, weight=1)

        queue_card = tk.LabelFrame(
            middle,
            text="Queue",
            bg=CARD_BG,
            fg=TEXT_PRIMARY,
            padx=16,
            pady=16,
            font=("Helvetica", 12, "bold"),
            bd=1,
            relief="groove",
        )
        queue_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(queue_card, text="Active status", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).pack(anchor="w")
        tk.Label(queue_card, textvariable=self.status_var, bg=CARD_BG, fg=TEXT_MUTED, font=("Helvetica", 11), wraplength=400, justify="left").pack(anchor="w", pady=(4, 14))
        tk.Label(queue_card, text="Queued jobs", bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).pack(anchor="w")
        tk.Label(queue_card, textvariable=self.queue_var, bg=CARD_BG, fg=TEXT_MUTED, font=("Helvetica", 11), wraplength=400, justify="left").pack(anchor="w", pady=(4, 0))

        log_card = tk.LabelFrame(
            outer,
            text="Live Updates",
            bg=CARD_BG,
            fg=TEXT_PRIMARY,
            padx=16,
            pady=16,
            font=("Helvetica", 12, "bold"),
            bd=1,
            relief="groove",
        )
        log_card.pack(fill="both", expand=True, pady=(14, 0))
        self.log_text = tk.Text(log_card, height=18, wrap="word", font=("Menlo", 11), bg=INPUT_BG, fg=INPUT_FG, insertbackground=INPUT_FG, relief="solid", bd=1)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

        self._append_log("App ready. Load a preview before adding a job so the user can verify the title, channel, and detected link type.")
        if not YT_DLP:
            self._append_log("yt-dlp was not found on this machine. The app cannot run downloads until it is installed.")
        if not NODE:
            self._append_log("Node was not found. Some YouTube links may be less reliable without a JavaScript runtime.")
        if not FFMPEG:
            self._append_log("ffmpeg was not found. Video downloads still work, but MP3 conversion is unavailable until ffmpeg is installed.")

    def _add_preview_row(self, parent: tk.LabelFrame, row: int, label: str, value_var: tk.StringVar) -> None:
        tk.Label(parent, text=label, bg=CARD_BG, fg=TEXT_PRIMARY, font=("Helvetica", 12)).grid(row=row, column=0, sticky="nw", pady=(0, 8))
        tk.Label(parent, textvariable=value_var, bg=CARD_BG, fg=TEXT_MUTED, font=("Helvetica", 11), wraplength=290, justify="left").grid(row=row, column=1, sticky="nw", pady=(0, 8), padx=(14, 0))

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.home()))
        if selected:
            self.output_dir_var.set(selected)
            self._append_log(f"Download folder set to: {selected}")

    def _current_url(self) -> str:
        return self.url_input.get("1.0", "end").strip()

    def _load_preview(self) -> None:
        url = self._current_url()
        if not url:
            messagebox.showerror(APP_TITLE, "Paste a YouTube link first.")
            return
        if not YT_DLP:
            messagebox.showerror(APP_TITLE, "yt-dlp is not installed on this machine.")
            return

        self.status_var.set("Loading preview...")
        self._append_log("Fetching preview metadata...")

        def worker() -> None:
            try:
                preview = fetch_preview(url, self.mode_var.get())
                self.ui_queue.put(("preview_loaded", preview))
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("preview_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _queue_current_input(self) -> None:
        url = self._current_url()
        output_dir = self.output_dir_var.get().strip()
        requested_mode = self.mode_var.get()

        if not url:
            messagebox.showerror(APP_TITLE, "Paste a YouTube link first.")
            return
        if not output_dir:
            messagebox.showerror(APP_TITLE, "Choose a download folder first.")
            return
        if not Path(output_dir).exists():
            messagebox.showerror(APP_TITLE, "The selected download folder does not exist.")
            return
        if not self.current_preview or self.current_preview.input_url != url:
            messagebox.showinfo(APP_TITLE, "Load preview first so the user can confirm the title and channel before queueing.")
            return
        if self.media_var.get() == "both" and not FFMPEG:
            messagebox.showerror(APP_TITLE, "Audio + Video requires ffmpeg so the app can extract audio and build the zip archive.")
            return

        job = DownloadJob(
            url=url,
            requested_mode=requested_mode,
            media_type=self.media_var.get(),
            output_dir=output_dir,
            preview=self.current_preview,
            id=self.job_counter,
        )
        self.job_counter += 1
        self.job_queue.put(job)
        self._refresh_queue_summary()
        self._append_log(f"Queued job #{job.id}: {job.preview.title} ({job.media_type})")
        self.status_var.set("Job queued")

    def _start_download_worker(self) -> None:
        if self.download_worker_started:
            return
        self.download_worker_started = True

        def worker() -> None:
            while True:
                job = self.job_queue.get()
                self.active_job = job
                self.ui_queue.put(("job_started", job))
                try:
                    run_job(job, self.delay_var.get(), self.ui_queue)
                    self.ui_queue.put(("job_finished", job))
                except Exception as exc:  # noqa: BLE001
                    self.ui_queue.put(("job_failed", (job, str(exc))))
                finally:
                    self.active_job = None
                    self.job_queue.task_done()
                    self.ui_queue.put(("queue_changed", None))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_queue_summary(self) -> None:
        pending = list(self.job_queue.queue)
        if not pending:
            self.queue_var.set("No queued jobs")
            return
        summary = [f"#{job.id}: {job.preview.title} ({job.media_type})" for job in pending[:6]]
        if len(pending) > 6:
            summary.append(f"... and {len(pending) - 6} more")
        self.queue_var.set("\n".join(summary))

    def _pump_ui_queue(self) -> None:
        try:
            while True:
                event, payload = self.ui_queue.get_nowait()
                if event == "preview_loaded":
                    self._apply_preview(payload)
                elif event == "preview_error":
                    self.status_var.set("Preview failed")
                    self._append_log(f"Preview failed: {payload}")
                    messagebox.showerror(APP_TITLE, payload)
                elif event == "job_started":
                    job = payload
                    self.status_var.set(f"Downloading job #{job.id}: {job.preview.title}")
                    self._append_log(f"Started job #{job.id}: {job.preview.title} ({job.media_type})")
                elif event == "job_finished":
                    job = payload
                    self.status_var.set(f"Finished job #{job.id}: {job.preview.title}")
                    self._append_log(f"Finished job #{job.id}: {job.preview.title}")
                elif event == "job_failed":
                    job, error = payload
                    self.status_var.set(f"Job #{job.id} failed")
                    self._append_log(f"Job #{job.id} failed: {error}")
                elif event == "status":
                    self.status_var.set(str(payload))
                    self._append_log(str(payload))
                elif event == "queue_changed":
                    self._refresh_queue_summary()
        except queue.Empty:
            pass
        self.root.after(150, self._pump_ui_queue)

    def _apply_preview(self, preview: PreviewData) -> None:
        self.current_preview = preview
        self.preview_title_var.set(preview.title)
        self.preview_channel_var.set(preview.channel)
        self.preview_kind_var.set(preview.detected_kind)
        self.preview_count_var.set(str(preview.item_count))
        self.preview_url_var.set(preview.first_video_url or preview.input_url)
        self.preview_warning_var.set(preview.warning)
        if preview.warning:
            self.status_var.set("Preview loaded with a mode mismatch warning")
            self._append_log(preview.warning)
        else:
            self.status_var.set("Preview loaded")
        self._append_log(f"Preview ready: {preview.title} by {preview.channel}")

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def yt_dlp_base_command() -> list[str]:
    if not YT_DLP:
        raise RuntimeError("yt-dlp is not installed on this machine.")
    command = [YT_DLP]
    if NODE:
        command.extend(["--js-runtimes", "node"])
    return command


def fetch_preview(url: str, requested_mode: str) -> PreviewData:
    command = yt_dlp_base_command()
    command.extend(["--dump-single-json", "--skip-download", "--flat-playlist", url])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "Preview failed."
        raise RuntimeError(stderr)

    data = json.loads(completed.stdout)
    entries = data.get("entries") or []
    detected_kind = "playlist" if data.get("_type") == "playlist" or len(entries) > 1 else "single"
    warning = ""

    if requested_mode != detected_kind:
        requested_label = "playlist" if requested_mode == "playlist" else "single video"
        detected_label = "playlist" if detected_kind == "playlist" else "single video"
        warning = f"You selected {requested_label}, but this link looks like a {detected_label}. The app will use the detected type."

    if detected_kind == "playlist":
        first = entries[0] if entries else {}
        first_video_url = build_video_url_from_entry(first) if first else None
        title = data.get("title") or "Untitled playlist"
        channel = data.get("channel") or data.get("uploader") or "Unknown channel"
        item_count = len(entries)
    else:
        first_video_url = data.get("webpage_url") or url
        title = data.get("title") or "Untitled video"
        channel = data.get("channel") or data.get("uploader") or "Unknown channel"
        item_count = 1

    return PreviewData(
        input_url=url,
        detected_kind=detected_kind,
        title=title,
        channel=channel,
        item_count=item_count,
        first_video_url=first_video_url,
        warning=warning,
    )


def build_video_url_from_entry(entry: dict[str, Any]) -> str:
    webpage_url = entry.get("url")
    if isinstance(webpage_url, str) and webpage_url.startswith("http"):
        return webpage_url
    if entry.get("id"):
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return ""


def run_job(job: DownloadJob, delay_seconds: int, ui_queue: queue.Queue[tuple[str, Any]]) -> None:
    preview = job.preview
    if preview.detected_kind == "playlist":
        run_playlist_job(job, delay_seconds, ui_queue)
    else:
        ui_queue.put(("status", f"Downloading single video: {preview.title}"))
        download_video(job.url, job.output_dir, job.media_type, ui_queue, job.preview)


def run_playlist_job(job: DownloadJob, delay_seconds: int, ui_queue: queue.Queue[tuple[str, Any]]) -> None:
    ui_queue.put(("status", f"Preparing playlist: {job.preview.title}"))
    entries = fetch_playlist_entries(job.url)
    total = len(entries)
    if total == 0:
        raise RuntimeError("Playlist preview succeeded, but no downloadable videos were found.")

    for index, entry in enumerate(entries, start=1):
        video_url = build_video_url_from_entry(entry)
        video_title = entry.get("title") or f"Video {index}"
        if not video_url:
            ui_queue.put(("status", f"Skipping item {index} because no video URL was found."))
            continue

        ui_queue.put(("status", f"Downloading playlist item {index} of {total}: {video_title}"))
        item_preview = PreviewData(
            input_url=video_url,
            detected_kind="single",
            title=video_title,
            channel=entry.get("channel") or entry.get("uploader") or job.preview.channel,
            item_count=1,
            first_video_url=video_url,
        )
        download_video(video_url, job.output_dir, job.media_type, ui_queue, item_preview, playlist_context=job.preview.title)

        if index < total and delay_seconds > 0:
            for remaining in range(delay_seconds, 0, -1):
                ui_queue.put(
                    (
                        "status",
                        f"Waiting {remaining}s before the next playlist item to keep requests paced and avoid bursty traffic.",
                    )
                )
                time.sleep(1)


def fetch_playlist_entries(url: str) -> list[dict[str, Any]]:
    command = yt_dlp_base_command()
    command.extend(["--dump-single-json", "--skip-download", "--flat-playlist", url])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "Could not load playlist entries."
        raise RuntimeError(stderr)
    data = json.loads(completed.stdout)
    return data.get("entries") or []


def sanitize_name(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in (" ", "-", "_", ".", "(", ")")).strip()
    return cleaned[:120] or "download"


def build_job_folder(base_output_dir: str, preview: PreviewData, playlist_context: str | None = None) -> Path:
    base = Path(base_output_dir)
    if playlist_context:
        folder_name = sanitize_name(playlist_context)
    else:
        folder_name = sanitize_name(preview.title)
    folder = base / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def extract_watch_id(url: str) -> str:
    parsed = urlparse(url)
    video_id = parse_qs(parsed.query).get("v", [""])[0]
    return video_id.strip()


def build_output_template(folder: Path, preview: PreviewData, playlist_context: str | None = None) -> str:
    if playlist_context:
        video_id = extract_watch_id(preview.input_url)
        prefix = sanitize_name(preview.title)
        if video_id:
            prefix = f"{prefix} [{video_id}]"
        return str(folder / f"{prefix}.%(ext)s")
    return str(folder / "%(title)s.%(ext)s")


def run_yt_dlp_capture_paths(command: list[str], ui_queue: queue.Queue[tuple[str, Any]]) -> list[Path]:
    completed_paths: list[Path] = []
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("MOVE:"):
            completed_paths.append(Path(line.split("MOVE:", 1)[1].strip()))
            continue
        if "[download]" in line and "%" in line:
            ui_queue.put(("status", line))
        elif any(token in line for token in ("Destination:", "Merging formats", "Deleting original file", "ExtractAudio")):
            ui_queue.put(("status", line))

    process.wait()
    if process.returncode != 0:
        raise RuntimeError("Download failed.")
    return completed_paths


def build_zip_archive(folder: Path, zip_name: str, files: list[Path], ui_queue: queue.Queue[tuple[str, Any]]) -> Path:
    zip_path = folder / f"{sanitize_name(zip_name)}.zip"
    ui_queue.put(("status", f"Creating zip archive: {zip_path.name}"))
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in files:
            archive.write(file_path, arcname=file_path.name)
    return zip_path


def extract_audio_from_video(video_path: Path, ui_queue: queue.Queue[tuple[str, Any]]) -> Path:
    if not FFMPEG:
        raise RuntimeError("ffmpeg is required to extract audio from a downloaded video.")
    audio_path = video_path.with_suffix(".mp3")
    ui_queue.put(("status", f"Extracting audio from video: {video_path.name}"))
    completed = subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "Audio extraction failed."
        raise RuntimeError(stderr)
    return audio_path


def download_video(
    url: str,
    output_dir: str,
    media_type: str,
    ui_queue: queue.Queue[tuple[str, Any]],
    preview: PreviewData,
    playlist_context: str | None = None,
) -> None:
    job_folder = build_job_folder(output_dir, preview, playlist_context=playlist_context)
    output_template = build_output_template(job_folder, preview, playlist_context=playlist_context)
    command = yt_dlp_base_command()
    command.extend(["--newline", "--print", "after_move:MOVE:%(filepath)s", "-o", output_template])

    if media_type == "audio":
        if FFMPEG:
            command.extend(["-x", "--audio-format", "mp3", "--audio-quality", "128K"])
        else:
            command.extend(["-f", "bestaudio/best"])
            ui_queue.put(("status", "ffmpeg is not installed, so this audio download will keep the source audio format instead of converting to MP3."))
    elif media_type == "both":
        command.extend(["-f", "bv*+ba/b"])
    else:
        if FFMPEG:
            command.extend(["-f", "bv*+ba/b"])
        else:
            command.extend(["-f", "b"])
            ui_queue.put(("status", "ffmpeg is not installed, so the app is using a simpler video format to avoid merge failures."))

    command.append(url)
    completed_paths = run_yt_dlp_capture_paths(command, ui_queue)
    if not completed_paths:
        raise RuntimeError(f"Download failed for {url}")

    if media_type == "both":
        video_path = completed_paths[-1]
        audio_path = extract_audio_from_video(video_path, ui_queue)
        zip_name = f"{preview.title} bundle"
        zip_path = build_zip_archive(job_folder, zip_name, [video_path, audio_path], ui_queue)
        ui_queue.put(("status", f"Saved combined archive: {zip_path}"))


def main() -> None:
    if tk is None:
        raise SystemExit(
            "Tkinter is not available in this Python installation. Install a Python build with Tk support or package this as a Mac app with its own runtime."
        )
    write_startup_log("App launch started.")
    try:
        root = tk.Tk()
        DownloaderApp(root)
        write_startup_log("UI initialized successfully.")
        root.mainloop()
    except Exception as exc:  # noqa: BLE001
        error_details = "".join(traceback.format_exception(exc))
        write_startup_log(error_details)
        try:
            if tk is not None:
                messagebox.showerror(APP_TITLE, f"The app hit an error during startup.\n\nLog file:\n{startup_log_path()}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
