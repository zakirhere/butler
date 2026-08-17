# Project notes

Context for whoever picks up this task next (read this before writing code —
the README covers the butler framework itself, not these features).

## Facebook Marketplace watcher

## Goal

Hourly scan of Facebook Marketplace for a specific car listing. When a
genuine match is found, post it to Slack. Nothing more — see "Explicitly out
of scope" below.

- Search: 2026 Tesla Model Y
- Price: $35,000–$41,000
- Location: Las Vegas, NV, within 45mi
- Scan URL: `https://www.facebook.com/marketplace/vegas/search/?query=2026%20tesla%20model%20y&minPrice=35000&maxPrice=41000`
- On match, notify Slack channel `#marketplace-tesla` (ID `C0BQ44F6HM5`,
  workspace `zak-personal`) via `butler.slack.notify()` — see README's
  "Slack notifications" section for how that's wired.

## Explicitly out of scope

**Do not build auto-messaging to sellers.** Facebook has no public API for
Marketplace messaging, so it would require browser automation against their
web UI — that violates Meta's ToS (anti-scraping/anti-bot policies) and
risks the account being flagged or banned. The scanner posts a Slack
notification only; the user messages sellers manually. If this is revisited
later, it's a deliberate, separate decision — don't infer it back in.

## Open decisions — ask the user, don't assume

1. ~~**Facebook session for scanning.**~~ Resolved: dedicated automation-only
   browser profile (`data/facebook-profile`), launched persistently via
   Playwright. Note it's a one-time manual step to actually log into that
   profile before the first scheduled scan — nothing in the code performs
   the login itself.

2. ~~**LLM for match-scoring.**~~ Resolved: OpenAI (`butler/llm.py`). Env
   var is `OPENAI_API_KEY` (no `BUTLER_` prefix) — an exception to this
   project's usual `BUTLER_*` convention, so it's easy to miss when setting
   up `.env`.

3. ~~**Scheduling mechanism.**~~ Resolved: a second `launchd` plist per
   scheduled feature (see `launchd/com.zakbot.butler-marketplace.plist`),
   not an internal scheduler.

## Needed regardless (no open question, just build it)

- **Listing extraction**: title, price, url, thumbnail, and probably the
  full description (requires opening each listing's detail page — search
  results only show title/price/thumbnail).
- **Dedup store**: a small persisted store (JSON or SQLite is fine) of
  listing IDs already notified, so an hourly rerun doesn't re-alert on a
  listing it already flagged.

## Related repo, don't couple to it

`~/personal/trading-bot/tradebot/notify.py` has a similar (more elaborate,
multi-strategy) Slack notification pattern. `butler/slack.py` deliberately
copies the *pattern*, not the code — trading-bot is a separate live-money
repo with its own strict guardrails (see its `CLAUDE.md`) and shouldn't be
a runtime dependency of an unrelated personal project.

## Flight price watcher

### Goal

Watch a multi-city, one-way itinerary and alert on Slack when the price for
any leg drops to a new low (or under a max, if set). No booking/purchase —
same "notify only, human clicks buy" boundary as the Marketplace watcher,
deliberately: a flight purchase is an irreversible financial transaction,
and both Amadeus's and Kiwi's booking endpoints are gated behind
travel-agency/business partnership approval anyway, not something a
personal script can casually wire up.

### The itinerary (as of 2026-08-17)

Four one-way legs, `data/flight-routes.json` (gitignored — personal
itinerary, not a secret, just kept out of git like the rest of `data/`;
`flight-routes.example.json` at repo root is the committed format
reference):

1. SFO → TYO, flexible 2026-12-09 to 2026-12-13 (target 2026-12-11)
2. TYO → BOM, fixed 2026-12-16
3. BOM → CAI, flexible 2027-01-03 to 2027-01-07 (target 2027-01-05)
4. CAI → SFO, fixed 2027-01-09

"TYO" is the IATA city code covering both Narita and Haneda — used because
the user said "Japan" without a specific airport. If they want one
specifically, change `data/flight-routes.json` to `NRT` or `HND`.

Legs 1 and 3 are flexible by "a couple of days" per the user — implemented
as ±2 days. Legs 2 and 4 are fixed. If the user's actual tolerance differs
from ±2, adjust `date_start`/`date_end` in `data/flight-routes.json`.

### How it works

`butler/flights.py`: OAuth2 client-credentials against Amadeus
(`amadeus_client_id`/`secret` in config), then Flight Offers Search
(`/v2/shopping/flight-offers`) per candidate date. For a flexible leg this
means one API call per date in the window (5 calls for a ±2-day window),
not a single ranged query — Amadeus does have a dedicated Flight Cheapest
Date Search API for this, but wiring up a second endpoint with a different
response schema wasn't worth it for a 4-leg itinerary. Revisit if the
number of flexible legs grows.

Dedup/new-low tracking is per-route (`route.key`, not per-date) in
`data/flight-seen.json` — a flexible leg only re-notifies if its best price
across the whole window drops below the last notified price, not on every
date searched.

### Quota note

~12 Amadeus calls per full scan (2 fixed legs + 2 flexible legs × 5 dates
each). `launchd/com.zakbot.butler-flights.plist` runs this twice a day
(06:00 and 18:00), not hourly like Marketplace — deliberately, since the
free/test-tier Amadeus quota is limited and a 4-months-out trip doesn't
need hourly granularity anyway. Verify current quota limits on the
Amadeus account before changing this schedule.

### Setup still needed (nothing in code does this)

- Amadeus account + app (https://developers.amadeus.com) for
  `BUTLER_AMADEUS_CLIENT_ID`/`BUTLER_AMADEUS_CLIENT_SECRET`
- `BUTLER_FLIGHT_ENABLED=true` in `.env`, plus the Slack bot token/channel
  already required by the Marketplace feature (reused here unless
  `BUTLER_FLIGHT_SLACK_CHANNEL_ID` overrides it)
- `launchctl load` the flights plist once the above is set
- One manual dry run via `POST /tasks/flight_watch` before trusting the
  cron, same as recommended for Marketplace
