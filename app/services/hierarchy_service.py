"""
Service to parse and evaluate employee management hierarchy.

Sources:
1. VM_EmployeeMaster.ManagerChain: Comma-separated list of employee codes representing the
   hierarchy chain from right to left in increasing hierarchy order.
2. Fallback: ManagerEC / ReportingManagerEmployeeCode in VM_EmployeeMaster traversing upward.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.api import table_api

logger = logging.getLogger("fms_portal.hierarchy")

_CACHE_LOCK = asyncio.Lock()
_CACHE_TIMESTAMP = 0.0
_CACHE_TTL_SECONDS = 60.0

# Mapping: normalized_doer_code -> set of normalized manager employee codes
_MANAGER_CHAINS_CACHE: dict[str, set[str]] = {}
_NAME_TO_EC_CACHE: dict[str, str] = {}


def _norm_ec(code: Any) -> str:
    """Normalize an employee code for robust comparison (e.g. '0210' -> '0210' & handles string/digit)."""
    if code is None:
        return ""
    val = str(code).strip()
    if not val or val.upper() in ("NONE", "NULL", "-", ""):
        return ""
    return val.upper()


def _code_variants(code: Any) -> set[str]:
    """Return both exact string and stripped-zero variants of an employee code."""
    val = _norm_ec(code)
    if not val:
        return set()
    variants = {val}
    if val.isdigit():
        variants.add(str(int(val)))
        variants.add(val.zfill(4))
    return variants


def parse_manager_chain_string(raw: Any) -> list[str]:
    """Parse comma-separated employee codes from ManagerChain column."""
    if not raw:
        return []
    text = str(raw).strip()
    if not text or text.upper() in ("NONE", "NULL", "-"):
        return []
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return parts


def _pick_case_insensitive(row: dict[str, Any], *candidates: str) -> Any:
    for cand in candidates:
        norm_cand = "".join(ch for ch in cand.lower() if ch.isalnum())
        for k, v in row.items():
            norm_k = "".join(ch for ch in str(k).lower() if ch.isalnum())
            if norm_k == norm_cand and v not in (None, ""):
                return v
    return None


async def get_hierarchy_map() -> dict[str, set[str]]:
    """Return cached dictionary of normalized employee_code -> set of manager codes."""
    global _CACHE_TIMESTAMP, _MANAGER_CHAINS_CACHE, _NAME_TO_EC_CACHE
    now = time.monotonic()
    if _MANAGER_CHAINS_CACHE and (now - _CACHE_TIMESTAMP) < _CACHE_TTL_SECONDS:
        return _MANAGER_CHAINS_CACHE

    async with _CACHE_LOCK:
        if _MANAGER_CHAINS_CACHE and (time.monotonic() - _CACHE_TIMESTAMP) < _CACHE_TTL_SECONDS:
            return _MANAGER_CHAINS_CACHE

        try:
            rows = await table_api.get_table("VM_EmployeeMaster")
        except Exception as e:
            logger.warning(f"Failed to load VM_EmployeeMaster for hierarchy: {e}")
            return _MANAGER_CHAINS_CACHE

        new_map: dict[str, set[str]] = {}
        new_name_map: dict[str, str] = {}
        parent_map: dict[str, str] = {}

        for row in rows or []:
            if not isinstance(row, dict):
                continue
            ec = _pick_case_insensitive(row, "EmployeeCode", "Employee_Code", "employeecode", "EmpCode", "code")
            norm_ec = _norm_ec(ec)
            if not norm_ec:
                continue

            emp_name = _pick_case_insensitive(row, "EmployeeName", "Employee_Name", "Name", "empname", "emp_name")
            if emp_name:
                name_str = str(emp_name).strip()
                if name_str:
                    new_name_map[name_str.lower()] = norm_ec
                    clean_name = "".join(ch for ch in name_str.lower() if ch.isalnum())
                    if clean_name:
                        new_name_map[clean_name] = norm_ec

            # Check ManagerChain column
            chain_raw = _pick_case_insensitive(
                row,
                "ManagerChain",
                "managerchain",
                "Manager_Chain",
                "MANAGER_CHAIN",
                "Hierarchy",
                "hierarchy",
            )
            parsed_chain = parse_manager_chain_string(chain_raw)
            if parsed_chain:
                mgr_set = set()
                for m in parsed_chain:
                    mgr_set.update(_code_variants(m))
                new_map[norm_ec] = mgr_set
            else:
                new_map[norm_ec] = set()

            # Record immediate manager for upward tree fallback
            mgr_ec = _pick_case_insensitive(
                row,
                "ManagerEC",
                "Manager_EC",
                "ManagerCode",
                "Manager_Code",
                "ReportingManagerEmployeeCode",
                "SupervisorEmployeeCode",
            )
            if mgr_ec:
                parent_map[norm_ec] = _norm_ec(mgr_ec)

        # Fallback: if ManagerChain is empty, traverse upward using parent_map
        for emp, mgrs in new_map.items():
            if not mgrs and emp in parent_map:
                curr = parent_map[emp]
                seen = {emp}
                while curr and curr not in seen:
                    mgrs.update(_code_variants(curr))
                    seen.add(curr)
                    curr = parent_map.get(curr, "")

        _MANAGER_CHAINS_CACHE = new_map
        _NAME_TO_EC_CACHE = new_name_map
        _CACHE_TIMESTAMP = time.monotonic()
        return _MANAGER_CHAINS_CACHE


def resolve_employee_code(identifier: Any) -> str:
    """Resolve an employee code from either an employee code or an employee name."""
    if identifier is None:
        return ""
    val = str(identifier).strip()
    if not val or val.upper() in ("NONE", "NULL", "-", ""):
        return ""
    norm = _norm_ec(val)
    if norm in _MANAGER_CHAINS_CACHE or norm.isdigit():
        return norm
    lowered = val.lower()
    if lowered in _NAME_TO_EC_CACHE:
        return _NAME_TO_EC_CACHE[lowered]
    clean = "".join(ch for ch in lowered if ch.isalnum())
    if clean in _NAME_TO_EC_CACHE:
        return _NAME_TO_EC_CACHE[clean]
    return norm


def is_user_in_hierarchy_sync(user_ec: str, doer_identifier: str) -> bool:
    """Synchronous check if user_ec is in the hierarchy of doer_identifier using the cache."""
    if not user_ec or not doer_identifier:
        return False

    clean_user = str(user_ec).strip()
    clean_doer = str(doer_identifier).strip()

    if clean_doer.upper() == "ALL":
        return True

    # Direct name/string equality check
    if clean_user.lower() == clean_doer.lower():
        return True
    u_clean = "".join(ch for ch in clean_user.lower() if ch.isalnum())
    d_clean = "".join(ch for ch in clean_doer.lower() if ch.isalnum())
    if u_clean and u_clean == d_clean:
        return True

    resolved_user_ec = resolve_employee_code(user_ec)
    resolved_doer_ec = resolve_employee_code(doer_identifier)

    user_vars = _code_variants(resolved_user_ec) or _code_variants(user_ec)
    doer_vars = _code_variants(resolved_doer_ec) or _code_variants(doer_identifier)

    if not user_vars or not doer_vars:
        return False
    if bool(user_vars & doer_vars):
        return True
    if any(dv.upper() == "ALL" for dv in doer_vars):
        return True

    for dv in doer_vars:
        managers = _MANAGER_CHAINS_CACHE.get(dv, set())
        if bool(user_vars & managers):
            return True
    return False


async def is_user_in_hierarchy(user_ec: str, doer_identifier: str) -> bool:
    """Check if user_ec is in the hierarchy of doer_identifier (loading cache if needed)."""
    if not user_ec or not doer_identifier:
        return False

    clean_user = str(user_ec).strip()
    clean_doer = str(doer_identifier).strip()

    if clean_doer.upper() == "ALL":
        return True

    if clean_user.lower() == clean_doer.lower():
        return True
    u_clean = "".join(ch for ch in clean_user.lower() if ch.isalnum())
    d_clean = "".join(ch for ch in clean_doer.lower() if ch.isalnum())
    if u_clean and u_clean == d_clean:
        return True

    await get_hierarchy_map()
    return is_user_in_hierarchy_sync(user_ec, doer_identifier)
