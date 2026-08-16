# Project notes: Facebook Marketplace watcher

Context for whoever picks up this task next (read this before writing code —
the README covers the butler framework itself, not this feature).

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

1. **Facebook session for scanning.** No read API exists either, so this
   needs an authenticated Playwright browser session. Two options, both
   validated as workable, neither picked yet:
   - Dedicated automation-only browser profile (log in once, separate from
     daily browsing)
   - Reuse the user's everyday Chrome profile/cookies (simpler, but
     butler's automated session becomes indistinguishable from the user
     actively browsing)

   Either way: the saved session/cookies are equivalent to a live Facebook
   login. Do not commit them. Store outside git (add to `.gitignore`) with
   restricted file permissions.

2. **LLM for match-scoring.** The user chose LLM-based judgment over simple
   rule filters — i.e. an LLM should read each listing (title, description,
   maybe photos) and judge genuine fit, not just filter by price/keyword
   regex. No provider or API key has been chosen yet — ask which (Anthropic
   was the leaning, but not committed) and get a key before wiring this up.

3. **Scheduling mechanism.** Butler was originally purely phone-triggered
   (a task only runs when you hit the endpoint) — there's no autonomous
   scheduling yet. Decide between:
   - An internal async scheduler inside the FastAPI app (e.g. a background
     task started at app startup that sleeps/loops)
   - A second `launchd` plist (`StartCalendarInterval`) that hits a task
     endpoint hourly, similar to `launchd/com.zakbot.butler.plist`

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
