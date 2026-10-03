"""
Stage-level FMS endpoints (API_2).
"""
from __future__ import annotations

from typing import Any

from app.api.client import api_client
from app.api.table_api import as_dict


async def get_stage(task_id: str, stage_no: str) -> dict[str, Any] | None:
    """GET /api/fms/task/{taskId}/stage/{stageNo}."""
    data = await api_client.get("get_stage", path_params={"task_id": task_id, "stage_no": stage_no})
    return as_dict(data)
