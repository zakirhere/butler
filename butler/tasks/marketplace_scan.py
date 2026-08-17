import asyncio

from butler.marketplace import scan_and_notify
from butler.tasks.registry import task


@task("marketplace_scan")
async def marketplace_scan(payload: dict) -> dict:
    return await asyncio.to_thread(scan_and_notify)
