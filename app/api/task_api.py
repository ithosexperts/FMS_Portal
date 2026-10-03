"""
Task-level FMS endpoints (API_2) - dashboard listing + single task lookup.
"""
from __future__ import annotations

from typing import Any

from app.api.client import api_client
from app.api.table_api import as_dict, as_list


async def get_tasks(filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """GET /api/fms/tasks - raw task rows for the dashboard grid."""
    data = await api_client.get("get_tasks", query_params=filters)
    return as_list(data)


async def get_task(task_id: str) -> dict[str, Any] | None:
    """GET /api/fms/task/{taskId} - single task detail."""
    data = await api_client.get("get_task", path_params={"task_id": task_id})
    return as_dict(data)


async def save_task(payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST /api/fms/task/save - generic task/stage save used by Save & Submit."""
    data = await api_client.post("save_task", json_body=payload)
    return as_dict(data)
