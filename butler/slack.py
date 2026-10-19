import logging

import httpx

from butler.config import settings

log = logging.getLogger(__name__)


def notify(
    text: str,
    *,
    channel: str | None = None,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
) -> bool:
    """Post a message to Slack. Prefers a bot token (chat.postMessage) so a
    channel can be targeted by ID; falls back to an incoming webhook."""
    target_channel = channel or settings.slack_channel_id
    if settings.slack_bot_token and target_channel:
        try:
            r = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                json={
                    "channel": target_channel,
                    "text": text,
                    "unfurl_links": unfurl_links,
                    "unfurl_media": unfurl_media,
                },
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error") or "Slack API returned ok=false")
            return True
        except Exception:
            log.exception("slack chat.postMessage failed")
            return False

    if settings.slack_webhook_url:
        try:
            r = httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=10.0)
            r.raise_for_status()
            return True
        except Exception:
            log.exception("slack webhook notification failed")
            return False

    log.debug("slack notification skipped: not configured")
    return False


def post_message(
    text: str,
    *,
    channel: str,
    thread_ts: str | None = None,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
) -> str | None:
    """Post with a bot token and return the Slack timestamp for thread tracking."""
    if not settings.slack_bot_token:
        log.warning("Slack message timestamp unavailable: bot token is not configured")
        return None
    try:
        payload = {
            "channel": channel,
            "text": text,
            "unfurl_links": unfurl_links,
            "unfurl_media": unfurl_media,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        response = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Slack API returned ok=false")
        return data.get("ts")
    except Exception:
        log.exception("slack chat.postMessage failed")
        return None
