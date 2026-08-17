import asyncio

from butler.flights import scan_and_notify
from butler.tasks.registry import task


@task("flight_watch")
async def flight_watch(payload: dict) -> dict:
    return await asyncio.to_thread(scan_and_notify)
