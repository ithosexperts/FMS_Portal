"""
Dashboard data assembly - combines FMS_Config (FMS_TYPE='F' only) with each
application's live data table to build the rows shown on the dashboard
grid. Current stage / status / delay are always computed, never read from
a stored column.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
import json
import time
import re
import logging
from typing import Any

from app.api import table_api
from app.api.client import api_client
from app.models.schemas import TaskSummary
from app.services import metadata_service
from app.utils.delay_calculator import calculate_delay_minutes, format_duration, parse_datetime
from app.utils.stage_calculator import get_current_stage, sorted_stages
from app.utils.status_calculator import build_timeline, calculate_task_status

DASHBOARD_FMS_TYPE = "F"

# Progressive dashboard page loading.  Only the first two API pages are
# awaited during a dashboard request.  After those two pages are available,
# the remaining pages are fetched in the background after a two-second delay.
_PROGRESSIVE_FIRST_PAGES = 2
_PROGRESSIVE_BACKGROUND_CONCURRENCY = 1
# When filters are active, correctness is more important than progressive
# loading: computed filters (status/doer) can only be applied after every
# source page is available. Fetch those pages with bounded concurrency so the
# result is filtered across the complete dataset, not just page 1/2.
_FILTERED_SCAN_CONCURRENCY = 6
# Filter dropdowns must represent the complete table, not only dashboard pages 1/2.
# Use a larger page size where supported by the gateway to reduce round-trips.
_FILTER_OPTIONS_PAGE_SIZE = 500
_FILTER_OPTIONS_CONCURRENCY = 8
_FILTER_OPTIONS_CACHE_TTL_SECONDS = 60.0
_FILTER_OPTIONS_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, list[str]]]] = {}
_SALES_ORDER_OPTIONS_CACHE: tuple[float, list[str]] | None = None
_SALES_ORDER_OPTIONS_CACHE_TTL_SECONDS = 60.0
_PROGRESSIVE_START_DELAY_SECONDS = 2.0
_PROGRESSIVE_CACHE_TTL_SECONDS = 300.0
_PAGE_CACHE: dict[str, dict[int, tuple[list[dict[str, Any]], int, int, float]]] = {}
_PAGE_LOAD_TASKS: dict[str, asyncio.Task] = {}


def _normalize_lookup_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    return True


def _pick_row_value(row: dict[str, Any], *aliases: str) -> tuple[str | None, Any]:
    lookup = {_normalize_lookup_key(k): (k, v) for k, v in row.items()}
    for alias in aliases:
        key = _normalize_lookup_key(alias)
        if key in lookup:
            actual_key, value = lookup[key]
            if _has_value(value):
                return actual_key, value
    return None, None


def _row_value(row: dict[str, Any], field_name: Any) -> Any:
    """Case-insensitive dictionary lookup."""
    if not field_name:
        return None
    name_str = str(field_name)
    if name_str in row:
        return row[name_str]
    lowered = name_str.lower()
    for key, value in row.items():
        if str(key).lower() == lowered:
            return value
    return None


def _value_matches_any(raw_value: Any, selected_values: list[str]) -> bool:
    if raw_value is None:
        return False

    if isinstance(raw_value, (list, tuple, set)):
        normalized_values = {str(item).strip().lower() for item in raw_value if item is not None}
        return any(v.strip().lower() in normalized_values for v in selected_values)

    normalized = str(raw_value).strip().lower()
    return any(normalized == v.strip().lower() for v in selected_values)


async def get_dashboard_fms_options() -> list[dict[str, Any]]:
    return await metadata_service.get_dashboard_fms_options()


async def get_doer_options(fms_names: list[str] | None = None) -> list[str]:
    config_rows = await metadata_service.get_fms_config(fms_type=DASHBOARD_FMS_TYPE)
    if fms_names:
        selected = {str(name).strip().lower() for name in fms_names if str(name).strip()}
        config_rows = [
            r for r in config_rows
            if str(r.get("FMS_NAME") or "").strip().lower() in selected
        ]
    doers = {row["DOER"] for row in config_rows if row.get("DOER")}
    return sorted(doers)


async def _fetch_dashboard_api_page(
    table_name: str,
    filters: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int, int]:
    """Fetch one API page from the PHP gateway without fetchAll=true.

    The gateway response is expected to expose `data`, `totalPages`, and
    `currentPage`.  The page size is intentionally fixed at 50 for dashboard
    table reads so the dashboard never requests the complete table at once.
    """
    page = max(int(page or 1), 1)
    page_size = max(int(page_size or 50), 1)

    base_params: dict[str, Any] = {
        "action": "select",
        "table": table_name,
        "page": page,
        "limit": page_size,
    }
    if filters:
        base_params.update(filters)

    # Primary contract: page + limit.
    candidates = [base_params]
    # Compatibility fallbacks for API builds that expose a different page key.
    candidates.append({**{k: v for k, v in base_params.items() if k != "page"}, "currentPage": page})
    candidates.append({**{k: v for k, v in base_params.items() if k != "page"}, "pageNo": page})
    candidates.append({**{k: v for k, v in base_params.items() if k not in {"page", "limit"}}, "page": page, "pageSize": page_size})

    last_exc: Exception | None = None
    payload: Any = None
    for candidate in candidates:
        try:
            payload = await api_client.get("table_gateway", query_params=candidate)
            break
        except Exception as exc:
            last_exc = exc
            continue

    if payload is None:
        if last_exc:
            raise last_exc
        return [], 1, 0

    rows: list[dict[str, Any]] = []
    total_pages = 1
    current_page = page

    if isinstance(payload, dict):
        raw_rows = payload.get("data")
        if raw_rows is None:
            for key in ("rows", "items", "records", "results", "value", "payload", "list"):
                if isinstance(payload.get(key), list):
                    raw_rows = payload.get(key)
                    break
        if isinstance(raw_rows, list):
            rows = [row for row in raw_rows if isinstance(row, dict)]

        try:
            total_pages = max(int(payload.get("totalPages") or 1), 1)
        except (TypeError, ValueError):
            total_pages = 1
        try:
            current_page = max(int(payload.get("currentPage") or page), 1)
        except (TypeError, ValueError):
            current_page = page

    elif isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
        current_page = page
        total_pages = 1 if len(rows) < page_size else page

    return rows, total_pages, current_page


def _progressive_cache_key(table_name: str, filters: dict[str, Any] | None, page_size: int) -> str:
    """Stable cache key for one table/query, independent of FMS stage configuration."""
    normalized_filters = filters or {}
    try:
        filter_text = json.dumps(normalized_filters, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError:
        filter_text = repr(sorted((str(k), repr(v)) for k, v in normalized_filters.items()))
    return f"{table_name}|{page_size}|{filter_text}"


def _cache_prune() -> None:
    """Drop expired page-cache entries to keep long-running workers bounded."""
    now = time.monotonic()
    empty_keys: list[str] = []
    for cache_key, pages in _PAGE_CACHE.items():
        expired_pages = [
            page_no
            for page_no, (_, _, _, cached_at) in pages.items()
            if now - cached_at > _PROGRESSIVE_CACHE_TTL_SECONDS
        ]
        for page_no in expired_pages:
            pages.pop(page_no, None)
        if not pages:
            empty_keys.append(cache_key)
    for cache_key in empty_keys:
        _PAGE_CACHE.pop(cache_key, None)


def _cache_get(cache_key: str, page: int) -> tuple[list[dict[str, Any]], int, int] | None:
    _cache_prune()
    entry = _PAGE_CACHE.get(cache_key, {}).get(page)
    if not entry:
        return None
    rows, total_pages, current_page, _ = entry
    return rows, total_pages, current_page


def _cache_put(
    cache_key: str,
    page: int,
    rows: list[dict[str, Any]],
    total_pages: int,
    current_page: int,
) -> None:
    _PAGE_CACHE.setdefault(cache_key, {})[page] = (
        rows,
        max(int(total_pages or 1), 1),
        max(int(current_page or page), 1),
        time.monotonic(),
    )


def _cancel_obsolete_page_loads(active_cache_keys: set[str]) -> None:
    """Stop stale progressive queues when the dashboard query changes.

    Only background prefetch tasks are cancelled; already-cached pages remain
    available, and the current request always fetches whatever five pages it needs.
    """
    for cache_key, task in list(_PAGE_LOAD_TASKS.items()):
        if cache_key in active_cache_keys or task.done():
            continue
        task.cancel()


def _start_background_page_load(
    cache_key: str,
    table_name: str,
    filters: dict[str, Any] | None,
    page_size: int,
    total_pages: int,
    start_page: int,
) -> None:
    """Schedule the remaining pages once per query key."""
    if start_page > total_pages or total_pages <= 0:
        return
    existing = _PAGE_LOAD_TASKS.get(cache_key)
    if existing and not existing.done():
        return

    async def _load_remaining() -> None:
        try:
            # Do not compete with the first two interactive pages.  Once the
            # dashboard has been visible for two seconds, load all remaining
            # pages in the background with bounded concurrency.
            await asyncio.sleep(_PROGRESSIVE_START_DELAY_SECONDS)

            semaphore = asyncio.Semaphore(_PROGRESSIVE_BACKGROUND_CONCURRENCY)

            async def _fetch_and_cache(page_no: int) -> None:
                if _cache_get(cache_key, page_no) is not None:
                    return
                async with semaphore:
                    try:
                        rows, page_total, page_current = await _fetch_dashboard_api_page(
                            table_name,
                            filters=filters,
                            page=page_no,
                            page_size=page_size,
                        )
                        _cache_put(cache_key, page_no, rows, page_total or total_pages, page_current or page_no)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # A background page must never break the visible dashboard.
                        import logging
                        logging.getLogger(__name__).warning(
                            "Progressive dashboard page %s failed for %s: %s",
                            page_no,
                            table_name,
                            exc,
                        )

            await asyncio.gather(
                *(_fetch_and_cache(page_no) for page_no in range(start_page, total_pages + 1))
            )
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            if _PAGE_LOAD_TASKS.get(cache_key) is current_task:
                _PAGE_LOAD_TASKS.pop(cache_key, None)

    _PAGE_LOAD_TASKS[cache_key] = asyncio.create_task(_load_remaining())


class DashboardRowsResult(list):
    """List-compatible dashboard result carrying backend pagination metadata."""

    def __init__(self, tasks=None):
        super().__init__(tasks or [])
        self.total_pages_by_fms: dict[str, int] = {}
        self.requested_page_tasks_by_fms: dict[str, list[TaskSummary]] = {}
        self.complete_scan: bool = False




async def _fetch_distinct_filter_values_all_pages(
    table_name: str,
    column_names: list[str],
) -> dict[str, list[str]]:
    """Return distinct values for dashboard filter columns from every backend page.

    The dashboard grid intentionally loads only a small number of pages first for
    responsiveness. Filter option lists are different: a Sales Order must be
    selectable even when its row lives on page 50, 100, or later. This helper
    therefore walks the complete source table and builds distinct values without
    constructing TaskSummary objects for every row.
    """
    cleaned_columns: list[str] = []
    seen_columns: set[str] = set()
    for column_name in column_names:
        name = str(column_name or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_columns:
            continue
        seen_columns.add(key)
        cleaned_columns.append(name)

    if not cleaned_columns:
        return {}

    cache_key = (str(table_name or "").strip().lower(), tuple(sorted(c.lower() for c in cleaned_columns)))
    cached = _FILTER_OPTIONS_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _FILTER_OPTIONS_CACHE_TTL_SECONDS:
        return {k: list(v) for k, v in cached[1].items()}

    first_rows, total_pages, _ = await _fetch_dashboard_api_page(
        table_name,
        filters=None,
        page=1,
        page_size=_FILTER_OPTIONS_PAGE_SIZE,
    )
    total_pages = max(int(total_pages or 1), 1)

    values_by_column: dict[str, dict[str, str]] = {
        column_name: {} for column_name in cleaned_columns
    }

    def collect(rows: list[dict[str, Any]]) -> None:
        for row in rows or []:
            for column_name in cleaned_columns:
                raw_value = _row_value(row, column_name)
                if raw_value is None:
                    continue
                value_text = str(raw_value).strip()
                if not value_text:
                    continue
                value_key = value_text.lower()
                if value_key not in values_by_column[column_name]:
                    values_by_column[column_name][value_key] = value_text

    collect(first_rows)

    if total_pages > 1:
        semaphore = asyncio.Semaphore(_FILTER_OPTIONS_CONCURRENCY)

        async def fetch_page(page_no: int) -> list[dict[str, Any]]:
            async with semaphore:
                rows, _total, _current = await _fetch_dashboard_api_page(
                    table_name,
                    filters=None,
                    page=page_no,
                    page_size=_FILTER_OPTIONS_PAGE_SIZE,
                )
                return rows or []

        # Fetch all remaining pages concurrently with a strict bound. The
        # result is held only long enough to extract the dropdown values.
        remaining = await asyncio.gather(
            *(fetch_page(page_no) for page_no in range(2, total_pages + 1)),
            return_exceptions=True,
        )
        logger = logging.getLogger("fms_portal.dashboard")
        for page_no, result in enumerate(remaining, start=2):
            if isinstance(result, Exception):
                logger.warning(
                    "Filter-options page %s failed for %s: %s",
                    page_no,
                    table_name,
                    result,
                )
                continue
            collect(result)

    result = {
        column_name: sorted(values.values(), key=lambda item: item.lower())
        for column_name, values in values_by_column.items()
    }
    _FILTER_OPTIONS_CACHE[cache_key] = (time.monotonic(), result)
    logging.getLogger("fms_portal.dashboard").info(
        "Loaded all %s backend pages for filter options: table=%s, columns=%s",
        total_pages,
        table_name,
        cleaned_columns,
    )
    return {k: list(v) for k, v in result.items()}


async def _fetch_all_sales_order_numbers() -> list[str]:
    """Load the COMPLETE Z_SalesOrder.I_DocNum master for the Sales Order filter.

    IMPORTANT: do not trust ``fetchAll=true`` here.  The gateway can return a
    successful response containing only its normal first page while still
    accepting the fetchAll flag.  The only reliable source of completeness is
    the gateway's page metadata, so this function always walks every backend
    page and merges I_DocNum values.
    """
    global _SALES_ORDER_OPTIONS_CACHE

    now = time.monotonic()
    if _SALES_ORDER_OPTIONS_CACHE is not None:
        cached_at, cached_values = _SALES_ORDER_OPTIONS_CACHE
        if now - cached_at < _SALES_ORDER_OPTIONS_CACHE_TTL_SECONDS:
            return list(cached_values)

    logger = logging.getLogger("fms_portal.dashboard")
    table_name = "Z_SalesOrder"
    page_size = _FILTER_OPTIONS_PAGE_SIZE
    values: dict[str, str] = {}

    def collect(rows: list[dict[str, Any]]) -> int:
        added = 0
        for row in rows or []:
            raw = _row_value(row, "I_DocNum")
            if raw is None:
                continue
            text_value = str(raw).strip()
            if not text_value:
                continue
            key = text_value.casefold()
            if key not in values:
                values[key] = text_value
                added += 1
        return added

    # Page 1 establishes the authoritative total-page count.
    first_rows, total_pages, first_current_page = await _fetch_dashboard_api_page(
        table_name,
        filters=None,
        page=1,
        page_size=page_size,
    )
    total_pages = max(int(total_pages or 1), 1)
    collect(first_rows)

    logger.info(
        "[Sales Order Master] page=1/%s rows=%s distinct_so=%s",
        total_pages,
        len(first_rows or []),
        len(values),
    )

    if total_pages > 1:
        semaphore = asyncio.Semaphore(_FILTER_OPTIONS_CONCURRENCY)

        async def fetch_page(page_no: int) -> tuple[int, list[dict[str, Any]], int, int]:
            async with semaphore:
                rows, page_total, current_page = await _fetch_dashboard_api_page(
                    table_name,
                    filters=None,
                    page=page_no,
                    page_size=page_size,
                )
                return page_no, rows or [], max(int(page_total or total_pages), total_pages), current_page

        results = await asyncio.gather(
            *(fetch_page(page_no) for page_no in range(2, total_pages + 1)),
            return_exceptions=True,
        )

        successful_pages = 1
        for result in results:
            if isinstance(result, Exception):
                logger.warning("[Sales Order Master] page request failed: %s", result)
                continue
            page_no, rows, reported_total, current_page = result
            # Defensive guard against a broken gateway returning page 1 for
            # every requested page.  We keep page 1 data but do not count a
            # duplicate response as proof that later data was loaded.
            collect(rows)
            successful_pages += 1
            logger.debug(
                "[Sales Order Master] page=%s/%s current=%s rows=%s distinct_so=%s",
                page_no,
                reported_total,
                current_page,
                len(rows),
                len(values),
            )

        logger.info(
            "[Sales Order Master] completed pages=%s/%s distinct_so=%s",
            successful_pages,
            total_pages,
            len(values),
        )

    result = sorted(
        values.values(),
        key=lambda item: (0, int(item)) if item.isdigit() else (1, item.casefold()),
    )
    _SALES_ORDER_OPTIONS_CACHE = (time.monotonic(), result)
    logger.info(
        "[Sales Order Master] FINAL complete Z_SalesOrder.I_DocNum count=%s",
        len(result),
    )
    return list(result)


async def get_dashboard_filter_options_all_pages(
    fms_options: list[dict[str, Any]],
    filter_columns: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Build generic filter dropdown options from *all* source-table pages.

    Every configured filter is treated identically: resolve the FMS table, walk
    every backend page for that table, collect distinct values for the requested
    columns, and return those values for the UI. This deliberately does not use
    the dashboard's currently visible page.
    """
    if not fms_options or not filter_columns:
        return []

    selected_tables = {
        str(option.get("table_name") or "").strip()
        for option in fms_options
        if str(option.get("table_name") or "").strip()
    }

    # Fetch every requested dimension from every selected source table.
    by_table: dict[str, dict[str, list[str]]] = {}
    columns = [
        str(item.get("column_name") or "").strip()
        for item in filter_columns
        if str(item.get("column_name") or "").strip()
    ]

    if selected_tables and columns:
        table_tasks = {
            table_name: asyncio.create_task(
                _fetch_distinct_filter_values_all_pages(table_name, columns)
            )
            for table_name in sorted(selected_tables, key=str.lower)
        }
        if table_tasks:
            table_results = await asyncio.gather(
                *table_tasks.values(),
                return_exceptions=True,
            )
            for table_name, result in zip(table_tasks.keys(), table_results):
                if isinstance(result, Exception):
                    logging.getLogger("fms_portal.dashboard").warning(
                        "Could not load complete filter options for %s: %s",
                        table_name,
                        result,
                    )
                    by_table[table_name] = {}
                else:
                    by_table[table_name] = result

    # Merge values across selected FMS tables for the generic top-level result.
    # Section-level code narrows this back to its own source table below.
    union_values: dict[str, dict[str, str]] = {}
    for column_name in columns:
        union_values.setdefault(column_name, {})
    for table_values in by_table.values():
        for column_name, values in table_values.items():
            target = union_values.setdefault(column_name, {})
            for value in values:
                value_text = str(value).strip()
                key = value_text.casefold()
                if key and key not in target:
                    target[key] = value_text

    output: list[dict[str, Any]] = []
    for filter_def in filter_columns:
        column_name = str(filter_def.get("column_name") or "").strip()
        label = str(filter_def.get("label") or column_name)
        if not column_name:
            continue
        options = sorted(
            union_values.get(column_name, {}).values(),
            key=lambda item: item.casefold(),
        )
        output.append({
            "column_name": column_name,
            "label": label,
            "options": options,
        })

    return output

