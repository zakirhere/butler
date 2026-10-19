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

## Home listing follow-ups

The scheduled home watcher keeps the RentCast listing ID and Slack timestamp
for each alerted home. It checks tracked listings weekly, posts a `⏳`
reply when RentCast reports a pending or under-contract status, and posts a
`🎉` reply with the recorded sale price and date when the property record shows
a sale after the listing date. Price change alerts are replies in the original
alert thread. Follow-up checks count against the same monthly RentCast request
limit as home searches. At most 20 listings are followed at once by default;
newly alerted homes replace the oldest active follow-ups (LIFO). Set
`BUTLER_HOME_FOLLOWUP_MAX_ACTIVE` to change the cap.

RentCast currently documents listing statuses as `Active` or `Inactive`, so a
pending reply is sent only when a listing response provides a pending-like
status. Recorded sale prices can appear after closing when public transaction
data reaches RentCast; non-disclosure records may not include a price.

## Unofficial personal WhatsApp notifications

The optional `whatsapp-bridge/` service uses `whatsapp-web.js` and a
separate personal WhatsApp account. It is independent of the Python daemon,
binds only to `127.0.0.1`, and persists its browser session under `data/`.
WhatsApp may restrict or ban accounts used for unofficial automation.

Install Node.js first, then install the bridge dependencies:

```bash
cd whatsapp-bridge
npm install
cd ..
```

Add the `WHATSAPP_*` values from `.env.example` to the root `.env`. Use an
international phone number without `+` for `WHATSAPP_RECIPIENT`; groups use
their WhatsApp ID ending in `@g.us`.

Run the bridge once in the foreground to authenticate:

```bash
cd whatsapp-bridge
node server.js
```

Scan the QR code printed in the terminal. Stop it after `ready` appears. The
session is then retained in `data/whatsapp-auth`.

After authentication, list available groups and copy the desired `id` into
`WHATSAPP_GROUP_ID`:

```bash
curl http://127.0.0.1:8792/groups \
  -H "Authorization: Bearer <your WHATSAPP_BRIDGE_TOKEN>"
```

Install it as a persistent macOS service:

```bash
mkdir -p logs
cp launchd/com.zakbot.butler-whatsapp.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zakbot.butler-whatsapp.plist
```

Test through Butler without running a home search:

```bash
curl -X POST http://127.0.0.1:8790/tasks/whatsapp_test \
  -H "Authorization: Bearer <your BUTLER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"payload":{"text":"Butler WhatsApp test","target":"all"}}'
```

`target` can be `recipient`, `group`, or `all`. The bridge deduplicates the
same text per destination and limits deliveries per minute; both controls can
be configured with `WHATSAPP_DEDUP_TTL_SECONDS` and
`WHATSAPP_MAX_MESSAGES_PER_MINUTE`.

The bridge uses the installed Google Chrome executable by default, so npm’s
blocked Puppeteer download script is not required.

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

### Run history and approvals

Butler records HTTP-triggered task runs in SQLite at `data/runs.sqlite3` by
default. The run ledger stores the payload, status, result or error, and
timestamps. Inspect it with:

```bash
curl http://<tailscale-ip>:8787/tasks/runs \
  -H "Authorization: Bearer <your BUTLER_TOKEN>"
```

Tasks are automatic by default. A task that can take an external action can
declare `@task("name", approval="approval_required")`. Its POST response will
contain a `run_id` and `approval_required` status; approve it explicitly with:

```bash
curl -X POST http://<tailscale-ip>:8787/tasks/runs/<run_id>/approve \
  -H "Authorization: Bearer <your BUTLER_TOKEN>"
```

`smartfind_scan` currently uses this approval policy. Scheduled workers remain
separate and continue to follow their existing feature flags.

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

## Scheduled tasks (Marketplace, flight watching)

Marketplace review includes up to eight listing photos. A listing is alerted
only when the photos visually support the refreshed Model Y (Juniper) design;
missing or inconclusive photos are skipped. Photo evidence is a screening
signal, not proof of model year, VIN, title, or vehicle condition.

Some tasks run on a schedule instead of waiting for a phone request. Flight
watching uses one consolidated job at 6 AM and 6 PM. It checks the four
configured exact flights sequentially with a provider-safe delay, then posts
one family fare summary when any monitored fare reaches a new low. The worker
runs directly (not through the HTTP server), so it works even if the main
server isn't running:

```bash
cp launchd/com.zakbot.butler-marketplace.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zakbot.butler-marketplace.plist
launchctl load launchd/com.zakbot.butler-flights.plist
launchctl load launchd/com.zakbot.butler-flight-weekly.plist
```

The weekly flight job runs Sunday at 7 AM and summarizes the previous seven
days of checks, including fare results, unavailable flights, provider errors,
observed lows, and current baselines.

Each feature also has its own `*_enabled` flag in `.env` (e.g.
`BUTLER_FLIGHT_ENABLED`) — it's a no-op until you flip that on, even if the
launchd job is loaded. Do one manual run via the matching `POST /tasks/...`
endpoint before trusting the cron. See `NOTES.md` for what each feature
does and what setup (credentials, one-time logins) it still needs.

### SmartFind Express substitute jobs

`butler.smartfind_worker` attaches to an already-open Chrome session over CDP
and checks the Milpitas SmartFind Available Jobs page every 30 minutes. It
never launches a headless browser or performs an automatic login. Chrome must
be started with remote debugging enabled (default endpoint
`http://127.0.0.1:9222`); if the existing session is not logged in, the worker
pauses and posts a Slack warning.

The worker is deliberately opt-in for acceptance:

```bash
BUTLER_SMARTFIND_ENABLED=true
BUTLER_SMARTFIND_AUTO_ACCEPT=true
```

First run with `BUTLER_SMARTFIND_AUTO_ACCEPT=false` and call
`POST /tasks/smartfind_scan` once. Install the visible Chrome launch agent and
log into SmartFind in that browser session:

```bash
cp launchd/com.zakbot.butler-smartfind-chrome.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zakbot.butler-smartfind-chrome.plist
```

Then install the worker:

```bash
cp launchd/com.zakbot.butler-smartfind.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zakbot.butler-smartfind.plist
```

Only enable auto-accept after verifying that the dry-run Slack message shows
the expected job fields. The service accepts every detected job when enabled;
it does not apply school, date, classification, or location filters. If
SmartFind reports that it is calling other substitutes, the worker retries
the popup Accept button every 5 seconds, up to 12 retries by default, and
stops when a success message appears or the retry limit is reached.
