# Beam download queue (coordination-only server)

A tiny stdlib-Python server that hands out a limited number of "download slots"
so teammates behind the same public IP don't all hit YouTube at once. It runs
**only the queue** — it never touches YouTube, video files, or Trint keys.

This is an **experiment**. The desktop app only uses it when a queue URL + token
are filled in under Settings; with those blank, the app behaves exactly as
normal (fully local).

## What it does and doesn't see

- Receives: an opaque random per-install id, and (unavoidably, like any server)
  the source IP — which it turns into a **salted hash** used only to group
  same-network clients, and **never logs or stores raw**.
- Never receives: video URLs, titles, names, Trint keys, or files.
- Tells each client only its **own** status / position / rough ETA.

## Deploy on Render (free)

1. Push this repo to GitHub (the `queue-server/` folder).
2. Render → New → **Web Service** → pick the repo. It reads `render.yaml`
   (root dir `queue-server`, start command `python main.py`).
3. Set env var **`TEAM_TOKEN`** to a long random secret (e.g. `openssl rand -hex 24`).
   Optionally tune `PER_IP_LIMIT` (default 2).
4. Deploy. You get a URL like `https://beam-queue.onrender.com`.
5. Share the **URL + TEAM_TOKEN** with testers (secure channel). They paste both
   into the app's Settings → "Shared download queue".

Free instances sleep after ~15 min idle and take ~30–60s to wake; the app waits
for that and falls back to a normal local download if it can't reach the server.

## Tuning (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `TEAM_TOKEN` | — | required shared secret; server won't start without it |
| `PER_IP_LIMIT` | 2 | max concurrent downloads per network (per source IP) |
| `GLOBAL_LIMIT` | 0 (off) | optional hard cap across everyone |
| `HEARTBEAT_TIMEOUT` | 90 | seconds before a silent active slot is reclaimed |
| `WAITING_TIMEOUT` | 30 | seconds before a client that stopped polling is dropped |

## Run locally (for testing)

```bash
TEAM_TOKEN=test123 PER_IP_LIMIT=2 python queue-server/main.py
# server on http://localhost:8080
```

Logs print to stdout (Render captures these). Example lines:

```
[enqueue] t=ab12cd grp=7f3a -> GO pos=0 eta=0s (group active=1/2, tickets=1)
[poll]    ... -> WAIT pos=2 eta=240s
[release] t=ab12cd grp=7f3a (tickets=0)
[reap]    removed 1 stale active, 0 stale waiting
```
