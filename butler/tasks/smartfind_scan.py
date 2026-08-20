from butler.smartfind import scan_once
from butler.tasks.registry import task


@task("smartfind_scan")
async def smartfind_scan(payload: dict) -> dict:
    return scan_once()
