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


def review_listing(listing: dict) -> Review:
    api_key = settings.openai_api_key
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for Marketplace alerts")
    prompt = (
        "Review this Facebook Marketplace listing for a buyer. Recommend contact only "
        "if it appears to be a genuine clean-title 2026 Tesla Model Y within budget. "
        "Reject salvage/rebuilt/flood/lemon/parts listings and obvious scams. Return "
        "JSON only with decision (RECOMMENDED_CONTACT, POSSIBLE_CONTACT, or SKIP), "
        "reason, and verify. Do not infer missing facts.\n\n"
        + json.dumps(listing, ensure_ascii=False)
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
            "input": prompt,
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
    return Review(decision, str(data.get("reason", "")), str(data.get("verify", "")))