async def _fetch_all_dashboard_pages(
    table_name: str,
    filters: dict[str, Any] | None,
    page_size: int,
) -> list[dict[str, Any]]:
    """Fetch every backend page for a filtered dashboard scan.

    The first request establishes totalPages. Remaining pages are fetched in
    parallel with a small semaphore so a filter never silently sees only the
    first couple of pages.
    """
    first_rows, total_pages, _ = await _fetch_dashboard_api_page(
        table_name, filters=filters, page=1, page_size=page_size
    )
    total_pages = max(int(total_pages or 1), 1)
    if total_pages <= 1:
        return list(first_rows or [])

    semaphore = asyncio.Semaphore(_FILTERED_SCAN_CONCURRENCY)

    async def fetch_page(page_no: int) -> list[dict[str, Any]]:
        async with semaphore:
            rows, _total, _current = await _fetch_dashboard_api_page(
                table_name, filters=filters, page=page_no, page_size=page_size
            )
            return rows or []

    remaining = await asyncio.gather(
        *(fetch_page(page_no) for page_no in range(2, total_pages + 1)),
        return_exceptions=True,
    )

    all_rows = list(first_rows or [])
    for page_no, result in enumerate(remaining, start=2):
        if isinstance(result, Exception):
            import logging
            logging.getLogger(__name__).warning(
                "Filtered dashboard page %s failed for %s: %s",
                page_no, table_name, result,
            )
            continue
        all_rows.extend(result)
    return all_rows


