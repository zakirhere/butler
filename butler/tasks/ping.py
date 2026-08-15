from butler.tasks.registry import task


@task("ping")
async def ping(payload: dict) -> dict:
    """Trivial task that proves the phone-to-machine round trip works."""
    return {"pong": True, "echo": payload}
