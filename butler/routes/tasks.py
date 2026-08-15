from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from butler.auth import require_token
from butler.tasks.registry import get_task, list_tasks

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_token)])


class RunTaskRequest(BaseModel):
    payload: dict = {}


@router.get("")
async def list_available_tasks() -> dict:
    return {"tasks": list_tasks()}


@router.post("/{name}")
async def run_task(name: str, request: RunTaskRequest) -> dict:
    fn = get_task(name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"no such task: {name}")
    return await fn(request.payload)
