from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from butler.auth import require_token
from butler.runs import create_run, get_run, list_runs, update_run
from butler.tasks.registry import get_policy, get_task, list_tasks

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_token)])


class RunTaskRequest(BaseModel):
    payload: dict = {}


@router.get("")
async def list_available_tasks() -> dict:
    return {
        "tasks": list_tasks(),
        "policies": {name: get_policy(name) for name in list_tasks()},
    }


async def _execute(run_id: str, name: str, payload: dict) -> dict:
    fn = get_task(name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"no such task: {name}")
    update_run(run_id, status="running")
    try:
        result = await fn(payload)
    except Exception as exc:
        update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    update_run(run_id, status="succeeded", result=result)
    return {"run_id": run_id, "status": "succeeded", "result": result}


@router.post("/{name}")
async def run_task(name: str, request: RunTaskRequest) -> dict:
    fn = get_task(name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"no such task: {name}")
    run_id = str(uuid4())
    policy = get_policy(name)
    create_run(run_id, name, policy, request.payload, "pending" if policy == "approval_required" else "created")
    if policy == "approval_required":
        return {"run_id": run_id, "status": "approval_required", "task": name}
    return await _execute(run_id, name, request.payload)


@router.get("/runs")
async def recent_runs(limit: int = 50) -> dict:
    return {"runs": list_runs(limit)}


@router.get("/runs/{run_id}")
async def run_status(run_id: str) -> dict:
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: str) -> dict:
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"run is {run['status']}, not pending")
    return await _execute(run_id, run["task"], run["payload"])
