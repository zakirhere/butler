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
import re
import time
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timezone
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
    flight_numbers: list[list[str]] | None = None

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
    route: str
    departure: str
    arrival: str


def _passengers() -> list[dict[str, int | str]]:
    passengers = [{"type": "adult"} for _ in range(settings.flight_adults)]
    for value in settings.flight_child_ages.split(","):
        value = value.strip()
        if value:
            passengers.append({"age": int(value)})
    return passengers


def _normalise_flight_number(value: str) -> str:
    match = re.fullmatch(r"([A-Za-z]+)0*(\d+)", value.strip())
    if not match:
        return value.strip().upper()
    return f"{match.group(1).upper()}{int(match.group(2))}"


def _offer_matches(route: Route, segments: list[dict]) -> bool:
    if not route.flight_numbers:
        return True
    if len(route.flight_numbers) != len(segments):
        return False
    for segment, accepted in zip(segments, route.flight_numbers):
        marketing = segment.get("marketing_carrier", {}).get("iata_code", "")
        operating = segment.get("operating_carrier", {}).get("iata_code", "")
        number = segment.get("marketing_carrier_flight_number", "")
        candidates = {
            _normalise_flight_number(f"{marketing}{number}"),
            _normalise_flight_number(f"{operating}{number}"),
        }
        if not candidates.intersection(_normalise_flight_number(item) for item in accepted):
            return False
    return True


def _format_duration(value: str) -> str:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", value or "")
    if not match:
        return value or "?"
    hours, minutes = match.groups(default="0")
    parts = []
    if int(hours):
        parts.append(f"{int(hours)}h")
    if int(minutes):
        parts.append(f"{int(minutes)}m")
    return " ".join(parts) or "0m"


def _format_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%-I:%M %p")


def _format_date(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%b %-d")


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
            flight_numbers=item.get("flight_numbers"),
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


def _history_path() -> Path:
    path = Path(settings.flight_history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_history() -> list[dict]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, list) else []
    except (OSError, ValueError) as exc:
        log.warning("could not read flight history: %s", exc)
        return []


def _record_attempt(
    history: list[dict], route: Route, offer: FlightOffer | None, error: str | None = None
) -> None:
    entry = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "route_key": route.key,
        "route": route.display,
        "date": route.date or ",".join(route.candidate_dates),
        "status": "ok" if offer is not None else ("error" if error else "unavailable"),
    }
    if offer is not None:
        entry.update(
            {
                "price": offer.price,
                "currency": offer.currency,
                "per_person": offer.price / len(_passengers()),
                "airline": offer.airline,
                "itinerary": offer.route,
            }
        )
    if error:
        entry["error"] = error[:300]
    history.append(entry)


def _save_history(history: list[dict]) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - (90 * 86400)
    retained = [
        item for item in history
        if datetime.fromisoformat(item["checked_at"]).timestamp() >= cutoff
    ]
    _history_path().write_text(json.dumps(retained, indent=2) + "\n")


def _booking_url(route: Route, date: str) -> str:
    return (
        "https://www.google.com/travel/flights?q=flights%20from%20"
        f"{route.origin}%20to%20{route.destination}%20on%20{date}"
    )


