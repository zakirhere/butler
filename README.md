# butler

A personal automation agent: a small server that runs on this machine and
takes requests from your phone over your Tailscale tailnet. It exposes
"tasks" — Python functions you register — as HTTP endpoints, so triggering
one from Apple Shortcuts (or curl, or anything else) is a single request.

Not reachable from the public internet — it binds to this machine's
Tailscale IP, so only devices on your tailnet can reach it.

## Setup

```bash
cd /Users/zakir/zakbot/butler
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into .env as BUTLER_TOKEN
```

## Run it (development)

```bash
scripts/dev.sh
```

This binds to `127.0.0.1:8787` with auto-reload, for testing from this
machine only.

## Run it (real / on your tailnet)

```bash
source .venv/bin/activate
python -m butler.main
```

This binds to your Tailscale IPv4 address (auto-detected via `tailscale ip
-4`) on port 8787 (or `BUTLER_PORT` from `.env`).

## Try it

```bash
# No auth required — quick liveness check
curl http://<tailscale-ip>:8787/health

# Auth required — runs the example "ping" task
curl -X POST http://<tailscale-ip>:8787/tasks/ping \
  -H "Authorization: Bearer <your BUTLER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"payload": {"hello": "world"}}'

# List every registered task
curl http://<tailscale-ip>:8787/tasks -H "Authorization: Bearer <your BUTLER_TOKEN>"
```

## Slack notifications

`butler/slack.py` exposes `notify(text, channel=None)` for posting messages
from any task. It prefers a bot token (`chat.postMessage`, can target any
channel by ID) and falls back to an incoming webhook if only that's set.

Config (in `.env`, see `.env.example`):

- `BUTLER_SLACK_BOT_TOKEN` — `xoxb-...`, needs the `chat:write` scope
  (add `chat:write.public` too if you don't want to manually invite the
  bot to public channels)
- `BUTLER_SLACK_CHANNEL_ID` — default channel `notify()` posts to
- `BUTLER_SLACK_WEBHOOK_URL` — fallback if no bot token is set (bound to
  whichever channel the webhook was created for)

Verify it's wired up with the built-in test task:

```bash
curl -X POST http://<tailscale-ip>:8787/tasks/slack_test \
  -H "Authorization: Bearer <your BUTLER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"payload": {}}'
```

This is a deliberately scaled-down copy of the pattern in
`~/personal/trading-bot/tradebot/notify.py` (bot-token-first, webhook
fallback) — not an import of that package. trading-bot is a separate
live-money repo with its own strict guardrails and shouldn't be a
dependency of an unrelated project.

## Adding a new task

Create a file in `butler/tasks/`, register it with the `@task(...)`
decorator, and import it from `butler/tasks/__init__.py`:

```python
# butler/tasks/example.py
from butler.tasks.registry import task

@task("example")
async def example(payload: dict) -> dict:
    return {"received": payload}
```

```python
# butler/tasks/__init__.py
from butler.tasks import ping, example  # noqa: F401
```

It's now callable as `POST /tasks/example`.

## Wiring up from your iPhone (Apple Shortcuts)

No app needed. In the Shortcuts app:

1. New Shortcut → add "Get Contents of URL"
2. URL: `http://<tailscale-ip>:8787/tasks/ping`
3. Method: `POST`
4. Headers: `Authorization` → `Bearer <your BUTLER_TOKEN>`
5. Request Body: JSON → `{"payload": {}}`

Run it via the Shortcuts app, a home screen icon, or Siri ("Hey Siri, ping
butler"). Make sure Tailscale is connected on your phone.

## Running persistently (launchd)

To keep butler running in the background and restart it on boot:

```bash
mkdir -p logs
cp launchd/com.zakbot.butler.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zakbot.butler.plist
```

Check it's running: `launchctl list | grep butler`, or tail
`logs/butler.err.log`. To stop it:
`launchctl unload ~/Library/LaunchAgents/com.zakbot.butler.plist`.

The plist assumes the venv lives at `.venv` inside this repo — update the
`ProgramArguments` path if yours is elsewhere.