async def get_dashboard_rows(
    fms_names: list[str] | None = None,
    status: str | None = None,
    doers: list[str] | None = None,
    search_text: str | None = None,
    fms_column_filters: dict[str, list[str]] | None = None,
    include_cached_pages: bool = False,
    requested_page: int = 1,
    force_full_scan: bool = False,
) -> DashboardRowsResult:
    """Load dashboard rows with progressive mode or a complete filtered scan."""
    fms_options = await get_dashboard_fms_options()
    if fms_names:
        selected = {str(name).strip().lower() for name in fms_names if str(name).strip()}
        fms_options = [
            f for f in fms_options
            if str(f.get("fms_name") or "").strip().lower() in selected
        ]

    all_config_rows = await metadata_service.get_fms_config(fms_type=DASHBOARD_FMS_TYPE)
    config_by_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_config_rows:
        config_by_app[row.get("APP_ID")].append(row)

    result = DashboardRowsResult()
    tasks: list[TaskSummary] = []
    dashboard_page_size = 50
    initial_pages = _PROGRESSIVE_FIRST_PAGES
    requested_page = max(int(requested_page or 1), 1)

    query_specs: list[tuple[dict[str, Any], str, list[dict[str, Any]], str, dict[str, Any]]] = []
    active_cache_keys: set[str] = set()

    for fms in fms_options:
        table_name = fms.get("table_name")
        stage_configs = config_by_app.get(fms["app_id"], [])
        if not table_name or not stage_configs:
            continue

        primary_key = await metadata_service.resolve_primary_key(table_name)
        api_filters: dict[str, Any] = {}

        if search_text:
            api_filters[primary_key] = search_text

        if fms_column_filters:
            for col_name, selected_values in fms_column_filters.items():
                active_vals = [v for v in selected_values if str(v).strip()]
                if active_vals:
                    api_filters[col_name] = active_vals

        cache_key = _progressive_cache_key(table_name, api_filters, dashboard_page_size)
        active_cache_keys.add(cache_key)
        query_specs.append((fms, table_name, stage_configs, primary_key, api_filters))

    _cancel_obsolete_page_loads(active_cache_keys)

    # ------------------------------------------------------------------
    # COMPLETE FILTERED SCAN
    # ------------------------------------------------------------------
    # A status/doer/search/column filter must be applied against the complete
    # source table. The previous progressive flow loaded only the first two
    # pages before applying those filters, so matches on page 3+ disappeared.
    if force_full_scan:
        result.complete_scan = True
        for fms, table_name, stage_configs, primary_key, api_filters in query_specs:
            scan_filters = dict(api_filters)
            # Search/column filter arrays are applied locally below. Sending
            # them to every gateway implementation is unsafe because some
            # deployments treat repeated values differently.
            if search_text:
                scan_filters.pop(primary_key, None)
            if fms_column_filters:
                for col_name in fms_column_filters:
                    scan_filters.pop(col_name, None)

            rows = await _fetch_all_dashboard_pages(
                table_name, filters=scan_filters, page_size=dashboard_page_size
            )

            seen_row_ids: set[str] = set()
            for row in rows:
                row_key, row_value = _pick_row_value(
                    row, primary_key, "RowID", "row_id", "TaskID", "task_id", "DocEntry", "ID"
                )
                identity = f"{row_key}:{row_value}" if row_value is not None else repr(sorted(row.items()))
                if identity in seen_row_ids:
                    continue
                seen_row_ids.add(identity)
                tasks.append(_build_task_summary(fms, stage_configs, row, primary_key))

            result.total_pages_by_fms[str(fms.get("fms_name") or "").strip().lower()] = 1

        # Apply computed/raw filters only after ALL rows are present.
        tasks = [
            task for task in tasks
            if any(_has_value(stage.get("planned")) for stage in (task.timeline or []))
        ]
        if status:
            tasks = [t for t in tasks if t.status.lower() == status.lower()]
        if doers:
            selected_doers = {str(d).strip().lower() for d in doers if str(d).strip()}
            tasks = [t for t in tasks if (t.assigned_doer or "").strip().lower() in selected_doers]
        if search_text:
            needle = str(search_text).strip().lower()
            if needle:
                tasks = [
                    t for t in tasks
                    if needle == str(t.row_id_value or "").strip().lower()
                    or needle == str(t.task_id or "").strip().lower()
                ]
        if fms_column_filters:
            for column_name, selected_values in fms_column_filters.items():
                active_values = [str(v).strip() for v in selected_values if str(v).strip()]
                if not active_values:
                    continue
                tasks = [
                    t for t in tasks
                    if _value_matches_any(_row_value(t.raw, column_name), active_values)
                ]

        def _row_sort_value(task: TaskSummary) -> tuple[int, str]:
            raw = str(task.row_id_value or task.task_id or "").strip()
            try:
                return (1, f"{int(raw):020d}")
            except (TypeError, ValueError):
                return (0, raw.lower())

        tasks.sort(key=_row_sort_value, reverse=True)

        # Build correct filtered pagination per FMS. This is what keeps the
        # dashboard pager aligned with the filtered result set rather than
        # the unfiltered SQL/API page numbers. IMPORTANT: this metadata must
        # also be populated for the complete-scan path. Previously that path
        # left requested_page_tasks_by_fms empty, so a valid filter (such as
        # Sales Order 126363) existed in the dropdown but rendered no task.
        by_fms: dict[str, list[TaskSummary]] = defaultdict(list)
        for task in tasks:
            by_fms[str(task.application_name or "").strip().lower()].append(task)

        for fms, _table_name, _stage_configs, _primary_key, _api_filters in query_specs:
            fms_key = str(fms.get("fms_name") or "").strip().lower()
            fms_tasks = by_fms.get(fms_key, [])
            total_pages = max(
                (len(fms_tasks) + dashboard_page_size - 1) // dashboard_page_size,
                1,
            )
            result.total_pages_by_fms[fms_key] = total_pages
            requested = min(requested_page, total_pages)
            start = (requested - 1) * dashboard_page_size
            result.requested_page_tasks_by_fms[fms_key] = fms_tasks[
                start:start + dashboard_page_size
            ]

        result.extend(tasks)
        return result

    for fms, table_name, stage_configs, primary_key, api_filters in query_specs:
        cache_key = _progressive_cache_key(table_name, api_filters, dashboard_page_size)
        page_results: list[tuple[int, list[dict[str, Any]], int, int]] = []
        total_pages = 1

        # Only the first two pages are interactive-blocking.
        for page_no in range(1, initial_pages + 1):
            cached = _cache_get(cache_key, page_no)
            if cached is None:
                rows, page_total, page_current = await _fetch_dashboard_api_page(
                    table_name,
                    filters=api_filters,
                    page=page_no,
                    page_size=dashboard_page_size,
                )
                _cache_put(cache_key, page_no, rows, page_total, page_current)
                cached = (rows, page_total, page_current)

            rows, page_total, page_current = cached
            total_pages = max(total_pages, int(page_total or 1))
            page_results.append((page_no, rows, page_total, page_current))
            if not rows or page_no >= total_pages:
                break

        if not page_results:
            continue

        # The UI may request a page beyond the first two while the backend
        # is still warming the cache. Fetch that requested page directly so
        # navigation never depends on the background worker having reached it.
        requested_rows: list[dict[str, Any]] | None = None
        if requested_page <= initial_pages:
            for page_no, rows, _page_total, _page_current in page_results:
                if page_no == requested_page:
                    requested_rows = rows
                    break
        elif requested_page <= total_pages:
            cached_requested = _cache_get(cache_key, requested_page)
            if cached_requested is None:
                req_rows, req_total, req_current = await _fetch_dashboard_api_page(
                    table_name,
                    filters=api_filters,
                    page=requested_page,
                    page_size=dashboard_page_size,
                )
                _cache_put(cache_key, requested_page, req_rows, req_total, req_current)
                requested_rows = req_rows
                total_pages = max(total_pages, int(req_total or 1))
            else:
                requested_rows, req_total, req_current = cached_requested
                total_pages = max(total_pages, int(req_total or 1))
            page_results.append((requested_page, requested_rows or [], total_pages, requested_page))

        result.total_pages_by_fms[str(fms.get("fms_name") or "").strip().lower()] = max(total_pages, 1)

        # Remaining pages are loaded in the background after a two-second delay.
        _start_background_page_load(
            cache_key,
            table_name,
            api_filters,
            dashboard_page_size,
            total_pages,
            min(initial_pages + 1, total_pages + 1),
        )

        # On a normal dashboard/filter request, render only the first two pages.
        # On a progressive refresh, render every contiguous page already cached,
        # so the UI can grow while the backend finishes the remaining pages.
        if include_cached_pages:
            cached_results: list[tuple[int, list[dict[str, Any]], int, int]] = []
            for page_no in range(1, total_pages + 1):
                cached = _cache_get(cache_key, page_no)
                if cached is None:
                    break
                rows, page_total, page_current = cached
                cached_results.append((page_no, rows, page_total, page_current))
            if cached_results:
                page_results = cached_results

        seen_row_ids: set[str] = set()
        seen_page_signatures: set[tuple[str, ...]] = set()
        requested_task_object_ids: set[int] = set()

        for page_no, rows, page_total, page_current in page_results:
            if not rows:
                continue

            page_signature = tuple(
                str(_pick_row_value(row, primary_key, "RowID", "row_id", "TaskID", "task_id")[1] or "")
                for row in rows
            )
            if page_signature in seen_page_signatures:
                continue
            seen_page_signatures.add(page_signature)

            for row in rows:
                row_key, row_value = _pick_row_value(
                    row, primary_key, "RowID", "row_id", "TaskID", "task_id", "DocEntry", "ID"
                )
                identity = f"{row_key}:{row_value}" if row_value is not None else repr(sorted(row.items()))
                if identity in seen_row_ids:
                    continue
                seen_row_ids.add(identity)
                task_summary = _build_task_summary(fms, stage_configs, row, primary_key)
                tasks.append(task_summary)
                if page_no == requested_page:
                    requested_task_object_ids.add(id(task_summary))

        result.requested_page_tasks_by_fms[str(fms.get("fms_name") or "").strip().lower()] = [
            task for task in tasks if id(task) in requested_task_object_ids
        ]

    tasks = [
        task for task in tasks
        if any(
            _has_value(stage.get("planned"))
            for stage in (task.timeline or [])
        )
    ]

    if status:
        tasks = [t for t in tasks if t.status.lower() == status.lower()]
    if doers:
        selected_doers = {str(d).strip().lower() for d in doers if str(d).strip()}
        tasks = [t for t in tasks if (t.assigned_doer or "").lower() in selected_doers]

    if search_text:
        needle = str(search_text).strip().lower()
        if needle:
            tasks = [
                t for t in tasks
                if needle == str(t.row_id_value or "").strip().lower()
                or needle == str(t.task_id or "").strip().lower()
            ]

    allowed_task_ids = {id(task) for task in tasks}
    for fms_key, requested_tasks in list(result.requested_page_tasks_by_fms.items()):
        result.requested_page_tasks_by_fms[fms_key] = [
            task for task in requested_tasks if id(task) in allowed_task_ids
        ]

    def _row_sort_value(task: TaskSummary) -> tuple[int, str]:
        raw = str(task.row_id_value or task.task_id or "").strip()
        try:
            return (1, f"{int(raw):020d}")
        except (TypeError, ValueError):
            return (0, raw.lower())

    tasks.sort(key=_row_sort_value, reverse=True)
    for fms_key, requested_tasks in list(result.requested_page_tasks_by_fms.items()):
        requested_tasks.sort(key=_row_sort_value, reverse=True)
        result.requested_page_tasks_by_fms[fms_key] = requested_tasks
    result.extend(tasks)
    return result


