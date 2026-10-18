"""Persistent execution ledger for Butler task runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from butler.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = Path(settings.run_ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_runs (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            policy TEXT NOT NULL,
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    connection.commit()
    return connection


def create_run(run_id: str, task: str, policy: str, payload: dict[str, Any], status: str) -> dict:
    now = _now()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO task_runs (id, task, policy, status, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, task, policy, status, json.dumps(payload), now),
        )
    return get_run(run_id) or {}


def update_run(run_id: str, *, status: str, result: Any = None, error: str | None = None) -> None:
    now = _now()
    with _connect() as connection:
        if status == "running":
            connection.execute(
                "UPDATE task_runs SET status=?, started_at=? WHERE id=?",
                (status, now, run_id),
            )
        else:
            connection.execute(
                "UPDATE task_runs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                (status, json.dumps(result) if result is not None else None, error, now, run_id),
            )


def get_run(run_id: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    for field in ("payload", "result"):
        if item[field] is not None:
            try:
                item[field] = json.loads(item[field])
            except json.JSONDecodeError:
                pass
    return item


def list_runs(limit: int = 50) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM task_runs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
    result = []
    for row in rows:
        item = get_run(row[0])
        if item is not None:
            result.append(item)
    return result
