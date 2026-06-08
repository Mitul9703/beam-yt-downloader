#!/usr/bin/env python3

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
import base64
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("YT_DOWNLOADER_PORT", "8765"))


def is_frozen() -> bool:
    """True when running inside a packaged (py2app/PyInstaller) bundle."""
    return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def resource_root() -> Path:
    """Where bundled read-only resources (e.g. bin/yt-dlp) live."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "Resources"
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Writable location for logs, settings, and temp downloads.

    A packaged .app bundle is read-only, so when frozen we keep user data in
    Application Support. In plain dev mode we keep it next to the script so the
    existing logs/ folder and saved settings keep working.
    """
    if is_frozen():
        target = Path.home() / "Library" / "Application Support" / "BeamYTDownloader"
    else:
        target = Path(__file__).resolve().parent
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_binary(name: str) -> str | None:
    """Find a helper binary inside the bundle first, then fall back to PATH."""
    root = resource_root()
    for candidate in (root / "bin" / name, root / name):
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


BASE_DIR = resource_root()
DATA_DIR = data_dir()
DOWNLOADS_DIR = DATA_DIR / "downloads"
LOGS_DIR = DATA_DIR / "logs"
SETTINGS_PATH = DATA_DIR / ".beam_downloader_settings.json"
YT_DLP = resolve_binary("yt-dlp")
NODE = resolve_binary("node")
FFMPEG = resolve_binary("ffmpeg")
FFPROBE = resolve_binary("ffprobe")
PLAYLIST_DELAY_SECONDS = 3

JOB_LOCK = threading.Lock()
JOB_QUEUE: list["DownloadJob"] = []
JOBS: dict[int, "DownloadJob"] = {}
NEXT_JOB_ID = 1
ACTIVE_PROCESSES: dict[int, subprocess.Popen[str]] = {}


class DownloadCancelled(RuntimeError):
    pass


MIN_FREE_MB = 500


def free_space_mb(folder: Path) -> int:
    try:
        return shutil.disk_usage(folder).free // (1024 * 1024)
    except Exception:  # noqa: BLE001
        return -1


def ensure_free_space(folder: Path) -> None:
    mb = free_space_mb(folder)
    if 0 <= mb < MIN_FREE_MB:
        raise RuntimeError(
            f"Only {mb} MB free in the save folder. Free up some space and try again."
        )


def default_output_dir() -> Path:
    return Path.home() / "Downloads"


def app_log_path() -> Path:
    return LOGS_DIR / "app.log"


def write_app_log(message: str) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with app_log_path().open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_settings(data: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_trint_settings() -> TrintSettings:
    raw = load_settings().get("trint", {})
    if not isinstance(raw, dict):
        return TrintSettings()
    return TrintSettings(
        key_id=str(raw.get("key_id", "")),
        key_secret=str(raw.get("key_secret", "")),
        workspace_id=str(raw.get("workspace_id", "")),
        workspace_name=str(raw.get("workspace_name", "")),
        folder_id=str(raw.get("folder_id", "")),
        folder_name=str(raw.get("folder_name", "")),
    )


def save_trint_settings(settings: TrintSettings) -> None:
    data = load_settings()
    data["trint"] = asdict(settings)
    save_settings(data)


def clear_trint_settings() -> None:
    data = load_settings()
    data.pop("trint", None)
    save_settings(data)


def save_trint_destination(workspace_id: str, workspace_name: str, folder_id: str, folder_name: str) -> TrintSettings:
    settings = get_trint_settings()
    settings.workspace_id = workspace_id
    settings.workspace_name = workspace_name
    settings.folder_id = folder_id
    settings.folder_name = folder_name
    save_trint_settings(settings)
    return settings


@dataclass
class PreviewData:
    input_url: str
    requested_mode: str
    detected_kind: str
    title: str
    channel: str
    item_count: int
    preview_url: str
    embed_url: str = ""
    thumbnail_url: str = ""
    warning: str = ""
    effective_url: str = ""


@dataclass
class TrintSettings:
    key_id: str = ""
    key_secret: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    folder_id: str = ""
    folder_name: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.key_secret)


@dataclass
class DownloadJob:
    id: int
    url: str
    requested_mode: str
    media_type: str
    output_dir: str
    preview: PreviewData
    upload_to_trint: bool = False
    trint_workspace_id: str = ""
    trint_workspace_name: str = ""
    trint_folder_id: str = ""
    trint_folder_name: str = ""
    trint_new_folder: bool = False
    trint_parent_id: str = ""
    status: str = "Queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    active: bool = False
    finished: bool = False
    failed: bool = False
    cancelled: bool = False
    cancel_requested: bool = False
    logs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    progress_percent: float = 0.0
    progress_label: str = ""
    transfer_label: str = ""
    current_item_label: str = ""
    queue_message: str = ""
    log_path: str = ""
    trint_uploaded_files: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.logs.append(entry)
        self.set_status(message)
        self.updated_at = time.time()
        if self.log_path:
            log_file = Path(self.log_path)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"{entry}\n")
        write_app_log(f"job-{self.id}: {message}")

    def set_status(self, message: str) -> None:
        self.status = message
        self.updated_at = time.time()