def is_progressive_dashboard_loading() -> bool:
    """Return whether any dashboard page queues are still warming in this worker."""
    _cache_prune()
    return any(not task.done() for task in _PAGE_LOAD_TASKS.values())


def build_fms_filter_options(
    tasks: list[TaskSummary],
    filter_columns: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Build distinct dropdown values for metadata-driven dashboard filters."""
    options: list[dict[str, Any]] = []

    for filter_column in filter_columns:
        column_name = str(filter_column.get("column_name") or "").strip()
        if not column_name:
            continue

        values: dict[str, str] = {}
        for task in tasks:
            value = _row_value(task.raw, column_name)
            if value is None:
                continue
            value_str = str(value).strip()
            if not value_str:
                continue
            value_key = value_str.lower()
            if value_key not in values:
                values[value_key] = value_str

        options.append({
            "column_name": column_name,
            "label": str(filter_column.get("label") or column_name),
            "options": sorted(values.values(), key=lambda item: item.lower()),
        })

    return options


async def get_stage_headers(fms_name: str | None = None) -> list[dict[str, Any]]:
    """Ordered stage list for dashboard matrix headers."""
    config_rows = await metadata_service.get_fms_config(fms_type=DASHBOARD_FMS_TYPE)
    if fms_name:
        config_rows = [
            r for r in config_rows
            if str(r.get("FMS_NAME") or "").strip().lower() == fms_name.strip().lower()
        ]
    stages: dict[str, dict[str, Any]] = {}
    for row in sorted_stages(config_rows):
        stage_no = row.get("STAGE_NO")
        if stage_no and stage_no not in stages:
            stages[stage_no] = {
                "stage_no": stage_no,
                "stage_name": row.get("STAGE_NAME") or stage_no,
                "display_order": row.get("DISPLAY_ORDER") or 0,
            }
    return list(stages.values())


def _build_task_summary(
    fms: dict[str, Any],
    stage_configs: list[dict[str, Any]],
    row: dict[str, Any],
    primary_key: str,
) -> TaskSummary:
    current = get_current_stage(stage_configs, row)
    status = calculate_task_status(stage_configs, row)
    stages = sorted_stages(stage_configs)
    timeline = build_timeline(stage_configs, row)
    has_any_planned = any(_has_value(stage.get("planned")) for stage in timeline)
    lifecycle_status = "Close" if timeline and timeline[-1].get("status") == "Completed" else ("Open" if has_any_planned else "-")

    if current:
        stage_no, stage_name, doer = current.get("STAGE_NO"), current.get("STAGE_NAME"), current.get("DOER")
        delay = calculate_delay_minutes(_row_value(row, current.get("PLANNED_COLUMN")), _row_value(row, current.get("ACTUAL_COLUMN")))
    elif stages and has_any_planned:
        if status == "Completed":
            # Do not point to the configured last stage merely because the
            # status calculator found the planned stages completed. In a
            # progressive FMS, later stages may still be unplanned. Show the
            # last stage that actually contains an Actual value instead.
            reference = next(
                (stage for stage in reversed(stages)
                 if _has_value(_row_value(row, stage.get("ACTUAL_COLUMN")))),
                stages[0],
            )
        else:
            # When there is no active planned+actual gap yet, point to the
            # first configured stage instead of jumping to the final stage.
            reference = stages[0]
        stage_no, stage_name, doer = reference.get("STAGE_NO"), reference.get("STAGE_NAME"), reference.get("DOER")
        delay = 0
    else:
        stage_no = stage_name = doer = None
        delay = 0

    row_key, row_value = _pick_row_value(
        row,
        primary_key,
        "RowID",
        "row_id",
        "PK",
        "TaskID",
        "task_id",
        "Task Id",
        "DocEntry",
        "ID",
    )
    row_id_value = str(row_value).strip() if _has_value(row_value) else ""
    primary_key_label = row_key or primary_key or "Row ID"
    elapsed = format_duration(_elapsed_minutes(stages, row))
    return TaskSummary(
        task_id=row_id_value,
        app_id=str(fms["app_id"]),
        application_name=fms.get("fms_name") or str(fms["app_id"]),
        table_name=fms.get("table_name"),
        primary_key_name=primary_key_label,
        row_id_value=row_id_value,
        current_stage_no=stage_no,
        current_stage_name=stage_name,
        assigned_doer=doer,
        elapsed_display=elapsed,
        delay_minutes=delay,
        status=status,
        lifecycle_status=lifecycle_status,
        timeline=timeline,
        raw=row,
    )


def _elapsed_minutes(stages: list[dict[str, Any]], row: dict[str, Any]) -> int:
    """Time elapsed since the task's first recorded stage activity."""
    start = None
    for stage in stages:
        candidate = _row_value(row, stage.get("ACTUAL_COLUMN")) or _row_value(row, stage.get("PLANNED_COLUMN"))
        start = parse_datetime(candidate)
        if start:
            break
    if not start:
        return 0

    end = datetime.now()
    if stages:
        last_actual = parse_datetime(_row_value(row, stages[-1].get("ACTUAL_COLUMN")))
        if last_actual:
            end = last_actual

    return max(int((end - start).total_seconds() // 60), 0)



