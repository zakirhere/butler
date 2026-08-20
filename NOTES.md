# Project notes

Context for whoever picks up this task next (read this before writing code —
the README covers the butler framework itself, not these features).

## SmartFind Express substitute-job watcher

The dedicated authenticated browser profile is `data/smartfind-profile`.
The username is `856027`; the password is stored in macOS Keychain under
service `com.zakbot.butler.smartfind`, account `856027`. Do not put that
password in `.env` or commit it.

`butler.smartfind_worker` polls the Available Jobs route every 30 minutes.
The launchd worker must own the profile, so close the visible SmartFind
Chrome window before loading `launchd/com.zakbot.butler-smartfind.plist`.
SmartFind CAPTCHA is intentionally not automated. If the session expires,
the worker posts a Slack warning and waits for a manual login/CAPTCHA.

`BUTLER_SMARTFIND_AUTO_ACCEPT=false` is the safe dry-run default. Set it to
`true` only after verifying one manual scan; with it enabled, every detected
available job is accepted without date, school, classification, or location
filtering. If SmartFind reports that it is calling other substitutes, the
worker retries the popup Accept button every 5 seconds for up to 12 attempts
by default, stopping on a success message or retry exhaustion.

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
and flight-booking APIs are generally gated behind travel-agency/business
approval anyway, not something a personal script can casually wire up.

### API provider — history, don't re-litigate this

Went through three options before landing on Duffel:

1. **Amadeus for Developers Self-Service** — originally chosen (free,
   no-card signup). As of 2026-07-17 the self-service portal is
   decommissioned; developers.amadeus.com is now "Enterprise API Portal"
   only, sales-gated ("Request access... get support from one of our
   experienced travel consultants"). Dead for hobbyist use, don't retry.
2. **Kiwi.com Tequila** — considered as a fallback early on. Turns out
   it's been closed to new signups since **2024-05-30**
   (partners.kiwi.com redirects to a blog post: "Any new partnerships on
   the Tequila platform will be on an invitation only basis"). Was never
   actually viable in this window — don't retry.
3. **Duffel** — what's implemented. Genuinely self-serve (~1 min signup,
   no sales call), real-time GDS/NDC pricing, docs at docs.duffel.com.
   **Important**: needs a *live* API key (`duffel_live_...`), not test
   mode — test-mode keys only return simulated fares from a fake airline
   ("Duffel Airways"), useless for real price tracking. A live key
   requires a payment method on file with Duffel (their search fee is
   ~$0.005/search past a free allowance — trivial for our volume, but a
   real card is on file, unlike the old Amadeus free tier). User chose to
   go live on Duffel with this tradeoff accepted (2026-08-17).

If Duffel also becomes unavailable someday, FlightAPI.io was the next
candidate researched (purpose-built for price-tracking only, no booking
capability, 20 free calls then $49/mo) — verify it's still viable before
switching, this space seems to be consolidating away from free tiers.

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

`butler/flights.py`: single Bearer key against Duffel (`duffel_api_key` in
config, no OAuth exchange needed — simpler than Amadeus was). Per candidate
date: `POST /air/offer_requests` with `return_offers` defaulting to true,
so the offers array comes back synchronously in the same response — no
second call. For a flexible leg this means one request per date in the
window (5 requests for a ±2-day window), not a single ranged query; Duffel
doesn't have a cheapest-date-range endpoint like Amadeus did, so this is
the only option, not a shortcut taken for convenience.

Dedup/new-low tracking is per-route (`route.key`, not per-date) in
`data/flight-seen.json` — a flexible leg only re-notifies if its best price
across the whole window drops below the last notified price, not on every
date searched.

### Quota / cost note

~12 Duffel requests per full scan (2 fixed legs + 2 flexible legs × 5 dates
each). `launchd/com.zakbot.butler-flights.plist` runs this twice a day
(06:00 and 18:00), not hourly like Marketplace — a 4-months-out trip
doesn't need hourly granularity, and it keeps the (small) per-search cost
down. ~24 requests/day ≈ 720/month; at Duffel's ~$0.005/search-past-free-
allowance this is a few dollars/month worst case. Verify current pricing
on the Duffel dashboard before changing this schedule upward.

### Setup still needed (nothing in code does this)

- Duffel account (https://duffel.com) + a **live** API key — test keys
  won't work for this, see above. Live access needs a payment method on
  file.
  - **Status as of 2026-08-17**: account created, "Go live" submitted,
    business details under review ("usually less than 2 business days"
    per Duffel's own banner). No live key exists yet — don't try to
    enable `BUTLER_FLIGHT_ENABLED` until this clears, a test-mode key
    will just silently return fake fares.
- `BUTLER_DUFFEL_API_KEY=duffel_live_...` and `BUTLER_FLIGHT_ENABLED=true`
  in `.env`, plus the Slack bot token/channel already required by the
  Marketplace feature (reused here unless `BUTLER_FLIGHT_SLACK_CHANNEL_ID`
  overrides it)
- `launchctl load` the flights plist once the above is set
- One manual dry run via `POST /tasks/flight_watch` before trusting the
  cron, same as recommended for Marketplace
