"""Flight price watching via the Duffel API.

Watches a list of one-way legs (see data/flight-routes.json, or
flight-routes.example.json for the format) rather than a single
origin/destination pair, since a multi-city itinerary is a set of
independent one-way searches, not one round trip.

Requires a *live* Duffel API key (duffel_live_...) — a test-mode key only
returns simulated fares from Duffel's fake "Duffel Airways", which is
useless for tracking real prices. See NOTES.md for the tradeoff (a live
key needs a payment method on file with Duffel).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path

import httpx

from butler.config import settings
from butler.slack import notify

log = logging.getLogger(__name__)

DUFFEL_VERSION = "v2"


@dataclass
class Route:
    origin: str
    destination: str
    date: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    max_price: float | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.date and not (self.date_start and self.date_end):
            raise ValueError(f"route {self.origin}->{self.destination} needs date, or date_start+date_end")

    @property
    def candidate_dates(self) -> list[str]:
        if self.date:
            return [self.date]
        start = date_cls.fromisoformat(self.date_start)
        end = date_cls.fromisoformat(self.date_end)
        span = (end - start).days
        return [(start + timedelta(days=offset)).isoformat() for offset in range(span + 1)]

    @property
    def key(self) -> str:
        window = self.date or f"{self.date_start}_{self.date_end}"
        return f"{self.origin}-{self.destination}-{window}"

    @property
    def display(self) -> str:
        return self.label or f"{self.origin} → {self.destination}"


@dataclass
class FlightOffer:
    price: float
    currency: str
    airline: str
    stops: int
    duration: str
    date: str
    booking_url: str


def _headers() -> dict[str, str]:
    if not settings.duffel_api_key:
        raise RuntimeError("Duffel API key is required for flight watching")
    return {
        "Authorization": f"Bearer {settings.duffel_api_key}",
        "Duffel-Version": DUFFEL_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _load_routes() -> list[Route]:
    path = Path(settings.flight_routes_path)
    if not path.exists():
        raise RuntimeError(
            f"no flight routes configured — create {path} (see flight-routes.example.json)"
        )
    data = json.loads(path.read_text())
    return [
        Route(
            origin=item["origin"],
            destination=item["destination"],
            date=item.get("date"),
            date_start=item.get("date_start"),
            date_end=item.get("date_end"),
            max_price=item.get("max_price"),
            label=item.get("label"),
        )
        for item in data
    ]


def _state_path() -> Path:
    path = Path(settings.flight_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_best_prices() -> dict[str, float]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.warning("could not read flight watch state: %s", exc)
        return {}


def _save_best_prices(best_prices: dict[str, float]) -> None:
    _state_path().write_text(json.dumps(best_prices, indent=2) + "\n")


def _booking_url(route: Route, date: str) -> str:
    return (
        "https://www.google.com/travel/flights?q=flights%20from%20"
        f"{route.origin}%20to%20{route.destination}%20on%20{date}"
    )


def _search_offers(route: Route, date: str) -> list[FlightOffer]:
    body = {
        "data": {
            "slices": [{"origin": route.origin, "destination": route.destination, "departure_date": date}],
            "passengers": [{"type": "adult"} for _ in range(settings.flight_adults)],
            "cabin_class": "economy",
        }
    }
    response = httpx.post(
        f"{settings.duffel_api_base}/air/offer_requests",
        json=body,
        headers=_headers(),
        timeout=30,
    )
    response.raise_for_status()
    raw_offers = response.json()["data"].get("offers", [])
    offers = []
    for item in raw_offers:
        slice0 = item["slices"][0]
        segments = slice0["segments"]
        owner = item.get("owner") or {}
        offers.append(
            FlightOffer(
                price=float(item["total_amount"]),
                currency=item["total_currency"],
                airline=owner.get("iata_code") or owner.get("name") or "?",
                stops=len(segments) - 1,
                duration=slice0.get("duration", ""),
                date=date,
                booking_url=_booking_url(route, date),
            )
        )
    return sorted(offers, key=lambda offer: offer.price)


def _best_offer_across_dates(route: Route) -> FlightOffer | None:
    best: FlightOffer | None = None
    for date in route.candidate_dates:
        try:
            offers = _search_offers(route, date)
        except Exception:
            log.exception("flight search failed for %s on %s", route.display, date)
            continue
        if offers and (best is None or offers[0].price < best.price):
            best = offers[0]
    return best


def _check_route(route: Route, best_prices: dict[str, float]) -> bool:
    best = _best_offer_across_dates(route)
    if best is None:
        return False

    previous_best = best_prices.get(route.key)
    threshold_ok = route.max_price is None or best.price <= route.max_price
    is_new_low = previous_best is None or best.price < previous_best

    if not (threshold_ok and is_new_low):
        return False

    date_line = f"🗓️ *Date:* {best.date}" + (" (flexible window)" if route.date_start else "")
    message = (
        f"✈️ *Flight price alert · {route.display}*\n"
        f"────────────────────\n"
        f"💰 *Price:* {best.currency} {best.price:,.2f}"
        f"{' (new low)' if previous_best else ''}\n"
        f"{date_line}\n"
        f"🛫 *Airline:* {best.airline} · {best.stops} stop(s) · {best.duration}\n\n"
        f"<{best.booking_url}|Search this route>"
    )
    if notify(message, channel=settings.flight_slack_channel_id or settings.slack_channel_id):
        best_prices[route.key] = best.price
        return True
    return False


def scan_and_notify() -> dict:
    if not settings.flight_enabled:
        return {"enabled": False, "checked": 0, "notified": 0}
    if not settings.slack_bot_token:
        raise RuntimeError("Flight watching requires a Slack bot token")

    routes = _load_routes()
    best_prices = _load_best_prices()

    notified = 0
    for route in routes:
        try:
            if _check_route(route, best_prices):
                notified += 1
        except Exception:
            log.exception("flight check failed for %s", route.display)

    _save_best_prices(best_prices)
    return {"enabled": True, "checked": len(routes), "notified": notified}


def run_once() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Flight watch result: %s", scan_and_notify())
