"""Hourly Milpitas single-family home listing monitor."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from playwright.sync_api import sync_playwright

from butler.config import settings
from butler.slack import notify

log = logging.getLogger(__name__)
RENTCAST_URL = "https://api.rentcast.io/v1/listings/sale"


class _GoogleLinkParser(HTMLParser):
    """Collect links from the Google results page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


@dataclass
class Home:
    url: str
    address: str
    price: int
    beds: int | None
    baths: float | None
    sqft: int | None
    lot_sqft: int | None
    year: int | None


def _state_path() -> Path:
    path = Path(settings.home_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state() -> dict[str, dict]:
    try:
        return json.loads(_state_path().read_text()) if _state_path().exists() else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, dict]) -> None:
    _state_path().write_text(json.dumps(state, indent=2) + "\n")


def _usage_path() -> Path:
    path = Path(settings.home_rentcast_usage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_usage() -> dict[str, int | str]:
    period = datetime.now(UTC).strftime("%Y-%m")
    try:
        usage = json.loads(_usage_path().read_text()) if _usage_path().exists() else {}
    except (OSError, ValueError):
        usage = {}
    if usage.get("period") != period:
        return {"period": period, "successful_requests": 0}
    return usage


def _save_usage(usage: dict[str, int | str]) -> None:
    _usage_path().write_text(json.dumps(usage, indent=2) + "\n")


def _score(home: Home) -> int:
    score = 50  # detached Milpitas house and 5,000+ sqft lot are prerequisites
    score += 20 if home.price <= settings.home_target_price else 12 if home.price <= settings.home_check_max_price else 0
    score += 10 if home.lot_sqft and home.lot_sqft >= 7000 else 6
    score += 8 if home.beds and home.beds >= 3 else 0
    score += 7 if home.baths and home.baths >= 2 else 0
    score += 5 if home.year and 1960 <= home.year <= 1999 else 2
    return min(score, 100)


def _parse_record(record: dict) -> Home:
    address = record.get("formattedAddress") or record.get("addressLine1", "Unknown address")
    url = "https://www.google.com/search?q=" + address.replace(" ", "+")
    return Home(
        url=url,
        address=address,
        price=int(record["price"]),
        beds=record.get("bedrooms"),
        baths=record.get("bathrooms"),
        sqft=record.get("squareFootage"),
        lot_sqft=record.get("lotSize"),
        year=record.get("yearBuilt"),
    )


def _redfin_link_from_google(address: str) -> str | None:
    """Search Google and return the first direct Redfin result URL."""
    search_url = "https://www.google.com/search?q=" + quote(f"site:redfin.com {address}")

    if settings.serpapi_api_key:
        try:
            response = httpx.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google",
                    "q": f"site:redfin.com {address}",
                    "api_key": settings.serpapi_api_key,
                    "num": 10,
                },
                timeout=20,
            )
            response.raise_for_status()
            for result in response.json().get("organic_results", []):
                candidate = result.get("link", "")
                parsed = urlparse(candidate)
                hostname = (parsed.hostname or "").lower()
                if hostname == "redfin.com" or hostname.endswith(".redfin.com"):
                    return candidate
            log.info("No direct Redfin result found in SerpApi results for %s", address)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.warning("SerpApi Redfin lookup failed for %s: %s", address, exc)
        return None

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                settings.home_browser_profile,
                headless=True,
                executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                locale="en-US",
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            hrefs = page.locator("a[href]").evaluate_all(
                "anchors => anchors.map(anchor => anchor.href)"
            )
            context.close()
        parser = _GoogleLinkParser()
        parser.links = hrefs
    except Exception as exc:
        log.warning("Google Redfin lookup failed for %s: %s", address, exc)
        return None

    for href in parser.links:
        candidate = href
        if href.startswith("/url?"):
            query = parse_qs(urlparse(href).query)
            candidate = next((query.get(key, [""])[0] for key in ("q", "url") if query.get(key)), "")
        candidate = unquote(candidate)
        parsed = urlparse(candidate)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme in {"http", "https"}
            and (hostname == "redfin.com" or hostname.endswith(".redfin.com"))
        ):
            return candidate
    log.info("No direct Redfin result found in Google results for %s", address)
    return None