def render_page() -> bytes:
    page = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Beam YouTube Downloader</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&display=swap" rel="stylesheet">
    <style>
      :root {{
        --bg: #f4f3f0;
        --panel: #ffffff;
        --panel-strong: #ffffff;
        --ink: #161616;
        --muted: #585858;
        --accent: #ffc627;
        --accent-dark: #161616;
        --gold-soft: #fff7da;
        --line: #e2e0db;
        --line-soft: #ededea;
        --success: #eef6ee;
        --error: #fdeeea;
        --shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
      }}

      * {{
        box-sizing: border-box;
      }}

      body {{
        margin: 0;
        font-family: "DM Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: var(--ink);
        background: var(--bg);
        min-height: 100vh;
        -webkit-font-smoothing: antialiased;
      }}

      main {{
        width: 100%;
        max-width: 1320px;
        margin: 0 auto;
        padding: 0 clamp(16px, 4vw, 48px) 56px;
      }}

      .hero {{
        padding: 30px 0 18px;
        margin-bottom: 24px;
        border-bottom: 2px solid var(--ink);
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        gap: 16px;
      }}

      h1 {{
        margin: 0;
        font-family: "Oswald", "Arial Narrow", sans-serif;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.01em;
        font-size: clamp(1.6rem, 3vw, 2.3rem);
        line-height: 1;
        display: flex;
        align-items: center;
        gap: 12px;
      }}

      h1::before {{
        content: "";
        width: 14px;
        height: 26px;
        background: var(--accent);
        display: inline-block;
      }}

      h2, h3 {{
        font-family: "Oswald", "Arial Narrow", sans-serif;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        font-weight: 600;
      }}

      .layout {{
        display: grid;
        grid-template-columns: 1.2fr 0.8fr;
        gap: 20px;
        padding: 0;
      }}

      .stack {{
        display: grid;
        gap: 16px;
      }}

      .card {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 12px;
        padding: 22px 24px;
      }}

      .card h2 {{
        margin: 0 0 16px;
        font-size: 1.05rem;
      }}

      label {{
        display: block;
        font-weight: 600;
        font-size: 0.9rem;
        margin-bottom: 8px;
      }}

      textarea {{
        width: 100%;
        min-height: 92px;
        resize: vertical;
        padding: 12px 14px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        color: var(--ink);
        font: inherit;
      }}

      input[type="text"],
      input[type="password"],
      select {{
        width: 100%;
        padding: 11px 13px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        color: var(--ink);
        font: inherit;
      }}

      textarea:focus,
      input:focus,
      select:focus {{
        outline: none;
        border-color: var(--ink);
        box-shadow: 0 0 0 3px rgba(255, 198, 39, 0.4);
      }}

      .inline-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 14px;
      }}

      .radio-row {{
        display: flex;
        width: 100%;
        gap: 3px;
        flex-wrap: nowrap;
        background: #ebe9e3;
        padding: 4px;
        border-radius: 12px;
      }}

      .check-row {{
        display: flex;
        align-items: center;
        gap: 10px;
      }}

      .radio-pill {{
        flex: 1;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        text-align: center;
        white-space: nowrap;
        border: 0;
        border-radius: 9px;
        padding: 9px 10px;
        background: transparent;
        color: var(--ink);
        font-weight: 600;
        font-size: 0.92rem;
        cursor: pointer;
        user-select: none;
        transition: background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
      }}

      .radio-pill input {{
        position: absolute;
        opacity: 0;
        width: 0;
        height: 0;
        pointer-events: none;
      }}

      .radio-pill:hover {{
        background: rgba(0, 0, 0, 0.05);
      }}

      .radio-pill:has(input:checked) {{
        background: #fff;
        color: var(--ink);
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18);
      }}

      .actions {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
      }}

      button {{
        border: 1px solid var(--ink);
        border-radius: 6px;
        padding: 11px 18px;
        background: var(--ink);
        color: #fff;
        font: inherit;
        font-weight: 600;
        letter-spacing: 0.01em;
        cursor: pointer;
        transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
      }}

      button:hover {{
        background: #000;
        border-color: #000;
      }}

      button.secondary {{
        background: transparent;
        color: var(--ink);
        border: 1px solid var(--line);
      }}

      button.secondary:hover {{
        background: #f1efe9;
        border-color: #c9c6bf;
      }}

      button:disabled {{
        background: #ededed;
        color: #9a9a9a;
        border-color: #e2e2e2;
        cursor: not-allowed;
      }}

      button.is-busy {{
        background: #000;
        color: #fff;
        cursor: progress;
      }}

      button.secondary.is-busy {{
        background: #f1efe9;
        color: var(--ink);
      }}

      .btn-spinner {{
        display: inline-block;
        width: 14px;
        height: 14px;
        border: 2px solid currentColor;
        border-right-color: transparent;
        border-radius: 50%;
        vertical-align: -2px;
        margin-right: 8px;
        animation: spin 0.7s linear infinite;
        opacity: 0.85;
      }}

      @keyframes spin {{
        to {{ transform: rotate(360deg); }}
      }}

      .help {{
        color: var(--muted);
        font-size: 0.94rem;
        line-height: 1.55;
      }}

      .mini-note {{
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.45;
      }}

      .live-row {{
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 10px;
      }}

      .loader-dot {{
        width: 12px;
        height: 12px;
        border-radius: 999px;
        background: var(--accent);
        box-shadow: 0 0 0 rgba(184, 91, 43, 0.4);
        animation: pulse 1.2s infinite;
        opacity: 0;
      }}

      .loader-dot.show {{
        opacity: 1;
      }}

      .status-spinner {{
        width: 18px;
        height: 18px;
        border: 2px solid var(--line);
        border-top-color: var(--accent);
        border-radius: 50%;
        animation: spin 0.7s linear infinite;
        display: none;
        flex: 0 0 18px;
      }}

      .status-spinner.show {{
        display: inline-block;
      }}

      @keyframes pulse {{
        0% {{ transform: scale(0.9); box-shadow: 0 0 0 0 rgba(184, 91, 43, 0.35); }}
        70% {{ transform: scale(1); box-shadow: 0 0 0 12px rgba(184, 91, 43, 0); }}
        100% {{ transform: scale(0.9); box-shadow: 0 0 0 0 rgba(184, 91, 43, 0); }}
      }}

      .status-box,
      .queue-box,
      .downloads-box,
      .details-box {{
        border: 1px solid var(--line-soft);
        border-radius: 18px;
        padding: 16px;
        background: white;
      }}

      .details-box {{
        min-height: 170px;
      }}

      .meta-grid {{
        display: grid;
        grid-template-columns: 140px 1fr;
        gap: 10px 14px;
        font-size: 0.96rem;
      }}

      .meta-label {{
        color: var(--muted);
        font-weight: 700;
      }}

      .scroll-panel {{
        max-height: 310px;
        overflow: auto;
      }}

      .status-banner {{
        border-radius: 14px;
        padding: 12px 14px;
        margin-bottom: 16px;
        display: none;
      }}

      .status-banner.show {{
        display: block;
      }}

      .status-banner.info {{
        background: #f8efe4;
      }}

      .status-banner.error {{
        background: var(--error);
      }}

      .status-banner.success {{
        background: var(--success);
      }}

      .logs {{
        margin: 10px 0 0;
        padding-left: 18px;
        max-height: 280px;
        overflow: auto;
        color: var(--muted);
        line-height: 1.5;
      }}

      .job-list {{
        display: grid;
        gap: 10px;
      }}

      .job-item {{
        border: 1px solid var(--line-soft);
        border-radius: 14px;
        padding: 12px 14px;
        background: white;
      }}

      .job-item strong {{
        display: block;
      }}

      .inline-actions {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        align-items: center;
      }}

      .tutorial {{
        border: 1px solid var(--line-soft);
        border-radius: 16px;
        background: #f7f6f2;
        padding: 16px 18px;
      }}

      .tutorial-steps {{
        margin: 0;
        padding-left: 20px;
        display: grid;
        gap: 8px;
        color: var(--ink);
        font-size: 0.95rem;
        line-height: 1.45;
      }}

      .tutorial-warn {{
        margin-top: 14px;
        padding: 12px 14px;
        background: #fff4e0;
        border: 1px solid #f0d9a8;
        border-radius: 12px;
        font-size: 0.92rem;
        color: #7a5a12;
        line-height: 1.45;
      }}

      .icon-button {{
        width: 44px;
        height: 44px;
        border-radius: 8px;
        padding: 0;
        font-size: 1.2rem;
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }}

      .hidden {{
        display: none !important;
      }}

      .trint-inline {{
        border: 1px solid var(--line);
        border-radius: 16px;
        background: #ffffff;
        padding: 16px 18px;
        display: grid;
        gap: 12px;
      }}
      .trint-inline-head {{
        display: flex; align-items: center; gap: 10px;
        font-weight: 700; color: var(--ink);
      }}
      .trint-dest-row {{
        display: flex; align-items: center; justify-content: space-between;
        gap: 12px; flex-wrap: wrap;
      }}
      .trint-dest-value {{
        display: inline-flex; align-items: center; gap: 8px;
        background: #f1efe9; border: 1px solid var(--line);
        border-radius: 999px; padding: 8px 14px;
        font-size: 0.92rem; color: var(--ink); font-weight: 600;
        max-width: 100%; overflow: hidden; text-overflow: ellipsis;
      }}
      .trint-dest-value.is-new {{ background: #e7f6ef; border-color: #bfe6d2; }}

      .fx-modal {{
        width: min(980px, 100%);
        max-height: 90vh;
        display: flex;
        flex-direction: column;
        padding: 0;
        overflow: hidden;
      }}
      .fx-head {{
        display: flex; align-items: flex-start; justify-content: space-between;
        gap: 16px; padding: 22px 24px 16px;
        border-bottom: 1px solid var(--line-soft);
      }}
      .fx-space {{
        display: flex; align-items: center; gap: 8px;
        font-size: 0.9rem; color: var(--muted);
      }}
      .fx-space select {{ min-width: 170px; }}
      .fx-toolbar {{
        display: flex; align-items: center; justify-content: space-between;
        gap: 12px; flex-wrap: wrap;
        padding: 14px 24px; border-bottom: 1px solid var(--line-soft);
        background: #f7f6f2;
      }}
      .fx-breadcrumbs {{
        display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
        font-size: 0.95rem; color: var(--muted);
      }}
      .fx-crumb {{
        border: 0; background: transparent; color: var(--accent-dark);
        font: inherit; font-weight: 700; padding: 0; cursor: pointer;
      }}
      .fx-crumb.current {{ color: var(--ink); cursor: default; }}
      .fx-list {{
        flex: 1; overflow: auto; min-height: 320px; max-height: 52vh;
        background: #fff;
      }}
      .fx-row {{
        display: flex; align-items: center; gap: 12px;
        padding: 12px 24px; border-bottom: 1px solid #f0ece4;
        cursor: pointer;
      }}
      .fx-row:hover {{ background: #f7f6f2; }}
      .fx-row.selected {{ background: #fff7da; box-shadow: inset 3px 0 0 var(--accent-dark); }}
      .fx-row.here {{ background: #f4f3f0; }}
      .fx-row.file {{ cursor: default; color: var(--muted); }}
      .fx-row.file:hover {{ background: #fff; }}
      .fx-icon {{ font-size: 1.2rem; width: 24px; text-align: center; flex: 0 0 24px; }}
      .fx-name {{ flex: 1; font-weight: 600; color: var(--ink); }}
      .fx-row.file .fx-name {{ font-weight: 500; color: var(--muted); }}
      .fx-meta {{ font-size: 0.82rem; color: var(--muted); font-weight: 500; }}
      .fx-open {{
        border: 1px solid var(--line); background: #fff; color: var(--ink);
        border-radius: 8px; padding: 6px 12px; font-size: 0.85rem; font-weight: 600;
      }}
      .fx-open:hover {{ background: #f1efe9; }}
      .fx-badge {{
        background: #dff3e8; color: #1d7a4d; border-radius: 999px;
        padding: 2px 10px; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em;
      }}
      .fx-new-input {{
        flex: 1; border: 1px solid var(--accent-dark); border-radius: 8px;
        padding: 8px 10px; font: inherit;
      }}
      .fx-footer {{
        display: flex; align-items: center; justify-content: space-between;
        gap: 16px; flex-wrap: wrap;
        padding: 16px 24px; border-top: 1px solid var(--line-soft);
        background: #f7f6f2;
      }}
      .fx-footer-dest {{ font-size: 0.92rem; color: var(--ink); }}
      .fx-footer-dest b {{ color: var(--accent-dark); }}
      .fx-footer-actions {{ display: flex; gap: 10px; }}
      .fx-loading {{
        display: flex; align-items: center; gap: 12px;
        padding: 48px 24px; color: var(--muted);
      }}
      .fx-empty {{ padding: 28px 24px; color: var(--muted); }}
      .fx-onlyfolders {{
        padding: 10px 24px;
        background: var(--gold-soft);
        border-bottom: 1px solid var(--line-soft);
        color: #6b5a16;
        font-size: 0.85rem;
      }}

      .modal-backdrop {{
        position: fixed;
        inset: 0;
        background: rgba(24, 35, 41, 0.35);
        display: none;
        align-items: center;
        justify-content: center;
        padding: 20px;
        z-index: 20;
      }}

      .modal-backdrop.show {{
        display: flex;
      }}

      .modal-card {{
        width: min(720px, 100%);
        max-height: 85vh;
        overflow: auto;
        background: var(--panel-strong);
        border: 1px solid var(--line);
        border-radius: 14px;
        box-shadow: 0 20px 50px rgba(0,0,0,0.18);
        padding: 24px;
      }}

      .modal-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
        margin-bottom: 18px;
      }}

      .modal-close {{
        background: #ededea;
        color: var(--ink);
      }}

      .downloads-box a {{
        color: var(--accent-dark);
        text-decoration: none;
        font-weight: 700;
      }}

      .downloads-box a:hover {{
        text-decoration: underline;
      }}

      @media (max-width: 900px) {{
        .layout {{
          grid-template-columns: 1fr;
        }}
        .fx-footer {{ flex-direction: column; align-items: stretch; }}
        .fx-footer-actions {{ justify-content: flex-end; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <h1>Beam YouTube Downloader</h1>
        <button id="openSettingsBtn" class="secondary" type="button" aria-label="Open settings" style="display:inline-flex; align-items:center; gap:8px; font-size:1rem; padding:11px 18px">&#9881;&#65039; Settings</button>
      </section>

      <section class="layout">
        <section class="stack">
          <div class="card">
            <h2>Download</h2>
            <div id="banner" class="status-banner info"></div>
            <div class="stack">
              <div>
                <label for="urlInput">YouTube Link</label>
                <textarea id="urlInput" placeholder="Paste a YouTube video or playlist URL here"></textarea>
              </div>

              <div class="inline-grid">
                <div>
                  <label>Link Type</label>
                  <div class="radio-row">
                    <label class="radio-pill"><input type="radio" name="kind" value="single" checked> Single video</label>
                    <label class="radio-pill"><input type="radio" name="kind" value="playlist"> Playlist</label>
                  </div>
                </div>

                <div>
                  <label>Download Type</label>
                  <div class="radio-row">
                    <label class="radio-pill"><input type="radio" name="media" value="video" checked> Video</label>
                    <label class="radio-pill"><input type="radio" name="media" value="audio"> Audio</label>
                    <label class="radio-pill"><input type="radio" name="media" value="both"> Audio + Video</label>
                  </div>
                </div>
              </div>

              <div>
                <label for="outputDirInput">Save To Folder</label>
                <div class="inline-actions">
                  <input id="outputDirInput" type="text" style="flex:1" value="{default_output_dir()}">
                  <button id="chooseFolderBtn" class="secondary" type="button">Choose&hellip;</button>
                </div>
                <div class="mini-note">Choose a folder, or type a path.</div>
              </div>

              <div class="check-row">
                <input id="uploadToTrint" type="checkbox">
                <label for="uploadToTrint" style="margin:0">Upload to Trint after download</label>
              </div>

              <div id="trintUploadPanel" class="hidden" style="display:grid; gap:0">
                <div id="trintNeedsSettings" class="help hidden">
                  No Trint key saved yet. Add your key in Settings to choose an upload folder.
                  <div style="margin-top:10px">
                    <button id="openSettingsFromPanelBtn" class="secondary" type="button">Open Settings</button>
                  </div>
                </div>
                <div id="trintInlineCard" class="trint-inline hidden">
                  <div class="trint-inline-head">&#128228; Trint upload destination</div>
                  <div class="trint-dest-row">
                    <span id="trintDestValue" class="trint-dest-value">No folder chosen yet</span>
                    <button id="chooseTrintFolderBtn" class="secondary" type="button">Choose folder&hellip;</button>
                  </div>
                  <div class="mini-note">The video uploads here after it finishes downloading. New folders are created at that point.</div>
                </div>
              </div>

              <div class="actions">
                <button id="downloadBtn" type="button">Download</button>
                <button id="cancelBtn" class="secondary" type="button" disabled>Cancel download</button>
              </div>
            </div>
          </div>

          <div class="card">
            <h2>Link Details</h2>
            <div class="details-box">
              <div id="detailsEmpty" class="help">Paste a YouTube link and the title, channel, link type, and item count will appear here.</div>
              <div id="detailsError" class="help" style="display:none; color:#b3261e; font-weight:600"></div>
              <div id="detailsContent" style="display:none">
                <div class="meta-grid">
                  <div class="meta-label">Title</div><div id="detailsTitle">-</div>
                  <div class="meta-label">Channel</div><div id="detailsChannel">-</div>
                  <div class="meta-label">Detected type</div><div id="detailsType">-</div>
                  <div class="meta-label">Items</div><div id="detailsCount">-</div>
                </div>
                <p id="detailsWarning" class="help" style="margin:14px 0 0"></p>
              </div>
            </div>
          </div>
        </section>

        <section class="stack">
          <div class="card">
            <h2>Download Status</h2>
            <div class="status-box scroll-panel">
              <div class="live-row">
                <div id="activeLoader" class="status-spinner"></div>
                <p id="activeStatus" class="help" style="margin:0">No active job right now.</p>
              </div>
              <div id="progressLabel" class="mini-note">Waiting for a download.</div>
              <ol id="activeLogs" class="logs"></ol>
            </div>
          </div>

          <div class="card">
            <h2>Up Next</h2>
            <div id="queueList" class="job-list queue-box scroll-panel">
              <div class="help">Nothing else is waiting right now.</div>
            </div>
            <div class="live-row" style="margin-top:12px">
              <div id="queueLoader" class="loader-dot"></div>
              <div id="queueHint" class="mini-note">Nothing is queued right now.</div>
            </div>
          </div>

          <div class="card">
            <h2>Completed Downloads</h2>
            <div id="downloadsList" class="downloads-box scroll-panel">
              <div class="help">Finished files will appear here.</div>
            </div>
          </div>
        </section>
      </section>
    </main>

    <div id="settingsModal" class="modal-backdrop">
      <div class="modal-card">
        <div class="modal-header">
          <div>
            <h2 style="margin:0 0 6px">Settings</h2>
            <div class="mini-note">Add your personal Trint key so uploads go to your own Trint account.</div>
          </div>
          <button id="closeSettingsBtn" class="secondary modal-close" type="button">Close</button>
        </div>

        <div class="stack">
          <div class="inline-grid">
            <div>
              <label for="trintKeyId">Trint Key ID</label>
              <input id="trintKeyId" type="text" placeholder="AK-...">
            </div>
            <div>
              <label for="trintKeySecret">Trint Key Secret</label>
              <div class="inline-actions">
                <input id="trintKeySecret" type="password" placeholder="Paste your personal Trint key secret" style="flex:1">
                <button id="toggleSecretBtn" class="secondary" type="button" style="min-width:74px">Show</button>
              </div>
            </div>
          </div>

          <div class="actions">
            <button id="saveTrintBtn" type="button">Save key</button>
            <button id="clearTrintBtn" class="secondary" type="button">Remove key</button>
          </div>

          <div class="tutorial">
            <h3 style="margin:0 0 10px; font-size:1.05rem">How to get your Trint Key ID and Key Secret</h3>
            <ol class="tutorial-steps">
              <li>Sign in to Trint in your web browser at <b>app.trint.com</b>.</li>
              <li>Click your <b>profile icon</b> in the top-right corner, then open <b>Account Settings</b>.</li>
              <li>In the menu, choose <b>API Keys</b> (it may sit under a "Developer" or "Integrations" heading).</li>
              <li>Click <b>Create API Key</b> (or "Generate new key") and give it a name you'll recognise, like "My Downloader".</li>
              <li>Trint will show a <b>Key ID</b> and a <b>Key Secret</b>. Copy <b>both</b> and paste them into the two boxes above.</li>
              <li>Click <b>Save Trint Key</b> below. That's it &mdash; you only do this once.</li>
            </ol>
            <div class="tutorial-warn">
              &#9888;&#65039; <b>Important:</b> Trint shows your Key Secret <b>only once</b>, right after you create it. You cannot view or edit it in Trint again afterwards. Copy it and keep it somewhere safe. If you lose it, it can't be recovered &mdash; just delete the old key in Trint and generate a brand-new one.
            </div>
            <div class="mini-note" style="margin-top:10px">Your key is stored locally on this computer and is used only for your own uploads. It is never shared.</div>
          </div>

          <div class="tutorial">
            <h3 style="margin:0 0 8px; font-size:1rem">Something not working?</h3>
            <div class="mini-note" style="margin-bottom:12px">Open the logs folder and send the most recent file to whoever set this up — it records what happened so problems can be fixed.</div>
            <button id="openLogsBtn" class="secondary" type="button">Open logs folder</button>
          </div>
        </div>
      </div>
    </div>

    <div id="trintFolderModal" class="modal-backdrop">
      <div class="modal-card fx-modal">
        <div class="fx-head">
          <div>
            <h2 style="margin:0 0 6px">Choose Trint upload folder</h2>
            <div class="mini-note">Browse your Trint folders, or create a new one that gets made when the download finishes.</div>
          </div>
          <div style="display:flex; align-items:center; gap:14px">
            <div class="fx-space">
              <span>Space</span>
              <select id="trintSpaceSelect"><option value="">My Drive</option></select>
            </div>
            <button id="closeTrintModalBtn" class="secondary modal-close" type="button">Close</button>
          </div>
        </div>
        <div class="fx-toolbar">
          <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap; min-width:0">
            <button id="trintBackBtn" class="secondary" type="button">&#8592; Back</button>
            <div id="trintBreadcrumbs" class="fx-breadcrumbs"></div>
          </div>
          <div style="display:flex; gap:8px">
            <button id="trintRefreshBtn" class="secondary" type="button">&#8635; Refresh</button>
            <button id="newFolderBtn" class="secondary" type="button">&#43; New folder here</button>
          </div>
        </div>
        <div class="fx-onlyfolders">Only folders are shown — open a folder to go inside it, then choose where your file should be uploaded.</div>
        <div id="trintFxList" class="fx-list"></div>
        <div class="fx-footer">
          <div class="fx-footer-dest">Upload to: <b id="trintSelectedLabel">Home</b></div>
          <div class="fx-footer-actions">
            <button id="cancelTrintModalBtn" class="secondary" type="button">Cancel</button>
            <button id="useTrintFolderBtn" type="button">Use this folder</button>
          </div>
        </div>
      </div>
    </div>

    <script>
      let bannerTimer = null;
      let seenFinishedJobIds = new Set();
      let detailTimer = null;
      let lastDetailUrl = "";
      let detailsRequestToken = 0;
      let trintSettingsState = null;
      let trintWorkspaces = [];
      let trintExplorerState = null;
      let trintExplorerVisible = false;
      let trintExpandedFolders = new Set([""]);

      function currentKind() {{
        return document.querySelector('input[name="kind"]:checked').value;
      }}

      function currentMedia() {{
        return document.querySelector('input[name="media"]:checked').value;
      }}

      function escapeHtml(value) {{
        return String(value == null ? "" : value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      }}

      function showBanner(message, tone = "info") {{
        const banner = document.getElementById("banner");
        banner.textContent = message;
        banner.className = `status-banner show ${{tone}}`;
        if (bannerTimer) clearTimeout(bannerTimer);
        if (tone !== "error") {{
          bannerTimer = setTimeout(() => {{
            banner.textContent = "";
            banner.className = "status-banner info";
          }}, 4500);
        }}
      }}

      async function requestJson(url, payload) {{
        const response = await fetch(url, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(payload)
        }});
        const data = await response.json();
        if (!response.ok) {{
          throw new Error(data.error || "Request failed.");
        }}
        return data;
      }}

      // ---- Trint destination state ----
      let trintTree = {{ folders: [], files: [], workspace_id: "" }};
      let trintCwd = "";           // folder id currently shown (the destination); "" = Home
      let trintPending = null;     // {{parent_id, name}} draft folder being created
      let trintDestination = null; // committed destination
      let trintTreeLoaded = false;

      function workspaceNameFor(id) {{
        const found = trintWorkspaces.find((w) => (w.id || "") === (id || ""));
        return found ? found.name : "My Drive";
      }}
      function folderById(id) {{
        return (trintTree.folders || []).find((f) => (f.id || "") === (id || "")) || null;
      }}
      function folderDisplayName(id) {{
        if (!id) return "Home";
        return folderById(id)?.name || "Folder";
      }}
      function childFolders(parentId) {{
        return (trintTree.folders || []).filter((f) => (f.parent_id || "") === (parentId || ""));
      }}

      function destinationLabel(dest) {{
        if (!dest) return "No folder chosen yet";
        if (dest.mode === "new") {{
          const parent = dest.parent_name && dest.parent_name !== "Home" ? dest.parent_name + " / " : "";
          return `${{parent}}${{dest.name}} (new)`;
        }}
        return dest.folder_name || "Home";
      }}

      function fillSelect(selectId, items, selectedId) {{
        const select = document.getElementById(selectId);
        if (!select) return;
        select.innerHTML = "";
        (items && items.length ? items : [{{ id: "", name: "My Drive" }}]).forEach((item) => {{
          const option = document.createElement("option");
          option.value = item.id;
          option.textContent = item.name;
          if ((item.id || "") === (selectedId || "")) option.selected = true;
          select.appendChild(option);
        }});
      }}

      function renderTrintInline() {{
        const checked = document.getElementById("uploadToTrint").checked;
        const configured = !!trintSettingsState?.configured;
        document.getElementById("trintUploadPanel").classList.toggle("hidden", !checked);
        document.getElementById("trintNeedsSettings").classList.toggle("hidden", configured || !checked);
        document.getElementById("trintInlineCard").classList.toggle("hidden", !configured || !checked);
        const value = document.getElementById("trintDestValue");
        value.textContent = destinationLabel(trintDestination);
        value.classList.toggle("is-new", !!(trintDestination && trintDestination.mode === "new"));
      }}

      async function loadTrintSettings() {{
        const response = await fetch("/api/trint/settings");
        const data = await response.json();
        trintSettingsState = data.settings;
        document.getElementById("trintKeyId").value = data.settings.key_id || "";
        document.getElementById("trintKeySecret").value = data.settings.key_secret || "";
        trintWorkspaces = data.workspaces || [{{ id: "", name: "My Drive" }}];
        if (data.settings.configured && data.settings.folder_id) {{
          trintDestination = {{
            mode: "existing",
            workspace_id: data.settings.workspace_id || "",
            workspace_name: data.settings.workspace_name || "My Drive",
            folder_id: data.settings.folder_id || "",
            folder_name: data.settings.folder_name || "Home",
          }};
        }} else if (data.settings.configured) {{
          trintDestination = {{
            mode: "existing", workspace_id: "", workspace_name: "My Drive",
            folder_id: "", folder_name: "Home",
          }};
        }}
        renderTrintInline();
      }}

      function setModalLoading(message) {{
        const list = document.getElementById("trintFxList");
        list.innerHTML = `<div class="fx-loading"><div class="loader-dot show"></div><div>${{message}}</div></div>`;
      }}

      async function loadTrintTree(workspaceId = "") {{
        setModalLoading("Loading your Trint folders and files...");
        const data = await requestJson("/api/trint/browse", {{
          workspace_id: workspaceId,
          selected_folder_id: "",
        }});
        trintTree = {{
          folders: data.folders || [],
          files: data.files || [],
          workspace_id: data.workspace_id || "",
        }};
        trintWorkspaces = data.workspaces || [{{ id: "", name: "My Drive" }}];
        trintTreeLoaded = true;
      }}

      function renderBreadcrumbs() {{
        const holder = document.getElementById("trintBreadcrumbs");
        holder.innerHTML = "";
        const chain = [{{ id: "", name: "Home" }}];
        const stack = [];
        let walker = trintCwd;
        while (walker) {{
          const f = folderById(walker);
          if (!f) break;
          stack.push({{ id: f.id, name: f.name }});
          walker = f.parent_id || "";
        }}
        stack.reverse().forEach((c) => chain.push(c));
        if (trintPending) chain.push({{ id: "__pending__", name: (trintPending.name || "New folder"), pending: true }});
        chain.forEach((crumb, index) => {{
          const last = index === chain.length - 1;
          const button = document.createElement("button");
          button.type = "button";
          button.className = "fx-crumb" + (last ? " current" : "");
          button.textContent = crumb.name + (crumb.pending ? " (new)" : "");
          if (!last) {{
            button.addEventListener("click", () => {{ trintCwd = crumb.id; trintPending = null; renderTrintModal(); }});
          }}
          holder.appendChild(button);
          if (!last) {{
            const sep = document.createElement("span");
            sep.textContent = "/";
            holder.appendChild(sep);
          }}
        }});
      }}

      function renderTrintModal() {{
        fillSelect("trintSpaceSelect", trintWorkspaces, trintTree.workspace_id);
        renderBreadcrumbs();
        const pendingActive = !!trintPending;
        document.getElementById("trintBackBtn").disabled = !trintCwd && !pendingActive;
        document.getElementById("newFolderBtn").disabled = pendingActive;
        const list = document.getElementById("trintFxList");
        list.innerHTML = "";

        if (pendingActive) {{
          // Name the new folder — it gets created in Trint when the download finishes.
          const box = document.createElement("div");
          box.style.padding = "20px 24px";
          const lbl = document.createElement("label");
          lbl.textContent = "Name your new folder";
          box.appendChild(lbl);
          const input = document.createElement("input");
          input.id = "trintNewFolderInput";
          input.type = "text";
          input.placeholder = "e.g. Board meetings";
          input.value = trintPending.name || "";
          input.style.maxWidth = "380px";
          input.addEventListener("input", () => {{ trintPending.name = input.value; updateDestPreview(); }});
          box.appendChild(input);
          const hint = document.createElement("div");
          hint.className = "mini-note";
          hint.style.marginTop = "10px";
          hint.textContent = `It will be created inside "${{folderDisplayName(trintCwd)}}" when your download finishes.`;
          box.appendChild(hint);
          list.appendChild(box);
          setTimeout(() => input.focus(), 0);
        }} else {{
          const subs = childFolders(trintCwd);
          subs.forEach((folder) => {{
            const row = document.createElement("div");
            row.className = "fx-row";
            row.innerHTML = `<div class="fx-icon">&#128193;</div><div class="fx-name">${{escapeHtml(folder.name)}}</div><div class="fx-meta">Open &rsaquo;</div>`;
            row.addEventListener("click", () => {{ trintCwd = folder.id; renderTrintModal(); }});
            list.appendChild(row);
          }});
          if (!subs.length) {{
            const empty = document.createElement("div");
            empty.className = "fx-empty";
            empty.textContent = "No folders inside here. Upload into this folder, or create a new one.";
            list.appendChild(empty);
          }}
        }}

        updateDestPreview();
      }}

      function updateDestPreview() {{
        const label = document.getElementById("trintSelectedLabel");
        const useBtn = document.getElementById("useTrintFolderBtn");
        if (trintPending) {{
          const name = (trintPending.name || "").trim();
          const here = folderDisplayName(trintCwd);
          label.textContent = (here === "Home" ? "" : here + " / ") + (name || "(name your folder)") + " (new)";
          useBtn.disabled = !name;
          useBtn.textContent = "Create & use";
        }} else {{
          label.textContent = folderDisplayName(trintCwd);
          useBtn.disabled = false;
          useBtn.textContent = "Use this folder";
        }}
      }}

      async function openTrintModal() {{
        if (!trintSettingsState?.configured) {{
          document.getElementById("settingsModal").classList.add("show");
          return;
        }}
        document.getElementById("trintFolderModal").classList.add("show");
        trintCwd = "";
        trintPending = null;
        if (!trintTreeLoaded) {{
          setModalLoading("Loading your Trint folders...");
          try {{
            await loadTrintTree(trintDestination?.workspace_id || "");
          }} catch (error) {{
            document.getElementById("trintFxList").innerHTML = `<div class="fx-empty">Could not load your Trint folders. ${{error.message}}</div>`;
            document.getElementById("trintSelectedLabel").textContent = "nothing yet";
            return;
          }}
        }}
        // Re-open inside the previously chosen folder so it's the current location.
        if (trintDestination && trintDestination.mode === "existing" && trintDestination.folder_id && folderById(trintDestination.folder_id)) {{
          trintCwd = trintDestination.folder_id;
        }}
        renderTrintModal();
      }}

      function closeTrintModal() {{
        document.getElementById("trintFolderModal").classList.remove("show");
      }}

      function notifyUser(title, body) {{
        if (!("Notification" in window) || Notification.permission !== "granted") {{
          return;
        }}
        new Notification(title, {{ body }});
      }}

      function renderDetails(preview) {{
        document.getElementById("detailsEmpty").style.display = "none";
        document.getElementById("detailsError").style.display = "none";
        document.getElementById("detailsContent").style.display = "block";
        document.getElementById("detailsTitle").textContent = preview.title || "-";
        document.getElementById("detailsChannel").textContent = preview.channel || "-";
        document.getElementById("detailsType").textContent = preview.detected_kind === "playlist" ? "Playlist" : "Single video";
        document.getElementById("detailsCount").textContent = preview.item_count || "-";
        document.getElementById("detailsWarning").textContent = preview.warning || "";
        // Auto-set the Link Type toggle to match what was detected.
        const detected = document.querySelector(`input[name="kind"][value="${{preview.detected_kind}}"]`);
        if (detected) detected.checked = true;
      }}

      function clearDetails() {{
        document.getElementById("detailsEmpty").style.display = "block";
        document.getElementById("detailsError").style.display = "none";
        document.getElementById("detailsContent").style.display = "none";
        document.getElementById("detailsWarning").textContent = "";
      }}

      function looksLikeYouTube(url) {{
        try {{
          const host = new URL(url).hostname.toLowerCase();
          return host.endsWith("youtube.com") || host.endsWith("youtu.be") || host.endsWith("youtube-nocookie.com");
        }} catch (e) {{
          return false;
        }}
      }}

      function renderDetailsError(message) {{
        document.getElementById("detailsEmpty").style.display = "none";
        document.getElementById("detailsContent").style.display = "none";
        const err = document.getElementById("detailsError");
        err.style.display = "block";
        err.textContent = message;
      }}

      async function loadDetailsForUrl() {{
        const url = document.getElementById("urlInput").value.trim();
        if (!url) {{
          lastDetailUrl = "";
          clearDetails();
          return;
        }}

        if (url === lastDetailUrl) {{
          return;
        }}

        const requestToken = ++detailsRequestToken;
        try {{
          const data = await requestJson("/api/preview", {{
            url,
            requested_mode: currentKind(),
          }});
          if (requestToken !== detailsRequestToken) {{
            return;
          }}
          lastDetailUrl = url;
          renderDetails(data.preview);
        }} catch (error) {{
          if (requestToken !== detailsRequestToken) {{
            return;
          }}
          lastDetailUrl = "";
          renderDetailsError(error.message || "Couldn't read this link.");
        }}
      }}

      function scheduleDetailsRefresh() {{
        if (detailTimer) clearTimeout(detailTimer);
        detailTimer = setTimeout(() => {{
          loadDetailsForUrl();
        }}, 450);
      }}

      function renderJobs(state) {{
        const activeStatus = document.getElementById("activeStatus");
        const activeLogs = document.getElementById("activeLogs");
        const queueList = document.getElementById("queueList");
        const downloadsList = document.getElementById("downloadsList");
        const progressLabel = document.getElementById("progressLabel");
        const activeLoader = document.getElementById("activeLoader");
        const queueLoader = document.getElementById("queueLoader");
        const queueHint = document.getElementById("queueHint");
        const cancelBtn = document.getElementById("cancelBtn");

        activeLogs.innerHTML = "";
        const active = state.active_job;
        if (active) {{
          activeLoader.classList.add("show");
          cancelBtn.disabled = false;
          cancelBtn.classList.remove("secondary");
          activeStatus.textContent = active.status;
          const itemLabel = active.current_item_label ? `Current download: ${{active.current_item_label}}` : "";
          const transferText = active.transfer_label ? `File progress: ${{active.transfer_label}}` : "";
          const stageText = active.progress_label && active.progress_label !== active.transfer_label ? active.progress_label : "";
          const workingHint = active.transfer_label ? "" : "working… large files can take a minute";
          progressLabel.textContent = [itemLabel, transferText, stageText, workingHint].filter(Boolean).join(" • ") || active.status;
          for (const log of active.logs.slice(-12)) {{
            const li = document.createElement("li");
            li.textContent = log;
            activeLogs.appendChild(li);
          }}
        }} else {{
          activeLoader.classList.remove("show");
          cancelBtn.disabled = true;
          cancelBtn.classList.add("secondary");
          activeStatus.textContent = "No active job right now.";
          progressLabel.textContent = "Waiting for a download.";
        }}

        queueList.innerHTML = "";
        if (!state.queued_jobs.length) {{
          queueLoader.classList.remove("show");
          queueHint.textContent = "Nothing is queued right now.";
          queueList.innerHTML = '<div class="help">Nothing else is waiting right now.</div>';
        }} else {{
          queueLoader.classList.add("show");
          queueHint.textContent = `${{state.queued_jobs.length}} download(s) waiting.`;
          for (const job of state.queued_jobs) {{
            const item = document.createElement("div");
            item.className = "job-item";
            item.innerHTML = `<strong>#${{job.id}} ${{escapeHtml(job.preview.title)}}</strong><div class="help">${{escapeHtml(job.media_type)}} • waiting to start</div>`;
            queueList.appendChild(item);
          }}
        }}

        downloadsList.innerHTML = "";
        if (!state.completed_jobs.length) {{
          downloadsList.innerHTML = '<div class="help">Finished files will appear here.</div>';
        }} else {{
          for (const job of state.completed_jobs) {{
            if (!seenFinishedJobIds.has(job.id)) {{
              seenFinishedJobIds.add(job.id);
              if (job.cancelled) {{
                showBanner(`Download cancelled. Anything already finished is still in ${{job.output_dir}}.`, "info");
                notifyUser("Download cancelled", job.preview.title);
              }} else if (job.failed) {{
                showBanner(`Download failed for ${{job.preview.title}}.`, "error");
                notifyUser("Download failed", job.preview.title);
              }} else {{
                showBanner(`Downloaded: ${{job.preview.title}}`, "success");
                notifyUser("Download finished", job.preview.title);
              }}
            }}
            const wrapper = document.createElement("div");
            wrapper.style.marginBottom = "14px";
            const heading = document.createElement("div");
            heading.innerHTML = `<strong>#${{job.id}} ${{escapeHtml(job.preview.title)}}</strong>`;
            wrapper.appendChild(heading);
            if (job.trint_uploaded_files && job.trint_uploaded_files.length) {{
              const note = document.createElement("div");
              note.className = "mini-note";
              note.style.marginTop = "6px";
              note.textContent = `Uploaded to Trint: ${{job.trint_uploaded_files.length}} file(s)`;
              wrapper.appendChild(note);
            }}
            job.outputs.forEach((output, index) => {{
              const link = document.createElement("a");
              link.href = job.download_links[index];
              link.textContent = output.split("/").pop();
              link.style.display = "block";
              link.style.marginTop = "6px";
              wrapper.appendChild(link);
            }});
            downloadsList.appendChild(wrapper);
          }}
        }}
      }}

      async function refreshState() {{
        const response = await fetch("/api/state");
        const state = await response.json();
        if (!seenFinishedJobIds.size) {{
          state.completed_jobs.forEach(job => seenFinishedJobIds.add(job.id));
        }}
        renderJobs(state);
      }}

      async function runBusy(button, busyLabel, fn) {{
        // Disables the button and shows a spinner for the duration of fn so a
        // slow action can't be triggered twice by repeated clicks.
        if (!button || button.dataset.busy === "1") return;
        button.dataset.busy = "1";
        const original = button.innerHTML;
        button.disabled = true;
        button.classList.add("is-busy");
        button.innerHTML = `<span class="btn-spinner"></span>${{busyLabel}}`;
        try {{
          return await fn();
        }} finally {{
          button.dataset.busy = "0";
          button.disabled = false;
          button.classList.remove("is-busy");
          button.innerHTML = original;
        }}
      }}

      document.getElementById("downloadBtn").addEventListener("click", () => {{
        const button = document.getElementById("downloadBtn");
        const url = document.getElementById("urlInput").value.trim();
        if (!url) {{
          showBanner("Paste a YouTube link first.", "error");
          return;
        }}
        if (!looksLikeYouTube(url)) {{
          showBanner("Please paste a valid YouTube link (youtube.com or youtu.be).", "error");
          renderDetailsError("That doesn't look like a YouTube link.");
          return;
        }}

        runBusy(button, "Starting...", async () => {{
          if ("Notification" in window && Notification.permission === "default") {{
            Notification.requestPermission().catch(() => null);
          }}
          showBanner("Starting your download...");
          const data = await requestJson("/api/queue", {{
            url,
            requested_mode: currentKind(),
            media_type: currentMedia(),
            output_dir: document.getElementById("outputDirInput").value.trim(),
            upload_to_trint: document.getElementById("uploadToTrint").checked,
            trint_workspace_id: trintDestination?.workspace_id || "",
            trint_workspace_name: trintDestination?.workspace_name || "My Drive",
            trint_folder_id: trintDestination?.mode === "existing" ? (trintDestination.folder_id || "") : "",
            trint_folder_name: trintDestination ? (trintDestination.mode === "new" ? trintDestination.name : (trintDestination.folder_name || "")) : "",
            trint_new_folder: trintDestination?.mode === "new",
            trint_parent_id: trintDestination?.mode === "new" ? (trintDestination.parent_id || "") : "",
          }});
          renderDetails(data.preview);
          lastDetailUrl = url;
          showBanner(data.job.queue_message || "Download started.", "success");
          await refreshState();
        }}).catch((error) => showBanner(error.message, "error"));
      }});

      document.getElementById("cancelBtn").addEventListener("click", async () => {{
        try {{
          const data = await requestJson("/api/cancel", {{}});
          showBanner(data.message || "Cancelling the current download...", "info");
          await refreshState();
        }} catch (error) {{
          showBanner(error.message, "error");
        }}
      }});

      document.getElementById("chooseFolderBtn").addEventListener("click", () => {{
        const button = document.getElementById("chooseFolderBtn");
        runBusy(button, "Opening...", async () => {{
          const data = await requestJson("/api/choose-folder", {{
            current: document.getElementById("outputDirInput").value.trim(),
          }});
          if (data.path) {{
            document.getElementById("outputDirInput").value = data.path;
          }}
        }}).catch((error) => showBanner(error.message, "error"));
      }});

      document.getElementById("urlInput").addEventListener("input", () => {{
        lastDetailUrl = "";
        scheduleDetailsRefresh();
      }});

      document.getElementById("openSettingsBtn").addEventListener("click", () => {{
        document.getElementById("settingsModal").classList.add("show");
      }});

      document.getElementById("openSettingsFromPanelBtn").addEventListener("click", () => {{
        document.getElementById("settingsModal").classList.add("show");
      }});

      document.getElementById("closeSettingsBtn").addEventListener("click", () => {{
        document.getElementById("settingsModal").classList.remove("show");
      }});

      document.getElementById("openLogsBtn").addEventListener("click", () => {{
        const button = document.getElementById("openLogsBtn");
        runBusy(button, "Opening...", async () => {{
          await requestJson("/api/open-logs", {{}});
          showBanner("Opened the logs folder.", "success");
        }}).catch((error) => showBanner(error.message, "error"));
      }});

      document.getElementById("toggleSecretBtn").addEventListener("click", () => {{
        const field = document.getElementById("trintKeySecret");
        const button = document.getElementById("toggleSecretBtn");
        if (field.type === "password") {{
          field.type = "text";
          button.textContent = "Hide";
        }} else {{
          field.type = "password";
          button.textContent = "Show";
        }}
      }});

      document.getElementById("settingsModal").addEventListener("click", (event) => {{
        if (event.target.id === "settingsModal") {{
          document.getElementById("settingsModal").classList.remove("show");
        }}
      }});

      document.getElementById("chooseTrintFolderBtn").addEventListener("click", () => {{
        openTrintModal();
      }});

      document.getElementById("closeTrintModalBtn").addEventListener("click", closeTrintModal);
      document.getElementById("cancelTrintModalBtn").addEventListener("click", closeTrintModal);
      document.getElementById("trintFolderModal").addEventListener("click", (event) => {{
        if (event.target.id === "trintFolderModal") closeTrintModal();
      }});

      document.getElementById("trintBackBtn").addEventListener("click", () => {{
        if (trintPending) {{
          trintPending = null;  // cancel the new folder, stay where we are
        }} else {{
          const f = folderById(trintCwd);
          trintCwd = f ? (f.parent_id || "") : "";
        }}
        renderTrintModal();
      }});

      document.getElementById("trintRefreshBtn").addEventListener("click", () => {{
        const button = document.getElementById("trintRefreshBtn");
        const keepCwd = trintCwd;
        runBusy(button, "Refreshing...", async () => {{
          await loadTrintTree(trintTree.workspace_id || "");
          trintCwd = folderById(keepCwd) ? keepCwd : "";
          trintPending = null;
          renderTrintModal();
        }}).catch((error) => showBanner(error.message, "error"));
      }});

      document.getElementById("newFolderBtn").addEventListener("click", () => {{
        trintPending = {{ parent_id: trintCwd, name: "" }};
        renderTrintModal();
      }});

      document.getElementById("trintSpaceSelect").addEventListener("change", async (event) => {{
        trintCwd = "";
        trintPending = null;
        try {{
          await loadTrintTree(event.target.value);
          renderTrintModal();
        }} catch (error) {{
          showBanner(error.message, "error");
        }}
      }});

      document.getElementById("useTrintFolderBtn").addEventListener("click", () => {{
        const spaceId = trintTree.workspace_id || "";
        const spaceName = workspaceNameFor(spaceId);
        if (trintPending) {{
          const name = (trintPending.name || "").trim();
          if (!name) {{
            showBanner("Type a name for the new folder first.", "error");
            return;
          }}
          trintDestination = {{
            mode: "new",
            workspace_id: spaceId,
            workspace_name: spaceName,
            parent_id: trintCwd,
            parent_name: folderDisplayName(trintCwd),
            name,
          }};
        }} else {{
          const folderId = trintCwd;
          const folderName = folderDisplayName(trintCwd);
          trintDestination = {{
            mode: "existing",
            workspace_id: spaceId,
            workspace_name: spaceName,
            folder_id: folderId,
            folder_name: folderName,
          }};
          requestJson("/api/trint/destination/save", {{
            workspace_id: spaceId,
            workspace_name: spaceName,
            folder_id: folderId,
            folder_name: folderName,
          }}).catch(() => null);
        }}
        renderTrintInline();
        closeTrintModal();
        showBanner(`Upload destination set: ${{destinationLabel(trintDestination)}}`, "success");
      }});

      document.getElementById("uploadToTrint").addEventListener("change", async (event) => {{
        renderTrintInline();
        if (event.target.checked && trintSettingsState?.configured && !trintTreeLoaded) {{
          try {{
            await loadTrintTree(trintDestination?.workspace_id || "");
          }} catch (error) {{
            // Surfaced when the picker is opened.
          }}
        }}
      }});

      document.getElementById("saveTrintBtn").addEventListener("click", () => {{
        const button = document.getElementById("saveTrintBtn");
        const keyId = document.getElementById("trintKeyId").value.trim();
        const keySecret = document.getElementById("trintKeySecret").value.trim();
        if (!keyId || !keySecret) {{
          showBanner("Paste both your Trint key ID and key secret.", "error");
          return;
        }}
        runBusy(button, "Saving...", async () => {{
          const data = await requestJson("/api/trint/settings/save", {{
            key_id: keyId,
            key_secret: keySecret,
          }});
          document.getElementById("trintKeySecret").value = data.settings.key_secret || keySecret;
          trintSettingsState = data.settings;
          trintTreeLoaded = false;
          showBanner("Trint key saved.", "success");
          document.getElementById("settingsModal").classList.remove("show");
          renderTrintInline();
        }}).catch((error) => showBanner(error.message, "error"));
      }});

      document.getElementById("clearTrintBtn").addEventListener("click", () => {{
        const button = document.getElementById("clearTrintBtn");
        runBusy(button, "Removing...", async () => {{
          const data = await requestJson("/api/trint/settings/clear", {{}});
          document.getElementById("trintKeyId").value = "";
          document.getElementById("trintKeySecret").value = "";
          trintSettingsState = data.settings;
          trintTree = {{ folders: [], files: [], workspace_id: "" }};
          trintTreeLoaded = false;
          trintDestination = null;
          renderTrintInline();
          showBanner("Trint key removed.", "info");
        }}).catch((error) => showBanner(error.message, "error"));
      }});

      document.querySelectorAll('input[name="kind"]').forEach((input) => {{
        input.addEventListener("change", () => {{
          lastDetailUrl = "";
          scheduleDetailsRefresh();
        }});
      }});

      renderTrintInline();
      loadTrintSettings().catch(() => null);
      refreshState();
      setInterval(refreshState, 2000);
    </script>
  </body>
</html>
"""
    return page.encode("utf-8")


def json_response(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


def trint_auth_headers(settings: TrintSettings) -> dict[str, str]:
    if not settings.configured:
        raise RuntimeError("Save your Trint key first.")
    token = base64.b64encode(f"{settings.key_id}:{settings.key_secret}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    }


def trint_request(
    method: str,
    url: str,
    settings: TrintSettings,
    *,
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    body_path: Path | None = None,
    content_type: str | None = None,
    timeout: int = 120,
) -> Any:
    if query:
        filtered = {key: value for key, value in query.items() if value not in (None, "")}
        if filtered:
            url = f"{url}?{urlencode(filtered)}"

    headers = trint_auth_headers(settings)
    data = raw_body
    body_file = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif body_path is not None:
        # Stream the file straight from disk instead of loading it all into RAM.
        headers["Content-Length"] = str(body_path.stat().st_size)
        if content_type:
            headers["Content-Type"] = content_type
        body_file = body_path.open("rb")
        data = body_file
    elif content_type:
        headers["Content-Type"] = content_type

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            response_type = response.headers.get("Content-Type", "")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Trint request failed with status {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Trint. {exc.reason}") from exc
    finally:
        if body_file is not None:
            body_file.close()

    if "application/json" in response_type and payload:
        return json.loads(payload.decode("utf-8"))
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return payload.decode("utf-8", errors="replace")


def list_trint_workspaces(settings: TrintSettings) -> list[dict[str, str]]:
    data = trint_request(
        "GET",
        "https://api.trint.com/workspaces/",
        settings,
        query={"include-archived": "false"},
    )
    results = [{"id": "", "name": "My Drive"}]
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "id": str(item.get("_id", "")),
                    "name": str(item.get("name", "Untitled Shared Drive")),
                }
            )
    return results


def list_trint_folders(settings: TrintSettings, workspace_id: str) -> list[dict[str, str]]:
    data = trint_request(
        "GET",
        "https://api.trint.com/folders/",
        settings,
        query={"workspace-id": workspace_id} if workspace_id else None,
    )
    # Trint returns a flat list. On personal drives nesting is encoded in the
    # NAME as a path ("Test1/NestedTest1") with parent/parentId null; on shared
    # drives an explicit `parent`/`parentId` may be present. Support both.
    raw: list[dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            raw.append(
                {
                    "id": str(item.get("_id", "")),
                    "full": str(item.get("name", "Untitled Folder")).strip("/"),
                    "explicit_parent": str(item.get("parent") or item.get("parentId") or ""),
                }
            )

    path_to_id = {entry["full"]: entry["id"] for entry in raw if entry["full"] and entry["id"]}
    results = [{"id": "", "name": "Top level", "parent_id": "", "workspace_id": workspace_id}]
    for entry in raw:
        if not entry["id"]:
            continue
        segments = entry["full"].split("/") if entry["full"] else []
        display_name = segments[-1] if segments else "Untitled Folder"
        parent_id = entry["explicit_parent"]
        if not parent_id and len(segments) > 1:
            parent_id = path_to_id.get("/".join(segments[:-1]), "")
        results.append(
            {
                "id": entry["id"],
                "name": display_name or "Untitled Folder",
                "parent_id": parent_id,
                "workspace_id": workspace_id,
            }
        )
    return results


def list_trint_files(settings: TrintSettings, workspace_id: str, folder_id: str = "") -> list[dict[str, str]]:
    page = 100
    skip = 0
    results: list[dict[str, str]] = []
    while True:
        query: dict[str, Any] = {"limit": page, "skip": skip}
        if workspace_id:
            query["sharedDriveId"] = workspace_id
            if folder_id:
                query["folderId"] = folder_id
        data = trint_request("GET", "https://api.trint.com/transcripts/", settings, query=query)
        if not isinstance(data, list) or not data:
            break
        for item in data:
            if not isinstance(item, dict):
                continue
            language = str(item.get("language", "")).strip()
            status = str(item.get("processingStatus", item.get("status", ""))).strip()
            meta_parts = [part for part in [language, status] if part]
            folder_ref = (
                item.get("folderId")
                or item.get("parentFolderId")
                or (item.get("folder") or {}).get("_id")
                or ""
            )
            results.append(
                {
                    "id": str(item.get("id") or item.get("_id", "")),
                    "name": str(item.get("title", "Untitled File")),
                    "folder_id": str(folder_ref or ""),
                    "meta": " • ".join(meta_parts) if meta_parts else "Trint file",
                }
            )
        if len(data) < page:
            break
        skip += page
        if skip >= 5000:  # safety cap so a huge account can't loop forever
            break
    return results


def build_trint_browse_payload(settings: TrintSettings, workspace_id: str, folder_id: str) -> dict[str, Any]:
    workspaces = list_trint_workspaces(settings)
    folders = list_trint_folders(settings, workspace_id)
    folder_map = {folder["id"]: folder for folder in folders if folder.get("id")}
    for folder in folders:
        folder_id_value = folder.get("id", "")
        if not folder_id_value:
            continue
        child_count = sum(1 for item in folders if item.get("parent_id", "") == folder_id_value)
        folder["child_count_text"] = f"{child_count} subfolder(s)" if child_count else "Folder"

    current_folder_name = "Top level"
    if folder_id and folder_id in folder_map:
        current_folder_name = folder_map[folder_id].get("name", "Folder")

    return {
        "workspace_id": workspace_id,
        "selected_folder_id": folder_id,
        "selected_folder_name": current_folder_name,
        "workspaces": workspaces,
        "folders": [folder for folder in folders if folder.get("id")],
        # The picker only chooses a destination folder; individual files aren't shown.
        "files": [],
        "is_personal": not workspace_id,
        "loading_label": "Folders loaded.",
    }


def selected_workspace_name(workspaces: list[dict[str, str]], workspace_id: str) -> str:
    for workspace in workspaces:
        if workspace.get("id", "") == workspace_id:
            return workspace.get("name", "My Drive")
    return "My Drive"


def create_trint_folder(settings: TrintSettings, name: str, workspace_id: str, parent_id: str) -> dict[str, str]:
    payload: dict[str, Any] = {"name": name}
    if workspace_id:
        payload["workspaceId"] = workspace_id
    if parent_id:
        payload["parentId"] = parent_id
    data = trint_request("POST", "https://api.trint.com/folders/", settings, json_body=payload)
    if not isinstance(data, dict):
        raise RuntimeError("Trint did not return the new folder details.")
    return {
        "id": str(data.get("_id", "")),
        "name": str(data.get("name", name)),
    }


def yt_dlp_base_command() -> list[str]:
    if not YT_DLP:
        raise RuntimeError("yt-dlp is not installed on this machine.")
    command = [YT_DLP]
    if NODE:
        command.extend(["--js-runtimes", "node"])
        command.extend(["--remote-components", "ejs:github"])
    return command


def sanitize_name(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in (" ", "-", "_", ".", "(", ")")).strip()
    return safe[:120] or "download"


def build_video_url_from_entry(entry: dict[str, Any]) -> str:
    value = entry.get("url")
    if isinstance(value, str) and value.startswith("http"):
        return value
    if entry.get("id"):
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return ""


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/")
    if "youtube.com" in (parsed.hostname or ""):
        if parsed.path == "/watch":
            params = parse_qs(parsed.query)
            return params.get("v", [""])[0]
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/shorts/", 1)[1].split("/", 1)[0]
        if parsed.path.startswith("/embed/"):
            return parsed.path.split("/embed/", 1)[1].split("/", 1)[0]
    return ""


def build_embed_url(video_url: str) -> str:
    video_id = extract_video_id(video_url)
    return f"https://www.youtube-nocookie.com/embed/{video_id}?rel=0" if video_id else ""


def pick_thumbnail(data: dict[str, Any], entry: dict[str, Any] | None = None) -> str:
    if isinstance(data.get("thumbnail"), str) and data["thumbnail"]:
        return data["thumbnail"]
    thumbnails = data.get("thumbnails") or []
    if thumbnails:
        last = thumbnails[-1]
        if isinstance(last, dict) and isinstance(last.get("url"), str):
            return last["url"]
    if entry:
        if isinstance(entry.get("thumbnail"), str) and entry["thumbnail"]:
            return entry["thumbnail"]
        entry_thumbs = entry.get("thumbnails") or []
        if entry_thumbs:
            last = entry_thumbs[-1]
            if isinstance(last, dict) and isinstance(last.get("url"), str):
                return last["url"]
    return ""


def is_probable_youtube_url(url: str) -> bool:
    u = url.strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    return any(host in u for host in ("youtube.com/", "youtu.be/", "youtube-nocookie.com/"))


def friendly_ytdlp_error(raw: str) -> str:
    """Turn a raw yt-dlp error into a message a non-technical user can act on."""
    text = (raw or "").strip()
    low = text.lower()
    if "unsupported url" in low or "is not a valid url" in low:
        return "That doesn't look like a valid YouTube link. Please double-check the URL."
    if "video unavailable" in low or "this video is unavailable" in low or "removed by the uploader" in low:
        return "This video is unavailable. It may have been removed, made private, or blocked in your region."
    if "private video" in low or "members-only" in low or "join this channel" in low:
        return "This video is private or members-only, so it can't be downloaded."
    if "sign in to confirm your age" in low or "age" in low and "restricted" in low:
        return "This video is age-restricted and can't be downloaded without signing in."
    if "requested format is not available" in low or "no video formats" in low:
        return "No downloadable formats were found for this link."
    if any(s in low for s in ("getaddrinfo", "name resolution", "network is unreachable", "failed to resolve", "unable to download webpage")):
        return "Couldn't reach YouTube. Check your internet connection and try again."
    if "http error 429" in low or "too many requests" in low:
        return "YouTube is temporarily rate-limiting requests. Please wait a minute and try again."
    first = (text.splitlines()[0] if text else "").replace("ERROR:", "").strip()
    return first or "Something went wrong reading this link. Please check the URL and try again."


def extract_playlist_id(url: str) -> str:
    """Return a real playlist id from a URL, ignoring auto-generated mixes (RD/UL)."""
    list_id = parse_qs(urlparse(url).query).get("list", [""])[0]
    if list_id.startswith(("RD", "UL", "RDMM")):
        return ""  # YouTube "mix"/radio — infinite, not a real downloadable playlist
    return list_id


def resolve_download_target(url: str, requested_mode: str) -> tuple[str, str]:
    """Decide whether to act on the single video or the whole playlist.

    A `watch?v=X&list=Y` link defaults to the single video the user pasted; the
    whole playlist is only used when there's no single video, or the user
    explicitly picks Playlist. Returns (mode, url_to_download).
    """
    video_id = extract_video_id(url)
    list_id = extract_playlist_id(url)
    if requested_mode == "playlist" and list_id:
        mode = "playlist"
    elif requested_mode == "single" and video_id:
        mode = "single"
    elif list_id and not video_id:
        mode = "playlist"
    else:
        mode = "single"
    if mode == "playlist":
        target = f"https://www.youtube.com/playlist?list={list_id}" if list_id else url
    else:
        target = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    return mode, target


def fetch_preview(url: str, requested_mode: str) -> PreviewData:
    if not is_probable_youtube_url(url):
        raise RuntimeError("Please paste a valid YouTube link (youtube.com or youtu.be).")
    detected_kind, target = resolve_download_target(url, requested_mode)
    command = yt_dlp_base_command()
    command.extend(["--dump-single-json", "--skip-download", "--flat-playlist", target])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "Preview failed."
        write_app_log(f"preview failed for {url!r} (target={target!r}): {stderr[:500]}")
        raise RuntimeError(friendly_ytdlp_error(stderr))

    try:
        data = json.loads(completed.stdout)
    except Exception:  # noqa: BLE001
        write_app_log(f"preview parse failed for {url!r}")
        raise RuntimeError("Couldn't read this link. Make sure it's a valid YouTube video or playlist URL.")
    entries = data.get("entries") or []
    warning = ""
    if detected_kind == "single" and extract_playlist_id(url):
        warning = "This video is part of a playlist. Switch to \"Playlist\" if you want to download all of its videos."

    if detected_kind == "playlist":
        first_entry = entries[0] if entries else {}
        preview_url = build_video_url_from_entry(first_entry) or url
        title = data.get("title") or "Untitled playlist"
        channel = data.get("channel") or data.get("uploader") or "Unknown channel"
        item_count = len(entries)
        thumbnail_url = pick_thumbnail(data, first_entry if isinstance(first_entry, dict) else None)
    else:
        preview_url = data.get("webpage_url") or url
        title = data.get("title") or "Untitled video"
        channel = data.get("channel") or data.get("uploader") or "Unknown channel"
        item_count = 1
        thumbnail_url = pick_thumbnail(data)

    return PreviewData(
        input_url=url,
        requested_mode=requested_mode,
        detected_kind=detected_kind,
        title=title,
        channel=channel,
        item_count=item_count,
        preview_url=preview_url,
        embed_url=build_embed_url(preview_url),
        thumbnail_url=thumbnail_url,
        warning=warning,
        effective_url=target,
    )


def fetch_playlist_entries(url: str) -> list[dict[str, Any]]:
    command = yt_dlp_base_command()
    command.extend(["--dump-single-json", "--skip-download", "--flat-playlist", url])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "Could not load playlist entries."
        write_app_log(f"playlist load failed for {url!r}: {stderr[:500]}")
        raise RuntimeError(friendly_ytdlp_error(stderr))
    data = json.loads(completed.stdout)
    return data.get("entries") or []


def collect_existing_outputs(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    results: list[str] = []
    for file_path in sorted(folder.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix in {".part", ".ytdl"}:
            continue
        results.append(str(file_path))
    return results


def trint_media_candidates(job: DownloadJob) -> list[Path]:
    folder = Path(job.output_dir) / sanitize_name(job.preview.title)
    if not folder.exists():
        return []

    video_exts = {".mp4", ".mov", ".avi", ".wma"}
    audio_exts = {".mp3", ".m4a", ".aac", ".wav", ".mp4"}
    preferred_exts = audio_exts if job.media_type == "audio" else video_exts

    results: list[Path] = []
    for file_path in sorted(folder.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() in preferred_exts:
            results.append(file_path)
    return results


def upload_file_to_trint(path: Path, settings: TrintSettings, workspace_id: str, folder_id: str) -> dict[str, Any]:
    query: dict[str, Any] = {
        "filename": path.name,
        "detect-speaker-change": "true",
        "custom-dictionary": "true",
    }
    if workspace_id:
        query["workspace-id"] = workspace_id
    if folder_id:
        query["folder-id"] = folder_id
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return trint_request(
        "POST",
        "https://upload.trint.com/",
        settings,
        query=query,
        body_path=path,  # streamed from disk, not loaded into memory
        content_type=content_type,
        timeout=1800,
    )


def upload_job_outputs_to_trint(job: DownloadJob) -> None:
    settings = get_trint_settings()
    if not settings.configured:
        raise RuntimeError("Your saved Trint key is missing. Open Trint settings and save it again.")

    if job.trint_new_folder and not job.trint_folder_id:
        new_name = (job.trint_folder_name or "New folder").strip()
        job.set_status(f"Creating Trint folder: {new_name}")
        created = create_trint_folder(settings, new_name, job.trint_workspace_id, job.trint_parent_id)
        job.trint_folder_id = created["id"]
        job.trint_folder_name = created["name"]
        job.log(f"Created Trint folder: {created['name']}")

    files = trint_media_candidates(job)
    if not files:
        raise RuntimeError("No Trint-compatible media files were found to upload.")

    total = len(files)
    job.trint_uploaded_files = []
    for index, path in enumerate(files, start=1):
        if job.cancel_requested:
            raise DownloadCancelled("Download cancelled.")
        job.current_item_label = f"Trint upload {index} of {total}: {path.name}"
        job.set_status(f"Uploading to Trint: {path.name}")
        job.progress_percent = (index - 1) / total * 100
        job.progress_label = f"Uploading to Trint • {index} of {total}"
        upload_file_to_trint(path, settings, job.trint_workspace_id, job.trint_folder_id)
        job.trint_uploaded_files.append(path.name)
        job.log(f"Uploaded to Trint: {path.name}")
    job.progress_percent = 100.0
    job.progress_label = f"Uploaded to Trint • {total} file(s)"


def build_output_template(folder: Path, base_name: str) -> str:
    return str(folder / f"{sanitize_name(base_name)}.%(ext)s")


def add_media_args(command: list[str], media_type: str, job: DownloadJob) -> None:
    if media_type == "audio":
        if FFMPEG:
            command.extend(["-x", "--audio-format", "mp3", "--audio-quality", "128K"])
        else:
            command.extend(["-f", "bestaudio/best"])
            job.log("Audio will stay in the source format because ffmpeg is not installed.")
        return

    if media_type == "both" and not FFMPEG:
        raise RuntimeError("Audio + Video requires ffmpeg.")

    command.extend(["-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b" if FFMPEG else "b[ext=mp4]/b"])
    if FFMPEG:
        command.extend(["--merge-output-format", "mp4", "--remux-video", "mp4"])


def media_duration_seconds(path: Path) -> float | None:
    if not FFPROBE:
        return None
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def run_yt_dlp(command: list[str], job: DownloadJob) -> list[Path]:
    percent_re = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
    captured: list[Path] = []
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    ACTIVE_PROCESSES[job.id] = process

    assert process.stdout is not None
    last_lines: list[str] = []
    try:
        for raw_line in process.stdout:
            if job.cancel_requested:
                process.terminate()
            line = raw_line.strip()
            if not line:
                continue
            last_lines.append(line)
            if len(last_lines) > 20:
                last_lines.pop(0)

            if line.startswith("MOVE:"):
                captured.append(Path(line.split("MOVE:", 1)[1].strip()))
                continue

            if "[download]" in line and "%" in line:
                match = percent_re.search(line)
                if match:
                    job.progress_percent = float(match.group(1))
                    job.transfer_label = line.replace("[download]", "").strip()
                    job.progress_label = ""
                continue

            if any(token in line for token in ("Destination:", "Merging formats", "ExtractAudio", "Deleting original file")):
                write_app_log(f"job-{job.id}: {line}")

        process.wait()
        if job.cancel_requested:
            raise DownloadCancelled("Download cancelled.")
        if process.returncode != 0:
            detail = " | ".join(last_lines[-6:]) if last_lines else "No downloader output was captured."
            raise RuntimeError(f"The download could not finish. {detail}")
        return captured
    finally:
        ACTIVE_PROCESSES.pop(job.id, None)


def extract_audio(video_path: Path, job: DownloadJob) -> Path:
    if not FFMPEG:
        raise RuntimeError("ffmpeg is required for Audio + Video bundles.")

    audio_path = video_path.with_suffix(".mp3")
    job.log(f"Extracting audio from {video_path.name}")
    duration = media_duration_seconds(video_path)
    process = subprocess.Popen(
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
            "-progress",
            "pipe:1",
            "-nostats",
            str(audio_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    ACTIVE_PROCESSES[job.id] = process
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            if job.cancel_requested:
                process.terminate()
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "out_time_ms" and duration:
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                percent = min(100.0, max(0.0, (seconds / duration) * 100))
                job.progress_percent = percent
                job.progress_label = f"Preparing audio • {percent:.0f}%"
            elif key == "progress" and value == "end":
                job.progress_percent = 100.0
                job.progress_label = "Preparing audio • 100%"
        process.wait()
        if job.cancel_requested:
            raise DownloadCancelled("Download cancelled.")
        if process.returncode != 0:
            raise RuntimeError("The audio file could not be prepared.")
        return audio_path
    finally:
        ACTIVE_PROCESSES.pop(job.id, None)


def zip_files(zip_path: Path, files: list[Path], job: DownloadJob) -> Path:
    job.log(f"Creating zip bundle: {zip_path.name}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        total = len(files)
        for index, file_path in enumerate(files, start=1):
            archive.write(file_path, arcname=file_path.name)
            percent = (index / total) * 100 if total else 100
            job.progress_percent = percent
            job.progress_label = f"Creating zip • {percent:.0f}%"
    return zip_path


def zip_folder(zip_path: Path, folder: Path, job: DownloadJob) -> Path:
    job.log(f"Creating playlist zip: {zip_path.name}")
    files = [file_path for file_path in sorted(folder.rglob("*")) if file_path.is_file() and file_path != zip_path]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        total = len(files)
        for index, file_path in enumerate(files, start=1):
            archive.write(file_path, arcname=str(file_path.relative_to(folder)))
            percent = (index / total) * 100 if total else 100
            job.progress_percent = percent
            job.progress_label = f"Creating zip • {percent:.0f}%"
    return zip_path


def download_single(job: DownloadJob) -> None:
    preview = job.preview
    folder = Path(job.output_dir) / sanitize_name(preview.title)
    folder.mkdir(parents=True, exist_ok=True)
    job.current_item_label = preview.title
    job.progress_percent = 0.0
    job.progress_label = ""
    job.transfer_label = ""

    command = yt_dlp_base_command()
    command.extend(["--newline", "--print", "after_move:MOVE:%(filepath)s", "-o", build_output_template(folder, preview.title)])
    add_media_args(command, job.media_type, job)
    download_url = job.preview.effective_url or job.url
    command.append(download_url)
    job.log(f"Downloading {preview.title}")
    write_app_log(f"job-{job.id}: downloader command for single item {download_url}")

    outputs = run_yt_dlp(command, job)
    if not outputs:
        raise RuntimeError("No output files were captured.")

    if job.media_type == "both":
        video_path = outputs[-1]
        job.set_status(f"Preparing audio for {preview.title}")
        audio_path = extract_audio(video_path, job)
        job.set_status(f"Creating zip for {preview.title}")
        zip_path = zip_files(folder / f"{sanitize_name(preview.title)} bundle.zip", [video_path, audio_path], job)
        job.outputs = [str(zip_path)]
    else:
        job.outputs = [str(path) for path in outputs]


def download_playlist(job: DownloadJob, delay_seconds: int) -> None:
    playlist_title = job.preview.title
    folder = Path(job.output_dir) / sanitize_name(playlist_title)
    folder.mkdir(parents=True, exist_ok=True)
    entries = fetch_playlist_entries(job.preview.effective_url or job.url)
    total = len(entries)
    job.log(f"Playlist contains {total} item(s)")
    if total == 0:
        raise RuntimeError("No downloadable videos were found in this playlist.")

    for index, entry in enumerate(entries, start=1):
        if free_space_mb(folder) >= 0 and free_space_mb(folder) < MIN_FREE_MB:
            job.log(f"Stopping early at item {index}: low disk space. Files already downloaded are kept.")
            break
        video_url = build_video_url_from_entry(entry)
        item_title = entry.get("title") or f"Video {index}"
        if not video_url:
            job.log(f"Skipping item {index}: missing video URL.")
            continue

        job.log(f"Downloading {index} of {total}: {item_title}")
        job.current_item_label = f"{index} of {total}: {item_title}"
        job.progress_percent = 0.0
        job.progress_label = ""
        job.transfer_label = ""
        job.set_status(f"Downloading {item_title}")

        command = yt_dlp_base_command()
        output_template = build_output_template(folder, f"{index:02d} - {item_title}")
        command.extend(["--newline", "--print", "after_move:MOVE:%(filepath)s", "-o", output_template])
        add_media_args(command, job.media_type, job)
        command.append(video_url)
        write_app_log(f"job-{job.id}: downloader command for playlist item {index} {video_url}")

        try:
            item_outputs = run_yt_dlp(command, job)
            if job.media_type == "both" and item_outputs:
                job.set_status(f"Preparing audio for {item_title}")
                extract_audio(item_outputs[-1], job)
            job.log(f"Downloaded {index} of {total}: {item_title}")
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            job.log(f"Skipping item {index} because it could not be downloaded. {exc}")
            continue

        if index < total and delay_seconds > 0:
            for remaining in range(delay_seconds, 0, -1):
                if job.cancel_requested:
                    raise DownloadCancelled("Download cancelled.")
                job.set_status("Pausing briefly before the next video")
                job.progress_label = f"Starting the next video in {remaining}s"
                time.sleep(1)

    if job.media_type == "both":
        job.set_status(f"Creating zip for {playlist_title}")
        zip_path = zip_folder(folder / f"{sanitize_name(playlist_title)} bundle.zip", folder, job)
        job.outputs = [str(zip_path)]
    else:
        outputs = [str(path) for path in sorted(folder.iterdir()) if path.is_file()]
        if len(outputs) > 1:
            job.set_status(f"Creating zip for {playlist_title}")
            zip_path = zip_folder(folder / f"{sanitize_name(playlist_title)}.zip", folder, job)
            job.outputs = [str(zip_path)]
        else:
            job.outputs = outputs


def worker_loop() -> None:
    while True:
        job: DownloadJob | None = None
        with JOB_LOCK:
            if JOB_QUEUE:
                job = JOB_QUEUE.pop(0)
                job.active = True
        if job is None:
            time.sleep(0.25)
            continue

        try:
            job.log(f"Starting job #{job.id}: {job.preview.title}")
            if job.preview.detected_kind == "playlist":
                download_playlist(job, PLAYLIST_DELAY_SECONDS)
            else:
                download_single(job)
            if job.upload_to_trint:
                upload_job_outputs_to_trint(job)
            job.finished = True
            job.progress_percent = 100.0
            job.log("Download completed successfully.")
        except DownloadCancelled:
            job.cancelled = True
            job.finished = True
            folder = Path(job.output_dir) / sanitize_name(job.preview.title)
            job.outputs = collect_existing_outputs(folder)
            job.transfer_label = ""
            job.progress_label = ""
            job.log("Download cancelled. Anything already finished is still saved in your folder.")
        except Exception as exc:  # noqa: BLE001
            job.failed = True
            job.log(f"Job failed: {exc}")
            job.log(traceback.format_exc().strip())
        finally:
            job.active = False
            job.updated_at = time.time()


def next_job_id() -> int:
    global NEXT_JOB_ID
    with JOB_LOCK:
        value = NEXT_JOB_ID
        NEXT_JOB_ID += 1
        return value


def queue_job(
    url: str,
    requested_mode: str,
    media_type: str,
    output_dir: str,
    upload_to_trint: bool,
    trint_workspace_id: str,
    trint_workspace_name: str,
    trint_folder_id: str,
    trint_folder_name: str,
    trint_new_folder: bool = False,
    trint_parent_id: str = "",
) -> DownloadJob:
    if media_type not in {"video", "audio", "both"}:
        raise RuntimeError("Choose Video, Audio, or Audio + Video.")
    if media_type == "both" and not FFMPEG:
        raise RuntimeError("Audio + Video requires ffmpeg.")
    chosen_dir = Path(output_dir).expanduser() if output_dir else default_output_dir()
    if not chosen_dir.exists():
        raise RuntimeError("The selected save folder does not exist.")
    if not chosen_dir.is_dir():
        raise RuntimeError("The selected save path is not a folder.")
    ensure_free_space(chosen_dir)
    if upload_to_trint and not get_trint_settings().configured:
        raise RuntimeError("Save your Trint user key first before turning on Upload to Trint.")

    preview = fetch_preview(url, requested_mode)
    job_id = next_job_id()
    job_log_file = LOGS_DIR / f"job-{job_id}.log"
    job = DownloadJob(
        id=job_id,
        url=url,
        requested_mode=requested_mode,
        media_type=media_type,
        output_dir=str(chosen_dir),
        preview=preview,
        upload_to_trint=upload_to_trint,
        trint_workspace_id=trint_workspace_id,
        trint_workspace_name=trint_workspace_name,
        trint_folder_id=trint_folder_id,
        trint_folder_name=trint_folder_name,
        trint_new_folder=trint_new_folder,
        trint_parent_id=trint_parent_id,
        log_path=str(job_log_file),
    )

    with JOB_LOCK:
        active_exists = any(existing.active for existing in JOBS.values())
        waiting_ahead = len(JOB_QUEUE)
        JOBS[job.id] = job
        JOB_QUEUE.append(job)

    job.queue_message = "Download started." if not active_exists and waiting_ahead == 0 else f"Download queued. {waiting_ahead + 1} job(s) are waiting on this machine."
    job.log(job.queue_message)
    job.log(f"Requested mode: {requested_mode}")
    job.log(f"Detected mode: {preview.detected_kind}")
    job.log(f"Media type: {media_type}")
    job.log(f"Save folder: {chosen_dir}")
    job.log(f"Free space in save folder: {free_space_mb(chosen_dir)} MB")
    if upload_to_trint:
        drive = trint_workspace_name or "My Drive"
        if trint_new_folder:
            job.log("Trint upload: on")
            job.log(f"Trint destination: {drive} / {trint_folder_name} (new folder, created on download)")
        else:
            destination = trint_folder_name or "Top level"
            job.log("Trint upload: on")
            job.log(f"Trint destination: {drive} / {destination}")
    return job


def serialize_trint_settings(settings: TrintSettings) -> dict[str, Any]:
    return {
        "configured": settings.configured,
        "key_id": settings.key_id if settings.configured else "",
        "key_secret": settings.key_secret if settings.configured else "",
        "workspace_id": settings.workspace_id,
        "workspace_name": settings.workspace_name,
        "folder_id": settings.folder_id,
        "folder_name": settings.folder_name,
    }


def cancel_job(job_id: int | None = None) -> str:
    with JOB_LOCK:
        active_job = None
        if job_id is not None:
            target_job = JOBS.get(job_id)
            if target_job is None:
                raise RuntimeError("That download could not be found.")
            if target_job in JOB_QUEUE:
                JOB_QUEUE.remove(target_job)
                target_job.cancelled = True
                target_job.finished = True
                target_job.log("Download cancelled before it started.")
                return "The queued download was cancelled."
            if target_job.active:
                active_job = target_job
        else:
            active_job = next((job for job in JOBS.values() if job.active), None)
            if active_job is None and JOB_QUEUE:
                target_job = JOB_QUEUE.pop(0)
                target_job.cancelled = True
                target_job.finished = True
                target_job.log("Download cancelled before it started.")
                return "The queued download was cancelled."

    if active_job is None:
        raise RuntimeError("There is no active download to cancel.")

    active_job.cancel_requested = True
    active_job.set_status("Cancelling this download...")
    process = ACTIVE_PROCESSES.get(active_job.id)
    if process is not None and process.poll() is None:
        process.terminate()
    write_app_log(f"job-{active_job.id}: cancel requested")
    return "Cancelling the current download. Anything already finished will stay in your folder."


def serialize_job(job: DownloadJob) -> dict[str, Any]:
    payload = asdict(job)
    payload["preview"] = asdict(job.preview)
    payload["download_links"] = [f"/files/{job.id}/{index}" for index, _ in enumerate(job.outputs)]
    payload["log_filename"] = Path(job.log_path).name if job.log_path else ""
    return payload


def current_state() -> dict[str, Any]:
    with JOB_LOCK:
        jobs = sorted(JOBS.values(), key=lambda item: item.created_at, reverse=True)
        active = next((job for job in jobs if job.active), None)
        queued = [job for job in jobs if not job.active and not job.finished and not job.failed and not job.cancelled]
        completed = [job for job in jobs if job.finished or job.cancelled or job.failed][-6:]
    return {
        "active_job": serialize_job(active) if active else None,
        "queued_jobs": [serialize_job(job) for job in queued],
        "completed_jobs": [serialize_job(job) for job in reversed(completed)],
    }


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(render_page())
            return
        if parsed.path == "/api/state":
            self._send_json(current_state())
            return
        if parsed.path == "/api/trint/settings":
            settings = get_trint_settings()
            try:
                workspaces = list_trint_workspaces(settings) if settings.configured else [{"id": "", "name": "My Drive"}]
            except Exception:  # noqa: BLE001
                workspaces = [{"id": "", "name": "My Drive"}]
            self._send_json(
                {
                    "settings": serialize_trint_settings(settings),
                    "workspaces": workspaces,
                }
            )
            return
        if parsed.path.startswith("/files/"):
            self._serve_output_file(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        if self.path == "/":
            self._send_html_headers(render_page())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {
            "/api/preview",
            "/api/queue",
            "/api/cancel",
            "/api/choose-folder",
            "/api/open-logs",
            "/api/trint/settings/save",
            "/api/trint/settings/clear",
            "/api/trint/workspaces",
            "/api/trint/folders",
            "/api/trint/browse",
            "/api/trint/destination/save",
            "/api/trint/create-folder",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            payload = parse_json_body(self)
        except json.JSONDecodeError:
            self._send_json({"error": "The request payload was not valid JSON."}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/trint/settings/save":
            try:
                key_id = str(payload.get("key_id", "")).strip()
                key_secret = str(payload.get("key_secret", "")).strip()
                if not key_id or not key_secret:
                    raise RuntimeError("Paste both your Trint key ID and key secret.")
                settings = TrintSettings(
                    key_id=key_id,
                    key_secret=key_secret,
                    workspace_id=str(payload.get("workspace_id", "")).strip(),
                    workspace_name=str(payload.get("workspace_name", "")).strip(),
                    folder_id=str(payload.get("folder_id", "")).strip(),
                    folder_name=str(payload.get("folder_name", "")).strip(),
                )
                list_trint_workspaces(settings)
                save_trint_settings(settings)
                self._send_json({"settings": serialize_trint_settings(settings)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/trint/settings/clear":
            clear_trint_settings()
            self._send_json({"settings": serialize_trint_settings(TrintSettings())})
            return

        if parsed.path == "/api/trint/workspaces":
            try:
                settings = get_trint_settings()
                self._send_json({"workspaces": list_trint_workspaces(settings)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/trint/folders":
            try:
                settings = get_trint_settings()
                workspace_id = str(payload.get("workspace_id", "")).strip()
                self._send_json({"folders": list_trint_folders(settings, workspace_id), "selected_folder_id": ""})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/trint/browse":
            try:
                settings = get_trint_settings()
                workspace_id = str(payload.get("workspace_id", "")).strip()
                folder_id = str(payload.get("selected_folder_id", "")).strip()
                self._send_json(build_trint_browse_payload(settings, workspace_id, folder_id))
            except Exception as exc:  # noqa: BLE001
                write_app_log(f"trint-browse-error: workspace={payload.get('workspace_id', '')} folder={payload.get('selected_folder_id', '')} error={exc}")
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/trint/create-folder":
            try:
                settings = get_trint_settings()
                name = str(payload.get("name", "")).strip()
                workspace_id = str(payload.get("workspace_id", "")).strip()
                workspace_name = str(payload.get("workspace_name", "")).strip()
                parent_id = str(payload.get("parent_id", "")).strip()
                if not name:
                    raise RuntimeError("Enter a folder name first.")
                folder = create_trint_folder(settings, name, workspace_id, parent_id)
                current = settings
                current.workspace_id = workspace_id
                current.workspace_name = workspace_name
                current.folder_id = folder["id"]
                current.folder_name = folder["name"]
                save_trint_settings(current)
                self._send_json({"folder": folder})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/trint/destination/save":
            try:
                settings = save_trint_destination(
                    str(payload.get("workspace_id", "")).strip(),
                    str(payload.get("workspace_name", "")).strip(),
                    str(payload.get("folder_id", "")).strip(),
                    str(payload.get("folder_name", "")).strip(),
                )
                self._send_json({"settings": serialize_trint_settings(settings)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/cancel":
            try:
                job_id_raw = payload.get("job_id")
                job_id = int(job_id_raw) if job_id_raw not in (None, "") else None
                self._send_json({"message": cancel_job(job_id)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/choose-folder":
            try:
                current = str(payload.get("current", "")).strip()
                self._send_json({"path": choose_folder_dialog(current) or ""})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/open-logs":
            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                subprocess.run(["open", str(LOGS_DIR)], check=False)
                self._send_json({"path": str(LOGS_DIR)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if not YT_DLP:
            self._send_json({"error": "yt-dlp is not installed. Install it first, then refresh this page."}, status=HTTPStatus.BAD_REQUEST)
            return

        url = str(payload.get("url", "")).strip()
        requested_mode = str(payload.get("requested_mode", "single")).strip()
        if requested_mode not in {"single", "playlist"}:
            self._send_json({"error": "Choose either Single video or Playlist."}, status=HTTPStatus.BAD_REQUEST)
            return
        if not url:
            self._send_json({"error": "Paste a YouTube link first."}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            if parsed.path == "/api/preview":
                preview = fetch_preview(url, requested_mode)
                self._send_json({"preview": asdict(preview)})
                return

            media_type = str(payload.get("media_type", "video")).strip()
            output_dir = str(payload.get("output_dir", "")).strip()
            upload_to_trint = bool(payload.get("upload_to_trint", False))
            trint_workspace_id = str(payload.get("trint_workspace_id", "")).strip()
            trint_workspace_name = str(payload.get("trint_workspace_name", "")).strip()
            trint_folder_id = str(payload.get("trint_folder_id", "")).strip()
            trint_folder_name = str(payload.get("trint_folder_name", "")).strip()
            trint_new_folder = bool(payload.get("trint_new_folder", False))
            trint_parent_id = str(payload.get("trint_parent_id", "")).strip()
            job = queue_job(
                url,
                requested_mode,
                media_type,
                output_dir,
                upload_to_trint,
                trint_workspace_id,
                trint_workspace_name,
                trint_folder_id,
                trint_folder_name,
                trint_new_folder,
                trint_parent_id,
            )
            self._send_json({"job": serialize_job(job), "preview": asdict(job.preview)})
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _serve_output_file(self, full_path: str) -> None:
        parts = [segment for segment in full_path.split("/") if segment]
        if len(parts) != 3:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            job_id = int(parts[1])
            output_index = int(parts[2])
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        with JOB_LOCK:
            job = JOBS.get(job_id)
            if job is None or output_index < 0 or output_index >= len(job.outputs):
                target = None
            else:
                target = Path(job.outputs[output_index]).resolve()

        if target is None or not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type, _ = mimetypes.guess_type(str(target))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.end_headers()
        # Stream from disk in chunks instead of loading the whole file into memory.
        try:
            with target.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send_html_headers(body, status)
        self.wfile.write(body)

    def _send_html_headers(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json_response(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _binary_version(path: str | None) -> str:
    if not path:
        return "MISSING"
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def log_environment(url: str) -> None:
    """Write a diagnostic snapshot to app.log so problems can be traced remotely."""
    import platform

    write_app_log("================ app starting ================")
    write_app_log(f"serving at {url}")
    write_app_log(f"version frozen={is_frozen()} python={platform.python_version()} os={platform.platform()}")
    write_app_log(f"data_dir={DATA_DIR}")
    write_app_log(f"yt-dlp path={YT_DLP} version={_binary_version(YT_DLP)}")
    write_app_log(f"ffmpeg path={FFMPEG} version={_binary_version(FFMPEG)}")
    write_app_log(f"ffprobe present={bool(FFPROBE)} node present={bool(NODE)}")
    trint = get_trint_settings()
    write_app_log(f"trint key configured={trint.configured}")


def choose_folder_dialog(initial: str = "") -> str | None:
    """Open the native macOS folder picker and return the chosen POSIX path.

    Works because the server runs on the same Mac as the user. Returns None if
    the user cancels or the dialog can't be shown.
    """
    script = (
        'tell application "System Events" to activate\n'
        'POSIX path of (choose folder with prompt '
        '"Choose where to save your downloads")'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None  # user pressed Cancel, or no GUI session
    path = result.stdout.strip()
    return path or None


def pick_port(preferred: int) -> int:
    """Use the preferred port if free, otherwise let the OS pick a free one."""
    for candidate in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((HOST, candidate))
                return probe.getsockname()[1]
        except OSError:
            continue
    return preferred


def run_macos_app(url: str, open_browser: bool = True) -> bool:
    """Run a minimal Cocoa app loop so the bundled .app behaves like a real Mac
    app: a working Quit menu (Cmd-Q), reopen-on-Dock-click (re-opens the browser
    tab), and a responsive process (no Dock bounce / force-quit).

    The HTTP server runs in a background thread; this owns the main thread.
    Returns False if AppKit is unavailable (e.g. a plain dev run).
    """
    try:
        import AppKit
        from Foundation import NSObject
    except Exception:  # noqa: BLE001
        return False

    def open_ui() -> None:
        webbrowser.open(url)

    class _Delegate(NSObject):
        def applicationDidFinishLaunching_(self, _notification):  # noqa: N802
            if open_browser:
                open_ui()

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _flag):  # noqa: N802
            # Fired when the user clicks the Dock icon while the app is running.
            open_ui()
            return True

        def openUI_(self, _sender):  # noqa: N802
            open_ui()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    delegate = _Delegate.alloc().init()
    app.setDelegate_(delegate)

    # Build a minimal menu bar: an "Open" item and a real Quit item.
    main_menu = AppKit.NSMenu.alloc().init()
    app_item = AppKit.NSMenuItem.alloc().init()
    main_menu.addItem_(app_item)
    app.setMainMenu_(main_menu)

    app_menu = AppKit.NSMenu.alloc().init()
    open_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Open Beam YouTube Downloader", "openUI:", "o"
    )
    open_item.setTarget_(delegate)
    app_menu.addItem_(open_item)
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    app_menu.addItem_(
        AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Beam YouTube Downloader", "terminate:", "q"
        )
    )
    app_item.setSubmenu_(app_menu)

    app.activateIgnoringOtherApps_(True)
    app.run()
    return True


def main() -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    port = pick_port(DEFAULT_PORT)
    threading.Thread(target=worker_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, port), AppHandler)
    url = f"http://{HOST}:{port}"
    print(f"Beam YouTube Downloader is running at {url}")
    log_environment(url)

    want_browser = os.environ.get("YT_DOWNLOADER_NO_BROWSER") != "1"

    # Packaged .app: drive a proper Cocoa lifecycle (Quit menu, reopen, no bounce).
    if is_frozen():
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            if run_macos_app(url, open_browser=want_browser):
                server.server_close()
                return
        except Exception as exc:  # noqa: BLE001
            write_app_log(f"macOS app loop failed, serving in foreground instead: {exc}")

    # Dev run (or AppKit unavailable): open the browser and block on the server.
    if want_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
