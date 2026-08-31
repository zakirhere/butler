"""Small provider adapter for Marketplace listing judgment."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from butler.config import settings


@dataclass
class Review:
    decision: str
    reason: str
    verify: str
    juniper_visual_match: str


def review_listing(listing: dict, photo_urls: list[str] | None = None) -> Review:
    api_key = settings.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for Marketplace alerts")
    photo_urls = photo_urls if photo_urls is not None else listing.get("photo_urls", [])
    listing_for_prompt = dict(listing)
    listing_for_prompt.pop("photo_urls", None)
    prompt = (
        "Review this Facebook Marketplace listing for a buyer. First assess only the "
        "listing title, description, price, and location. Recommend contact only if "
        "those text fields support a genuine clean-title 2026 Tesla Model Y within "
        "budget. "
        + ("Then assess the supplied photo(s) for the refreshed Tesla Model Y ('Juniper') design. " if photo_urls else "No photos are supplied in this pass; set juniper_visual_match to inconclusive. ")
        + "Reject salvage/rebuilt/flood/lemon/parts listings and obvious scams. Return "
        "JSON only with decision (RECOMMENDED_CONTACT, POSSIBLE_CONTACT, or SKIP), "
        "reason, verify, and juniper_visual_match (confirmed, likely, not_juniper, "
        "or inconclusive). Use not_juniper when the photos show the pre-refresh "
        "front/rear design. Use inconclusive when there are no clear exterior photos, "
        "only stock/unrelated photos, or the images are inaccessible. Do not infer "
        "visual facts from the title, model year, VIN, or seller claims.\n\n"
        + json.dumps(listing_for_prompt, ensure_ascii=False)
    )
    content = [{"type": "input_text", "text": prompt}]
    content.extend(
        {"type": "input_image", "image_url": url, "detail": "high"}
        for url in photo_urls
    )
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.marketplace_llm_model,
            "instructions": "You are a cautious used-car listing reviewer. Return JSON only.",
            "input": [{"role": "user", "content": content}],
            "reasoning": {"effort": "none"},
            "text": {"format": {"type": "json_object"}, "verbosity": "low"},
            "max_output_tokens": 300,
            "store": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    content = body.get("output_text")
    if not content:
        for item in body.get("output") or []:
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        content = part.get("text")
                        break
    if not content:
        raise RuntimeError("OpenAI response did not contain output_text")
    data = json.loads(content)
    decision = data.get("decision", "SKIP")
    if decision not in {"RECOMMENDED_CONTACT", "POSSIBLE_CONTACT", "SKIP"}:
        decision = "SKIP"
    visual_match = str(data.get("juniper_visual_match", "inconclusive")).lower()
    if visual_match not in {"confirmed", "likely", "not_juniper", "inconclusive"}:
        visual_match = "inconclusive"
    return Review(
        decision,
        str(data.get("reason", "")),
        str(data.get("verify", "")),
        visual_match,
    )
