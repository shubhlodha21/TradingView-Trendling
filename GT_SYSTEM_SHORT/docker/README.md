# Headless IB Gateway via Docker

Run a containerized IB Gateway with automated login (IBC), then point
the equity chaos test fleet at it. Used for the second paper account
that runs in parallel with the FX test on EC2.

## What this gives you

- IB Gateway running 24×7 with no GUI / X server
- IBC handles login automatically (no manual click-through)
- Auto-restart on crash or after IBKR's daily 5pm-ET scheduled shutdown
- One-command lifecycle (`docker compose up -d` / `down`)
- ~600 MB RAM footprint
- VNC on localhost:5900 if you ever need to debug visually

## One-time setup

```bash
cd docker
cp .env.example .env
# Edit .env in your editor — fill TWS_USERID and TWS_PASSWORD
# (your IBKR paper account credentials)
```

## Daily use

```bash
cd docker
docker compose up -d                  # start gateway in background
docker compose logs -f ib-gateway     # watch login progress
                                      # Ctrl+C to stop watching (gateway keeps running)
```

After ~90 seconds you should see:
```
ib-gateway  | [+] Server started
ib-gateway  | [+] Listening on port 4002
```

Verify from the host:
```bash
nc -zv 127.0.0.1 4002
# Expected:  Connection to 127.0.0.1 4002 port [tcp/*] succeeded!
```

## Launch the equity chaos test against this gateway

From the project root (one directory above `docker/`):

```bash
GT_IBKR_PORT=4002 \
tmux new -d -s chaos-equity \
  'python3 -u -m tests.paper.chaos_test_equity \
     --scenario rolling-kill \
     --kill-interval 600 \
     --settle 30 \
     2>&1 | tee -a logs/chaos_loop/rolling.log'
```

The `GT_IBKR_PORT=4002` env var tells the test to use the Gateway's
paper API port instead of TWS's default 7497.

## Common operations

```bash
# Check gateway status
docker compose ps

# Restart gateway (graceful)
docker compose restart ib-gateway

# Stop everything
docker compose down

# Watch live logs
docker compose logs -f --tail=100 ib-gateway

# Visual debug via VNC (in another terminal):
#   ssh -L 5900:localhost:5900 user@host       # if remote
#   open vnc://localhost:5900                  # macOS
#   # use the VNC_SERVER_PASSWORD from .env
```

## Why use Gateway + IBC instead of TWS desktop?

| | Full TWS | IB Gateway + IBC (this setup) |
|---|---|---|
| GUI required | yes (X server) | no (headless) |
| Memory | ~2 GB | ~600 MB |
| Daily auto-shutdown | manual restart | IBC handles automatically |
| Crash recovery | manual | container restart policy |
| Setup complexity | low | low (this docker-compose) |
| Suitable for unattended | poor | excellent |

## Security notes

- API ports (4001, 4002) are bound to `127.0.0.1` only — never reachable
  from the public internet
- VNC port (5900) is also `127.0.0.1` only — SSH-tunnel to access remotely
- `.env` is in `.gitignore` — credentials never touch git
- The container runs as a non-root user (`ibgateway`)
- Auto-restart at 23:55 dodges IBKR's daily 5pm-ET reset window

## Image source

[`ghcr.io/gnzsnz/ib-gateway:stable`](https://github.com/gnzsnz/ib-gateway-docker)
— maintained open-source image, ~100k pulls, regularly updated for new
IB Gateway releases.
