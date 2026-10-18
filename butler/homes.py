"""Hourly Milpitas single-family home listing monitor."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from playwright.sync_api import sync_playwright

from butler.config import settings
from butler.llm import explain_home_listing
from butler.slack import notify
from butler.whatsapp import notify as whatsapp_notify

log = logging.getLogger(__name__)
RENTCAST_URL = "https://api.rentcast.io/v1/listings/sale"
RENTCAST_AVM_URL = "https://api.rentcast.io/v1/avm/value"
MUSD_SCHOOL_FINDER_URL = "https://www.schfinder.com/MilpitasUSD/"


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


class _MetaDescriptionParser(HTMLParser):
    """Extract the public listing summary from a listing page."""

    def __init__(self) -> None:
        super().__init__()
        self.description: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = dict(attrs)
        if attributes.get("property", "").lower() == "og:description":
            self.description = attributes.get("content")


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


@dataclass
class ValueEstimate:
    price: int
    low: int
    high: int


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


def _estimate_value(home: Home) -> ValueEstimate | None:
    """Return RentCast's current AVM estimate and range for a listing."""
    if not settings.rentcast_api_key:
        return None
    params: dict[str, int | str] = {
        "address": home.address,
        "propertyType": "Single Family",
        "compCount": 15,
        "maxRadius": 2,
        "daysOld": 365,
    }
    if home.beds is not None:
        params["bedrooms"] = home.beds
    if home.baths is not None:
        params["bathrooms"] = home.baths
    if home.sqft is not None:
        params["squareFootage"] = home.sqft
    try:
        response = httpx.get(
            RENTCAST_AVM_URL,
            params=params,
            headers={"X-Api-Key": settings.rentcast_api_key, "Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return ValueEstimate(
            price=int(data["price"]),
            low=int(data["priceRangeLow"]),
            high=int(data["priceRangeHigh"]),
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        log.warning("Home value estimate failed for %s: %s", home.address, exc)
        return None


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


def _lookup_elementary_school(address: str) -> str | None:
    """Look up the resident elementary school through MUSD's SchoolFinder."""
    match = re.match(r"\s*\d+\s+(.+?),\s*Milpitas\b", address, flags=re.IGNORECASE)
    if not match:
        return None
    street_number = address.split(" ", 1)[0]
    street_name = match.group(1).strip().upper()
    lookup_address = f"{street_number} {street_name}, MILPITAS"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            )
            context = browser.new_context(locale="en-US")
            try:
                page = context.new_page()
                page.goto(MUSD_SCHOOL_FINDER_URL, wait_until="domcontentloaded", timeout=30000)
                page.locator(
                    "#ctl00_ctl00_ASPxSplitter1_Content_MainContent_ASPxSplitter2_"
                    "ASPxCallbackPanel2_ASPxPanel1_ASPxTextBox1_I"
                ).fill(address.split(" ", 1)[0])
                street = page.locator(
                    "#ctl00_ctl00_ASPxSplitter1_Content_MainContent_ASPxSplitter2_"
                    "ASPxCallbackPanel2_ASPxPanel1_ASPxComboBox1_I"
                )
                street.fill(street_name)
                page.wait_for_timeout(800)
                street.press("ArrowDown")
                street.press("Enter")
                page.wait_for_timeout(500)
                page.locator("span.dx-vam", has_text="Submit").click(force=True)
                page.wait_for_function(
                    f"document.body.innerText.includes({lookup_address!r})",
                    timeout=10000,
                )
                body_text = page.locator("body").inner_text()
            finally:
                context.close()
                browser.close()
        match = re.search(
            r"Lookup Address.*?\n\s*Elementary\s*\n\s*([^\n]+)",
            body_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        school = match.group(1).strip()
        return None if school.lower() == "no data to display" else school
    except Exception as exc:
        log.warning("MUSD elementary school lookup failed for %s: %s", address, exc)
        return None


def _listing_description_from_google(address: str) -> str | None:
    """Find and read a public listing description for an address."""
    if not settings.serpapi_api_key:
        return None
    try:
        response = httpx.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google",
                "q": f'"{address}" real estate listing',
                "api_key": settings.serpapi_api_key,
                "num": 10,
            },
            timeout=20,
        )
        response.raise_for_status()
        for result in response.json().get("organic_results", []):
            link = result.get("link")
            if not link or not link.startswith(("http://", "https://")):
                continue
            try:
                page = httpx.get(
                    link,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=8,
                    follow_redirects=True,
                )
            except httpx.HTTPError:
                continue
            if page.status_code >= 400:
                continue
            parser = _MetaDescriptionParser()
            parser.feed(page.text)
            description = (parser.description or "").strip()
            if len(description) >= 80:
                return description[:4000]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("Listing description lookup failed for %s: %s", address, exc)
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
    listing_description = _listing_description_from_google(home.address)
    value_estimate = _estimate_value(home)
    value_line = ""
    if value_estimate:
        gap = value_estimate.price - home.price
        if gap > 0:
            comparison = f"List is ${gap:,} below estimate"
        elif gap < 0:
            comparison = f"List is ${abs(gap):,} above estimate"
        else:
            comparison = "List equals estimate"
        value_line = (
            f"• *AVM estimate:* ${value_estimate.price:,}\n"
            f"• *Estimated range:* ${value_estimate.low:,}–${value_estimate.high:,}\n"
            f"• *List vs. estimate:* {comparison}\n"
        )
    narrative = explain_home_listing(
        {
            "city": "Milpitas",
            "property_type": "single-family",
            "price": home.price,
            "target_price": settings.home_target_price,
            "beds": home.beds,
            "baths": home.baths,
            "sqft": home.sqft,
            "lot_sqft": home.lot_sqft,
            "year": home.year,
            "listing_description": listing_description,
        },
        score,
    )
    why = narrative.why if narrative else "detached home with a 5,000+ sqft lot in Milpitas."
    concern_line = (
        f"\n⚠️ *Watch-out*\n{narrative.concern}\n"
        if narrative and narrative.concern
        else ""
    )
    elementary_school = _lookup_elementary_school(home.address) or "Not verified"
    return (
        f"🏠 *MILPITAS HOME ALERT* · {change}\n"
        f"*{home.address}*\n"
        "────────────────────\n"
        "💰 *Pricing*\n"
        f"• *List:* ${home.price:,}\n"
        f"• *Score:* {score}/100 · *Action:* {action}\n"
        + value_line
        + "\n🏡 *Property*\n"
        + f"• {home.beds or '?'} bd · {home.baths or '?'} ba · {home.sqft or '?'} sqft\n"
        + f"• {home.lot_sqft or '?'} sqft lot · Built {home.year or '?'}\n"
        + "\n✨ *Why it stands out*\n"
        + f"{why}\n"
        + f"\n🏫 *Assigned elementary:* {elementary_school}\n"
        + concern_line
        + f"\n🔗 *Sources:* <{zillow_url}|Zillow>"
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
        if meaningful and home.price <= settings.home_alert_max_price and score >= 80:
            message = _message(home, old)
            if notify(
                message,
                channel=settings.home_slack_channel_id,
                unfurl_links=False,
                unfurl_media=False,
            ):
                notified += 1
            whatsapp_notify(message)
        state[home.url] = {"price": home.price, "score": score, "address": home.address}
    _save_state(state)
    return {"enabled": True, "scanned": len(homes), "notified": notified}


def run_once() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Home watch result: %s", scan_and_notify())
