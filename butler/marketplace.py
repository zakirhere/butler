"""Read-only Facebook Marketplace scanning and Slack alerting."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urljoin

from playwright.sync_api import sync_playwright

from butler.config import settings
from butler.llm import review_listing
from butler.slack import notify

log = logging.getLogger(__name__)
BASE_URL = "https://www.facebook.com"
MONEY_RE = re.compile(r"\$\s*([\d,]+)")
REJECT_RE = re.compile(r"\b(salvage|rebuilt|rebuild|flood|lemon|junk|parts only)\b", re.I)


@dataclass
class Listing:
    url: str
    title: str
    price: int
    location: str
    detail: str
    photo_urls: list[str]


def _prices(text: str) -> list[int]:
    return [int(value.replace(",", "")) for value in MONEY_RE.findall(text)]


def _state_path() -> Path:
    path = Path(settings.marketplace_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_seen() -> set[str]:
    path = _state_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("seen", []))
    except (OSError, ValueError) as exc:
        log.warning("could not read Marketplace dedup state: %s", exc)
        return set()


def _save_seen(seen: set[str]) -> None:
    path = _state_path()
    path.write_text(json.dumps({"seen": sorted(seen)}, indent=2) + "\n")


def _browser_path() -> str:
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _location_label(slug: str) -> str:
    labels = {
        "sanfrancisco": "San Francisco",
        "losangeles": "Los Angeles",
        "portland": "Portland",
        "lasvegas": "Las Vegas",
        "saltlakecity": "Salt Lake City",
    }
    return labels.get(slug, slug.replace("-", " ").title())


def _scan_location(page, location: str) -> list[Listing]:
    url = (
        f"{BASE_URL}/marketplace/{location}/search/?query={quote(settings.marketplace_query)}"
        f"&minPrice={settings.marketplace_min_price}&maxPrice={settings.marketplace_max_price}"
    )
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    links = page.locator("a")
    candidates: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for index in range(min(links.count(), 1200)):
        anchor = links.nth(index)
        href = anchor.get_attribute("href")
        text = " ".join(anchor.inner_text(timeout=1000).split())
        if not href or "/marketplace/item/" not in href or href in seen_urls:
            continue
        seen_urls.add(href)
        if "model y" not in text.lower() or "2026" not in text.lower():
            continue
        values = _prices(text)
        if not values:
            continue
        price = values[0]
        if not settings.marketplace_min_price <= price <= settings.marketplace_max_price:
            continue
        candidates.append((urljoin(BASE_URL, href.split("?")[0]), text))

    listings: list[Listing] = []
    for item_url, card_text in candidates[: settings.marketplace_max_detail_pages]:
        detail_page = page.context.new_page()
        try:
            detail_page.goto(item_url, wait_until="domcontentloaded", timeout=30000)
            detail_page.wait_for_timeout(1200)
            detail = " ".join(detail_page.locator("body").inner_text(timeout=10000).split())
            photo_urls: list[str] = []
            for image in detail_page.locator("img").all():
                source = image.get_attribute("src")
                if source and source.startswith(("http://", "https://")) and source not in photo_urls:
                    photo_urls.append(source)
            photo_urls = photo_urls[:8]
            values = _prices(card_text)
            if not values or not photo_urls or REJECT_RE.search(f"{card_text} {detail}"):
                log.info("skipping rejected or unclear listing: %s", item_url)
                continue
            listings.append(
                Listing(
                    url=item_url,
                    title=card_text,
                    price=values[0],
                    location=location,
                    detail=detail[:12000],
                    photo_urls=photo_urls,
                )
            )
        except Exception as exc:
            log.warning("could not read listing %s: %s", item_url, exc)
        finally:
            detail_page.close()
    return listings


def scan_and_notify() -> dict:
    if not settings.marketplace_enabled:
        return {"enabled": False, "scanned": 0, "new": 0, "notified": 0}
    if not settings.slack_bot_token or not settings.slack_channel_id:
        raise RuntimeError("Marketplace scanning requires Slack bot token and channel ID")

    seen = _load_seen()
    new_listings: list[Listing] = []
    locations = [item.strip() for item in settings.marketplace_locations.split(",") if item.strip()]
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            settings.marketplace_browser_profile,
            headless=True,
            executable_path=_browser_path(),
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for location in locations:
                try:
                    for listing in _scan_location(page, location):
                        if listing.url not in seen:
                            new_listings.append(listing)
                except Exception as exc:
                    log.exception("Marketplace scan failed for %s: %s", location, exc)
        finally:
            context.close()

    notified = 0
    for listing in new_listings:
        listing_data = asdict(listing)
        text_decision = review_listing(listing_data, photo_urls=[])
        if text_decision.decision == "SKIP":
            seen.add(listing.url)
            continue

        # Inspect photos incrementally. Stop as soon as one image supports the
        # Juniper refresh; there is no reason to send the remaining photos.
        decision = None
        for photo_url in listing.photo_urls:
            candidate = review_listing(listing_data, photo_urls=[photo_url])
            if candidate.juniper_visual_match in {"confirmed", "likely"}:
                decision = candidate
                break
        if decision is None:
            log.info(
                "skipping listing without Juniper photo support: %s",
                listing.url,
            )
            seen.add(listing.url)
            continue
        decision = Review(
            text_decision.decision,
            decision.reason,
            decision.verify,
            decision.juniper_visual_match,
        )
        if decision.decision == "SKIP":
            seen.add(listing.url)
            continue
        status = {
            "RECOMMENDED_CONTACT": ("✅", "Recommended contact"),
            "POSSIBLE_CONTACT": ("👀", "Worth a closer look"),
        }.get(decision.decision, ("🚗", "Marketplace listing"))
        message = (
            f"{status[0]} *Marketplace lead · {status[1]}*\n"
            f"────────────────────\n"
            f"*{listing.title}*\n\n"
            f"💰 *Price:* ${listing.price:,}\n"
            f"📍 *Search area:* {_location_label(listing.location)}\n\n"
            f"📷 *Juniper photo check:* {decision.juniper_visual_match}\n"
            f"🤖 *Why:* {decision.reason}\n"
            f"🔎 *Verify:* {decision.verify}\n\n"
            f"<https://www.facebook.com/marketplace/item/{listing.url.rstrip('/').split('/')[-1]}|Open listing>"
        )
        if notify(message, channel=settings.slack_channel_id):
            seen.add(listing.url)
            notified += 1
    _save_seen(seen)
    return {"enabled": True, "scanned": len(locations), "new": len(new_listings), "notified": notified}


def run_once() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Marketplace scan result: %s", scan_and_notify())
