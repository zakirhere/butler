from butler import whatsapp
from butler.tasks.registry import task


@task("whatsapp_test")
async def whatsapp_test(payload: dict) -> dict:
    """Send a test message through the local WhatsApp bridge."""
    text = payload.get("text", "butler: WhatsApp integration is working ✅")
    target = payload.get("target", "all")
    if target not in {"all", "recipient", "group"}:
        raise ValueError("target must be one of: all, recipient, group")
    sent = whatsapp.notify(text, target=target)
    return {"sent": sent, "target": target}