def _scan() -> list[Home]:
    usage = _load_usage()
    if int(usage.get("successful_requests", 0)) >= settings.home_rentcast_max_requests:
        raise RuntimeError(
            "RentCast safety limit reached: "
            f"{usage['successful_requests']}/{settings.home_rentcast_max_requests} "
            f"successful requests in {usage['period']}"
        )
    response = httpx.get(
        RENTCAST_URL,
        params={
            "city": "Milpitas",
            "state": "CA",
            "propertyType": "Single Family",
            "status": "Active",
            "price": f"*:{settings.home_check_max_price}",
            "lotSize": f"{settings.home_min_lot_sqft}:*",
            "limit": 500,
        },
        headers={"X-Api-Key": settings.rentcast_api_key or "", "Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    usage["successful_requests"] = int(usage.get("successful_requests", 0)) + 1
    _save_usage(usage)
    return [_parse_record(record) for record in response.json()]


def _message(home: Home, previous: dict | None) -> str:
    score = _score(home)
    change = "New listing"
    if previous and home.price != previous.get("price"):
        change = f"Price changed: ${previous['price']:,} → ${home.price:,}"
    action = "Consider offer" if score >= 90 else "Tour" if score >= 80 else "Watch"
    zillow_url = f"https://www.zillow.com/homes/{quote(home.address, safe='')}_rb/"
    redfin_url = _redfin_link_from_google(home.address)
    redfin_label = "Redfin" if redfin_url else "Redfin via Google"
    redfin_url = redfin_url or f"https://www.google.com/search?q={quote('site:redfin.com ' + home.address)}"
    return (
        f"🏠 *Milpitas home alert · {change}*\n"
        f"*{home.address}*\n"
        f"List: ${home.price:,} · Score: {score}/100 · Action: *{action}*\n"
        f"{home.beds or '?'} bd · {home.baths or '?'} ba · {home.sqft or '?'} sqft · "
        f"{home.lot_sqft or '?'} sqft lot · Built {home.year or '?'}\n"
        "Why: detached home with a 5,000+ sqft lot in Milpitas.\n"
        "Concern: school assignment, comps, and property condition require verification.\n"
        f"<{zillow_url}|Zillow>"
        f" · <{redfin_url}|{redfin_label}>"
        f" · <{home.url}|Google search>"
    )


def scan_and_notify() -> dict:
    if not settings.home_enabled:
        return {"enabled": False, "scanned": 0, "notified": 0}
    if not settings.slack_bot_token or not settings.home_slack_channel_id:
        raise RuntimeError("Home watching requires Slack bot token and home channel ID")
    if not settings.rentcast_api_key:
        raise RuntimeError("Home watching requires a RentCast API key")
    usage = _load_usage()
    if int(usage.get("successful_requests", 0)) >= settings.home_rentcast_max_requests:
        log.warning(
            "Skipping home scan: RentCast safety limit reached (%s/%s)",
            usage["successful_requests"],
            settings.home_rentcast_max_requests,
        )
        return {
            "enabled": True,
            "scanned": 0,
            "notified": 0,
            "rentcast_requests": usage["successful_requests"],
            "rentcast_limit_reached": True,
        }
    state = _load_state()
    homes = _scan()
    notified = 0
    for home in homes:
        old = state.get(home.url)
        score = _score(home)
        meaningful = old is None or home.price != old.get("price")
        if meaningful and home.price <= settings.home_alert_max_price and score >= 80 and notify(
            _message(home, old),
            channel=settings.home_slack_channel_id,
            unfurl_links=False,
            unfurl_media=False,
        ):
            notified += 1
        state[home.url] = {"price": home.price, "score": score, "address": home.address}
    _save_state(state)
    return {"enabled": True, "scanned": len(homes), "notified": notified}


def run_once() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Home watch result: %s", scan_and_notify())
