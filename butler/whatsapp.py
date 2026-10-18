"""Client for the local unofficial WhatsApp Web bridge."""

from __future__ import annotations

import logging
import re

import httpx

from butler.config import settings

log = logging.getLogger(__name__)


def _whatsapp_text(text: str) -> str:
    """Convert Slack link markup while retaining WhatsApp-compatible styling."""
    return re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)


def notify(text: str, *, target: str = "all") -> bool:
    """Ask the local WhatsApp bridge to deliver text to configured targets."""
    if not settings.whatsapp_enabled:
        return False
    if not settings.whatsapp_bridge_token:
        log.warning("WhatsApp notification skipped: bridge token is not configured")
        return False
    try:
        response = httpx.post(
            f"http://127.0.0.1:{settings.whatsapp_bridge_port}/send",
            headers={"Authorization": f"Bearer {settings.whatsapp_bridge_token}"},
            json={"text": _whatsapp_text(text), "target": target},
            timeout=20.0,
        )
        response.raise_for_status()
        data = response.json()
        sent = int(data.get("sent", 0))
        skipped = int(data.get("skipped", 0))
        if sent == 0 and skipped == 0:
            log.warning("WhatsApp bridge accepted no deliveries")
            return False
        return sent > 0
    except Exception:
        log.exception("WhatsApp bridge notification failed")
        return False
