from butler import slack
from butler.tasks.registry import task


@task("slack_test")
async def slack_test(payload: dict) -> dict:
    """Posts a test message to the configured Slack channel."""
    text = payload.get("text", "butler: slack integration is working \U0001F44B")
    sent = slack.notify(text)
    return {"sent": sent}
