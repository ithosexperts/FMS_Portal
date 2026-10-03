"""
Single source of truth mapping a *logical* operation to a base URL + path
template. Nothing else in the codebase should ever build a URL by hand.
"""

API_ENDPOINTS = {
    # ---- Generic metadata-driven table access (API_1) -------------------
    "get_metadata": {"base": "API_1", "method": "GET", "path": ""},
    "get_table": {"base": "API_1", "method": "GET", "path": ""},
    "get_row": {"base": "API_1", "method": "GET", "path": ""},
    "update_row": {"base": "API_1", "method": "POST", "path": ""},
    "search_table": {"base": "API_1", "method": "GET", "path": ""},
    "get_dependent_list": {"base": "API_1", "method": "GET", "path": ""},

    # ---- PHP SQL gateway style access (action/table query contract) -----
    "table_gateway": {"base": "API_1", "method": "GET", "path": ""},

    # ---- Configuration tables (still generic table access) --------------
    "get_application_master": {"base": "API_1", "method": "GET", "path": ""},
    "get_fms_config": {"base": "API_1", "method": "GET", "path": ""},

    # ---- FMS specific task/stage endpoints (API_2) -----------------------
    "get_tasks": {"base": "API_2", "method": "GET", "path": "/api/fms/tasks"},
    "get_task": {"base": "API_2", "method": "GET", "path": "/api/fms/task/{task_id}"},
    "get_stage": {"base": "API_2", "method": "GET", "path": "/api/fms/task/{task_id}/stage/{stage_no}"},
    "save_task": {"base": "API_1", "method": "POST", "path": "/api/fms/task/save"},

    # ---- Auth (placeholder for future role/permission plumbing) ---------
    "login": {"base": "API_2", "method": "POST", "path": "/api/auth/login"},
    "get_current_user": {"base": "API_2", "method": "GET", "path": "/api/auth/me"},

    # ---- Form Settings (API_1) -------------------
    "save_form_settings": {"base": "API_1", "method": "POST", "path": ""},


}