def _search_offers(route: Route, date: str) -> list[FlightOffer]:
    body = {
        "data": {
            "slices": [{"origin": route.origin, "destination": route.destination, "departure_date": date}],
            "passengers": _passengers(),
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
        if not _offer_matches(route, segments):
            continue
        owner = item.get("owner") or {}
        first_segment = segments[0]
        last_segment = segments[-1]
        route_codes = [first_segment["origin"]["iata_code"]]
        route_codes.extend(segment["destination"]["iata_code"] for segment in segments)
        offers.append(
            FlightOffer(
                price=float(item["total_amount"]),
                currency=item["total_currency"],
                airline=owner.get("iata_code") or owner.get("name") or "?",
                stops=len(segments) - 1,
                duration=_format_duration(slice0.get("duration", "")),
                date=date,
                booking_url=_booking_url(route, date),
                route=" → ".join(route_codes),
                departure=_format_time(first_segment["departing_at"]),
                arrival=_format_time(last_segment["arriving_at"]),
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


def _check_route(route: Route, best_prices: dict[str, float]) -> tuple[FlightOffer | None, bool]:
    best = _best_offer_across_dates(route)
    if best is None:
        return None, False

    previous_best = best_prices.get(route.key)
    per_person = best.price / len(_passengers())
    threshold_ok = route.max_price is None or per_person <= route.max_price
    is_new_low = previous_best is None or per_person < previous_best
    return best, threshold_ok and is_new_low


def _itinerary_message(
    offers: list[tuple[Route, FlightOffer]],
    changed: list[tuple[Route, FlightOffer]],
    routes: list[Route],
) -> str:
    passenger_count = len(_passengers())
    offers_by_key = {route.key: offer for route, offer in offers}
    baselines = _load_best_prices()
    total = sum(offer.price for _, offer in offers)
    fallback_total = sum(
        baselines[route.key] * passenger_count
        for route in routes
        if route.key not in offers_by_key and route.key in baselines
    )
    estimated_total = total + fallback_total
    baseline_total = (
        sum(baselines[route.key] * passenger_count for route in routes)
        if all(route.key in baselines for route in routes)
        else None
    )
    per_person = total / passenger_count
    lines = [
        "✈️ *Flight price update*",
        "",
        "```",
        "Trip                   | Date       | Route             | Total          | /person      | Baseline   | Difference    ",
        "───────────────────────+────────────+───────────────────+────────────────+──────────────+────────────+───────────────",
    ]
    for route in routes:
        offer = offers_by_key.get(route.key)
        baseline = baselines.get(route.key)
        if offer is None:
            total_text = f"USD {baseline * passenger_count:,.2f}" if baseline is not None else "—"
            per_person_text = f"USD {baseline:,.2f}" if baseline is not None else "—"
            baseline_text = f"${baseline:,.2f}" if baseline is not None else "—"
            lines.append(
                f"{route.display[:21]:<21} | {(route.date or '?'):<10} | {'⚠️ ESTIMATE':<17} | "
                f"{total_text:>14} | {per_person_text:>12} | {baseline_text:>10} | {'—':>13}"
            )
            continue
        current_per_person = offer.price / passenger_count
        savings = baseline - current_per_person if baseline is not None else None
        baseline_text = f"${baseline:,.2f}" if baseline is not None else "—"
        savings_text = (
            f"${savings:,.2f} less" if savings is not None and savings > 0
            else f"${abs(savings):,.2f} higher" if savings is not None and savings < 0
            else "—"
        )
        lines.append(
            f"{route.display[:21]:<21} | {_format_date(offer.date):<10} | "
            f"{(offer.airline + ' ' + offer.route.replace(' → ', '-'))[:17]:<17} | "
            f"{offer.currency} {offer.price:>9,.2f} | {offer.currency} {current_per_person:>7,.2f} | "
            f"{baseline_text:>10} | {savings_text:>13}"
        )
    total_savings = baseline_total - estimated_total if baseline_total is not None else None
    savings_line = (
        f"*Savings vs baseline:* ✅ {offers[0][1].currency} {total_savings:,.2f} less"
        if total_savings is not None and total_savings > 0
        else f"*Savings vs baseline:* {offers[0][1].currency} {abs(total_savings):,.2f} higher"
        if total_savings is not None and total_savings < 0
        else "*Savings vs baseline:* unavailable"
    )
    lines.extend(
        [
            "```",
            f"*Estimated family total:* {offers[0][1].currency} {estimated_total:,.2f}",
            f"*Current baseline total:* {offers[0][1].currency} {baseline_total:,.2f}" if baseline_total is not None else "*Current baseline total:* unavailable",
            savings_line,
            f"*(Includes ⚠️ estimated, not-live values for unavailable flights; live fare total: {offers[0][1].currency} {total:,.2f})*",
            f"*Average per person ({passenger_count} travelers):* {offers[0][1].currency} {per_person:,.2f}",
            "",
            "*New low on:* " + ", ".join(route.display for route, _ in changed),
        ]
    )
    return "\n".join(lines)


def _route_message(route: Route, offer: FlightOffer) -> str:
    return (
        f"✈️ *New flight price low · {route.display}*\n"
        f"{_format_date(offer.date)} · {offer.airline} · {offer.route}\n"
        f"{offer.departure}–{offer.arrival} · {offer.duration} · "
        f"{offer.stops} stop(s) · {offer.currency} {offer.price / len(_passengers()):,.0f}/person\n"
        f"{offer.booking_url}"
    )


def scan_route_and_notify(route_index: int) -> dict:
    if not settings.flight_enabled:
        return {"enabled": False, "checked": 0, "notified": 0}
    if not settings.slack_bot_token:
        raise RuntimeError("Flight watching requires a Slack bot token")

    routes = _load_routes()
    if route_index < 0 or route_index >= len(routes):
        raise ValueError(f"flight route index {route_index} is outside 0..{len(routes) - 1}")
    route = routes[route_index]
    best_prices = _load_best_prices()
    history = _load_history()
    try:
        best, should_alert = _check_route(route, best_prices)
    except Exception:
        log.exception("flight check failed for %s", route.display)
        _record_attempt(history, route, None, "check failed; see watcher log")
        _save_history(history)
        return {"enabled": True, "checked": 1, "notified": 0, "route": route.display}
    _record_attempt(history, route, best)

    notified = 0
    if best is not None and should_alert:
        if notify(_route_message(route, best), channel=settings.flight_slack_channel_id or settings.slack_channel_id):
            best_prices[route.key] = best.price / len(_passengers())
            notified = 1
    _save_best_prices(best_prices)
    _save_history(history)
    return {"enabled": True, "checked": 1, "notified": notified, "route": route.display}


def scan_and_notify() -> dict:
    if not settings.flight_enabled:
        return {"enabled": False, "checked": 0, "notified": 0}
    if not settings.slack_bot_token:
        raise RuntimeError("Flight watching requires a Slack bot token")

    routes = _load_routes()
    best_prices = _load_best_prices()
    history = _load_history()

    best_offers: list[tuple[Route, FlightOffer]] = []
    changed: list[tuple[Route, FlightOffer]] = []
    for route in routes:
        try:
            best, should_alert = _check_route(route, best_prices)
            if best is not None:
                best_offers.append((route, best))
            if best is not None and should_alert:
                changed.append((route, best))
            _record_attempt(history, route, best)
        except Exception:
            log.exception("flight check failed for %s", route.display)
            _record_attempt(history, route, None, "check failed; see watcher log")
        if route is not routes[-1]:
            time.sleep(settings.flight_delay_seconds)

    notified = 0
    # A consolidated itinerary is only useful when every configured leg has a
    # current offer. Duffel can temporarily rate-limit a date search; in that
    # case wait for the next scheduled run instead of posting a partial trip.
    if changed and best_offers:
        message = _itinerary_message(best_offers, changed, routes)
        if notify(message, channel=settings.flight_slack_channel_id or settings.slack_channel_id):
            for route, best in changed:
                best_prices[route.key] = best.price / len(_passengers())
            notified = len(changed)
    _save_best_prices(best_prices)
    _save_history(history)
    return {"enabled": True, "checked": len(routes), "notified": notified}


def _summary_state_path() -> Path:
    path = Path(settings.flight_summary_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _summary_already_sent(window_key: str) -> bool:
    path = _summary_state_path()
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text()).get("window_key") == window_key
    except (OSError, ValueError):
        return False


def send_weekly_summary(*, force: bool = False) -> dict:
    if not settings.flight_enabled:
        return {"enabled": False, "sent": 0}
    if not settings.slack_bot_token:
        raise RuntimeError("Flight watching requires a Slack bot token")

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=7)
    window_key = window_start.date().isoformat()
    if not force and _summary_already_sent(window_key):
        return {"enabled": True, "sent": 0, "reason": "already sent"}

    history = [
        item for item in _load_history()
        if datetime.fromisoformat(item["checked_at"]) >= window_start
    ]
    if not history:
        return {"enabled": True, "sent": 0, "reason": "no checks in reporting window"}
    routes = _load_routes()
    baselines = _load_best_prices()
    lines = [
        "✈️ *Weekly flight-watcher summary*",
        f"{window_start.astimezone().strftime('%b %-d')} – {now.astimezone().strftime('%b %-d, %Y')}",
        "",
        "```",
        "Flight                 Checks  Fares  Unavail  Errors  Low/person  Last/person  Baseline",
        "──────────────────────────────────────────────────────────────────────────────────────",
    ]
    total_fares = 0
    weekly_low_total = 0.0
    baseline_total = sum(baselines.values()) * len(_passengers())
    for route in routes:
        entries = [item for item in history if item["route_key"] == route.key]
        fares = [item for item in entries if item.get("status") == "ok"]
        prices = [float(item["per_person"]) for item in fares]
        total_fares += len(fares)
        low = f"${min(prices):,.2f}" if prices else "—"
        latest = max(fares, key=lambda item: item["checked_at"]) if fares else None
        last = f"${float(latest['per_person']):,.2f}" if latest else "—"
        baseline = baselines.get(route.key)
        if prices:
            weekly_low_total += min(prices) * len(_passengers())
        elif baseline is not None:
            weekly_low_total += baseline * len(_passengers())
        base = (
            f"~${baseline:,.2f} EST"
            if baseline is not None and route.key == "SFO-NRT-2026-12-11"
            else f"${baseline:,.2f}" if baseline is not None else "unset"
        )
        lines.append(
            f"{route.display[:22]:<22} {len(entries):>6} {len(fares):>6} "
            f"{sum(item.get('status') == 'unavailable' for item in entries):>8} "
            f"{sum(item.get('status') == 'error' for item in entries):>7} {low:>11} {last:>12} {base:>9}"
        )
    lines.extend(
        [
            "```",
            f"*Total checks:* {len(history)} · *fare results:* {total_fares}",
            f"*Estimated family total using weekly lows:* USD {weekly_low_total:,.2f}",
            f"*Current baseline total:* USD {baseline_total:,.2f}",
            "(SFO → Tokyo uses the ⚠️ estimate—not a live fare.)",
            "Each check is one provider request for one configured flight.",
        ]
    )
    if not notify("\n".join(lines), channel=settings.flight_slack_channel_id or settings.slack_channel_id):
        return {"enabled": True, "sent": 0, "reason": "Slack notification failed"}
    _summary_state_path().write_text(json.dumps({"window_key": window_key}, indent=2) + "\n")
    return {"enabled": True, "sent": 1, "checks": len(history)}


def run_once() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if settings.flight_route_index is None:
        result = scan_and_notify()
    else:
        result = scan_route_and_notify(settings.flight_route_index)
    log.info("Flight watch result: %s", result)
