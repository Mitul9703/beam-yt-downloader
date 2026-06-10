#!/usr/bin/env python3
"""
Beam download queue — coordination-only server (Model A).

This server NEVER touches YouTube, video files, or anyone's Trint keys. It only
hands out a limited number of "download slots" so that, when several teammates
are behind the same public IP, they don't all hit YouTube at once.

Privacy by design:
  - No video URLs, titles, names, or Trint keys are ever sent here.
  - The only thing a client sends is an opaque random per-install id.
  - The source IP is used ONLY to group same-network clients, as a SALTED HASH
    that is never logged or stored in raw form.
  - Each client is told only its own status / position / rough ETA — never
    anything about anyone else.

Run locally:   TEAM_TOKEN=secret python main.py
On Render:     set TEAM_TOKEN (and optionally PER_IP_LIMIT) as env vars;
               start command is `python main.py` (binds to $PORT).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
TEAM_TOKEN = os.environ.get("TEAM_TOKEN", "").strip()

PER_IP_LIMIT = int(os.environ.get("PER_IP_LIMIT", "2"))      # max concurrent per network
GLOBAL_LIMIT = int(os.environ.get("GLOBAL_LIMIT", "0"))      # 0 = no global backstop
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "90"))  # active slot reclaim
WAITING_TIMEOUT = int(os.environ.get("WAITING_TIMEOUT", "30"))      # drop a client that stopped polling
DEFAULT_DURATION = int(os.environ.get("DEFAULT_DURATION", "120"))   # ETA guess before we have data

# A per-process random salt: enough to make the stored group key un-reversible.
IP_SALT = os.environ.get("QUEUE_SALT") or secrets.token_hex(16)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("beam-queue")

_LOCK = threading.Lock()
# ticket_id -> {group, client, state ("waiting"|"active"), created, last_seen, started}
_TICKETS: dict[str, dict] = {}
_DURATIONS: deque[float] = deque(maxlen=30)


def group_key(ip: str) -> str:
    digest = hashlib.sha256(f"{IP_SALT}:{ip}".encode("utf-8")).hexdigest()
    return digest[:8]  # short, opaque, never reversible to the IP


def avg_duration() -> float:
    return sum(_DURATIONS) / len(_DURATIONS) if _DURATIONS else float(DEFAULT_DURATION)


def reap() -> None:
    now = time.time()
    stale_active = []
    stale_waiting = []
    for tid, t in list(_TICKETS.items()):
        if t["state"] == "active" and now - t["last_seen"] > HEARTBEAT_TIMEOUT:
            stale_active.append(tid)
        elif t["state"] == "waiting" and now - t["last_seen"] > WAITING_TIMEOUT:
            stale_waiting.append(tid)
    for tid in stale_active + stale_waiting:
        _TICKETS.pop(tid, None)
    if stale_active or stale_waiting:
        log.info("[reap] removed %d stale active, %d stale waiting (now %d tickets)",
                 len(stale_active), len(stale_waiting), len(_TICKETS))


def active_in_group(group: str) -> int:
    return sum(1 for t in _TICKETS.values() if t["group"] == group and t["state"] == "active")


def total_active() -> int:
    return sum(1 for t in _TICKETS.values() if t["state"] == "active")


def promote() -> None:
    """Promote oldest waiting tickets to active while limits allow."""
    waiting = sorted(
        (t for t in _TICKETS.values() if t["state"] == "waiting"),
        key=lambda t: t["created"],
    )
    for t in waiting:
        if GLOBAL_LIMIT and total_active() >= GLOBAL_LIMIT:
            break
        if active_in_group(t["group"]) < PER_IP_LIMIT:
            t["state"] = "active"
            t["started"] = time.time()
            t["last_seen"] = time.time()
            log.info("[grant] t=%s grp=%s active_in_group=%d/%d",
                     t["id"][:6], t["group"], active_in_group(t["group"]), PER_IP_LIMIT)


def snapshot(ticket: dict) -> dict:
    """Status for THIS ticket only (never exposes other tickets)."""
    if ticket["state"] == "active":
        return {
            "ticket_id": ticket["id"],
            "status": "go",
            "position": 0,
            "eta_seconds": 0,
            "limit": PER_IP_LIMIT,
        }
    group = ticket["group"]
    ahead = sum(
        1 for t in _TICKETS.values()
        if t["group"] == group and t["state"] == "waiting" and t["created"] < ticket["created"]
    )
    waves = math.ceil((ahead + 1) / max(1, PER_IP_LIMIT))
    return {
        "ticket_id": ticket["id"],
        "status": "waiting",
        "position": ahead + 1,
        "eta_seconds": int(waves * avg_duration()),
        "limit": PER_IP_LIMIT,
    }


def client_ip(handler: "Handler") -> str:
    xff = handler.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return handler.client_address[0] if handler.client_address else "unknown"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default per-request noise; we log our own events
        return

    def _send(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {TEAM_TOKEN}"
        return bool(TEAM_TOKEN) and secrets.compare_digest(header, expected)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self) -> None:
        if self.path in ("/", "/ping", "/health"):
            # No auth on the wake/health ping so clients can warm a sleeping server.
            self._send({"ok": True, "service": "beam-queue"})
            return
        self._send({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path not in ("/enqueue", "/poll", "/heartbeat", "/release"):
            self._send({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._authed():
            self._send({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return

        body = self._body()
        now = time.time()

        if self.path == "/enqueue":
            group = group_key(client_ip(self))
            ticket_id = secrets.token_hex(8)
            with _LOCK:
                reap()
                _TICKETS[ticket_id] = {
                    "id": ticket_id,
                    "group": group,
                    "client": str(body.get("client_id", ""))[:40],
                    "state": "waiting",
                    "created": now,
                    "last_seen": now,
                    "started": None,
                }
                promote()
                result = snapshot(_TICKETS[ticket_id])
                log.info("[enqueue] t=%s grp=%s -> %s pos=%s eta=%ss (group active=%d/%d, tickets=%d)",
                         ticket_id[:6], group, result["status"].upper(), result["position"],
                         result["eta_seconds"], active_in_group(group), PER_IP_LIMIT, len(_TICKETS))
            self._send(result)
            return

        ticket_id = str(body.get("ticket_id", ""))
        with _LOCK:
            reap()
            ticket = _TICKETS.get(ticket_id)

            if self.path == "/release":
                if ticket:
                    if ticket.get("started"):
                        _DURATIONS.append(max(1.0, now - ticket["started"]))
                    _TICKETS.pop(ticket_id, None)
                    promote()
                    log.info("[release] t=%s grp=%s (tickets=%d)", ticket_id[:6], ticket["group"], len(_TICKETS))
                self._send({"ok": True})
                return

            if ticket is None:
                # Server may have restarted (state is in-memory) — tell the client to re-enqueue.
                self._send({"error": "unknown_ticket"}, HTTPStatus.NOT_FOUND)
                return

            ticket["last_seen"] = now
            if self.path == "/heartbeat":
                self._send({"ok": True, "status": ticket["state"]})
                return

            # /poll
            promote()
            result = snapshot(ticket)
            self._send(result)


def main() -> None:
    if not TEAM_TOKEN:
        raise SystemExit("Refusing to start: set the TEAM_TOKEN environment variable.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("beam-queue listening on %s:%d  (per-IP limit=%d, global=%s)",
             HOST, PORT, PER_IP_LIMIT, GLOBAL_LIMIT or "off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
