from butler.smartfind import scan_once
from butler.tasks.registry import task


@task("smartfind_scan", approval="approval_required")
async def smartfind_scan(payload: dict) -> dict:
    return scan_once()
