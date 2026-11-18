from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import client as api_client_mod
from app.api import metadata_api
from app.auth.request_context import (
    get_current_database_target,
    reset_current_database_target,
    set_current_database_target,
)
from app.middleware.oneportal_auth import _call_next_with_live_routing
from app.services import dashboard_service, metadata_service, task_service


def _clear_all_caches() -> None:
    metadata_service._cache.clear()
    metadata_api._METADATA_CACHE.clear()
    dashboard_service._PAGE_CACHE.clear()
    dashboard_service._FILTER_OPTIONS_CACHE.clear()
    dashboard_service._SALES_ORDER_OPTIONS_CACHE.clear()
    task_service._locator_cache.clear()


@pytest.fixture(autouse=True)
def clean_caches_and_context():
    _clear_all_caches()
    yield
    _clear_all_caches()


def _sample_db_tables() -> dict[str, dict[str, list[dict]]]:
    """Separate OperationsDB (apiv3.php) and Test_OperationsDB (apiv2.php) fixtures."""
    return {
        "API_2": {
            "TM_Application_Master": [
                {"app_id": "APP000001", "app_name": "Pack to Dispatch Details", "Type": "F", "Live": "Y", "active": 1},
                {"app_id": "APP000003", "app_name": "FMS_Lead Time Grant", "Type": "F", "Live": "N", "active": 1},
                {"app_id": "APP000077", "app_name": "Runner360", "Type": "F", "Live": "Y", "active": 1},
                {"app_id": "APP000080", "app_name": "Draft Workflow", "Type": "F", "Live": None, "active": 1},
                {"app_id": "APP000081", "app_name": "Blank Live Workflow", "Type": "F", "Live": "", "active": 1},
            ],
            "FMS_Config": [
                {"APP_ID": "APP000001", "FMS_NAME": "Pack to Dispatch Details", "FMS_TYPE": "F", "FMS_Table_Name": "Z_SalesOrder", "STAGE_NO": "S1", "STAGE_NAME": "Pick", "DOER": "Prod Doer 1", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
                {"APP_ID": "APP000003", "FMS_NAME": "FMS_Lead Time Grant", "FMS_TYPE": "F", "FMS_Table_Name": "Z_Lead_Time_Grant", "STAGE_NO": "S1", "STAGE_NAME": "Review", "DOER": "NonLive Doer", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
                {"APP_ID": "APP000077", "FMS_NAME": "Runner360", "FMS_TYPE": "F", "FMS_Table_Name": "Z_Runner360", "STAGE_NO": "S1", "STAGE_NAME": "Return", "DOER": "Prod Runner", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
                {"APP_ID": "APP000080", "FMS_NAME": "Draft Workflow", "FMS_TYPE": "F", "FMS_Table_Name": "Z_Draft", "STAGE_NO": "S1", "STAGE_NAME": "Draft", "DOER": "Draft Doer", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
                {"APP_ID": "APP000081", "FMS_NAME": "Blank Live Workflow", "FMS_TYPE": "F", "FMS_Table_Name": "Z_Blank", "STAGE_NO": "S1", "STAGE_NAME": "Blank", "DOER": "Blank Doer", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
            ],
        },
        "API_1": {
            "TM_Application_Master": [
                {"app_id": "APP000001", "app_name": "Pack to Dispatch Details", "Type": "F", "Live": "Y", "active": 1},
                {"app_id": "APP000003", "app_name": "FMS_Lead Time Grant", "Type": "F", "Live": "N", "active": 1},
                {"app_id": "APP000018", "app_name": "Hot Shot Transfer", "Type": "F", "Live": None, "active": 1},
                {"app_id": "APP000019", "app_name": "WIP Blank FMS", "Type": "F", "Live": "   ", "active": 1},
            ],
            "FMS_Config": [
                {"APP_ID": "APP000001", "FMS_NAME": "Pack to Dispatch Details", "FMS_TYPE": "F", "FMS_Table_Name": "Z_SalesOrder", "STAGE_NO": "S1", "STAGE_NAME": "Pick", "DOER": "Live Doer", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
                {"APP_ID": "APP000003", "FMS_NAME": "FMS_Lead Time Grant", "FMS_TYPE": "F", "FMS_Table_Name": "Z_Lead_Time_Grant", "STAGE_NO": "S1", "STAGE_NAME": "Review", "DOER": "Test Doer N", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
                {"APP_ID": "APP000018", "FMS_NAME": "Hot Shot Transfer", "FMS_TYPE": "F", "FMS_Table_Name": "Z_HotShotTransfer", "STAGE_NO": "S1", "STAGE_NAME": "Dispatch", "DOER": "Test Doer Null", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
                {"APP_ID": "APP000019", "FMS_NAME": "WIP Blank FMS", "FMS_TYPE": "F", "FMS_Table_Name": "Z_WipBlank", "STAGE_NO": "S1", "STAGE_NAME": "Init", "DOER": "Test Doer Blank", "PLANNED_COLUMN": "S1_PLTimeStamp", "ACTUAL_COLUMN": "S1_ALTimeStamp"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_operations_db_shows_only_live_y_fms_and_uses_apiv3(monkeypatch):
    db_tables = _sample_db_tables()
    called_urls: list[str] = []

    async def fake_get_table(table_name: str, filters=None):
        base = api_client_mod.get_active_api_base()
        _, url = api_client_mod.resolve_endpoint("table_gateway")
        called_urls.append(url)
        return list(db_tables[base].get(table_name, []))

    monkeypatch.setattr(metadata_service.table_api, "get_table", fake_get_table)

    token = set_current_database_target("OperationsDB")
    try:
        options = await metadata_service.get_dashboard_fms_options()
        names = [opt["fms_name"] for opt in options]
        assert names == ["Pack to Dispatch Details", "Runner360"]
        assert all(opt["live"] == "Y" for opt in options)
        assert all(opt["target_db"] == "OperationsDB" for opt in options)
        assert all("apiv3.php" in url for url in called_urls)

        doers = await dashboard_service.get_doer_options()
        assert doers == ["Prod Doer 1", "Prod Runner"]
        assert "NonLive Doer" not in doers
        assert "Draft Doer" not in doers
    finally:
        reset_current_database_target(token)


@pytest.mark.asyncio
async def test_test_operations_db_shows_all_fms_with_per_fms_live_targets(monkeypatch):
    db_tables = _sample_db_tables()
    called_urls: list[str] = []

    async def fake_get_table(table_name: str, filters=None):
        base = api_client_mod.get_active_api_base()
        _, url = api_client_mod.resolve_endpoint("table_gateway")
        called_urls.append(url)
        return list(db_tables[base].get(table_name, []))

    monkeypatch.setattr(metadata_service.table_api, "get_table", fake_get_table)

    token = set_current_database_target("Test_OperationsDB")
    try:
        options = await metadata_service.get_dashboard_fms_options()
        names = [opt["fms_name"] for opt in options]
        assert names == [
            "FMS_Lead Time Grant",
            "Hot Shot Transfer",
            "Pack to Dispatch Details",
            "WIP Blank FMS",
        ]
        by_name = {opt["fms_name"]: opt for opt in options}
        assert by_name["Pack to Dispatch Details"]["target_db"] == "OperationsDB"
        assert by_name["FMS_Lead Time Grant"]["target_db"] == "Test_OperationsDB"
        assert by_name["Hot Shot Transfer"]["target_db"] == "Test_OperationsDB"
        assert by_name["WIP Blank FMS"]["target_db"] == "Test_OperationsDB"
        assert all("apiv2.php" in url for url in called_urls)

        doers = await dashboard_service.get_doer_options()
        assert doers == ["Live Doer", "Test Doer Blank", "Test Doer N", "Test Doer Null"]
    finally:
        reset_current_database_target(token)


@pytest.mark.asyncio
async def test_caches_do_not_bleed_between_operations_and_test_db(monkeypatch):
    db_tables = _sample_db_tables()

    async def fake_get_table(table_name: str, filters=None):
        base = api_client_mod.get_active_api_base()
        return list(db_tables[base].get(table_name, []))

    monkeypatch.setattr(metadata_service.table_api, "get_table", fake_get_table)

    prod_token = set_current_database_target("OperationsDB")
    try:
        prod_names = [opt["fms_name"] for opt in await metadata_service.get_dashboard_fms_options()]
        assert prod_names == ["Pack to Dispatch Details", "Runner360"]
    finally:
        reset_current_database_target(prod_token)

    test_token = set_current_database_target("Test_OperationsDB")
    try:
        test_names = [opt["fms_name"] for opt in await metadata_service.get_dashboard_fms_options()]
        assert test_names == [
            "FMS_Lead Time Grant",
            "Hot Shot Transfer",
            "Pack to Dispatch Details",
            "WIP Blank FMS",
        ]
    finally:
        reset_current_database_target(test_token)


@pytest.mark.asyncio
async def test_pull_and_append_routes_by_live_in_both_operations_and_test_db(monkeypatch):
    db_tables = _sample_db_tables()

    async def fake_get_table(table_name: str, filters=None):
        base = api_client_mod.get_active_api_base()
        return list(db_tables[base].get(table_name, []))

    monkeypatch.setattr(metadata_service.table_api, "get_table", fake_get_table)

    # Case 1: OperationsDB selected
    prod_token = set_current_database_target("OperationsDB")
    try:
        # Live = 'Y' -> apiv3.php
        async with metadata_service.app_live_database_context(app_id="APP000001") as resolved:
            assert resolved == "OperationsDB"
            _, url = api_client_mod.resolve_endpoint("update_row")
            assert url.endswith("/apiv3.php")

        # Live = 'N' -> apiv2.php
        async with metadata_service.app_live_database_context(app_id="APP000003") as resolved:
            assert resolved == "Test_OperationsDB"
            _, url = api_client_mod.resolve_endpoint("update_row")
            assert url.endswith("/apiv2.php")

        # Live = None -> apiv2.php
        async with metadata_service.app_live_database_context(app_id="APP000080") as resolved:
            assert resolved == "Test_OperationsDB"
            _, url = api_client_mod.resolve_endpoint("table_gateway")
            assert url.endswith("/apiv2.php")

        # Live = '' -> apiv2.php
        async with metadata_service.app_live_database_context(table_name="Z_Blank") as resolved:
            assert resolved == "Test_OperationsDB"
            _, url = api_client_mod.resolve_endpoint("update_row")
            assert url.endswith("/apiv2.php")

        assert get_current_database_target() == "OperationsDB"
    finally:
        reset_current_database_target(prod_token)

    # Case 2: Test_OperationsDB selected -> STILL routes Live='Y' to apiv3.php and N/Null/Blank to apiv2.php
    test_token = set_current_database_target("Test_OperationsDB")
    try:
        # Live = 'Y' in Test_OperationsDB -> apiv3.php
        async with metadata_service.app_live_database_context(app_id="APP000001") as resolved:
            assert resolved == "OperationsDB"
            _, url = api_client_mod.resolve_endpoint("update_row")
            assert url.endswith("/apiv3.php")

        # Live = 'N' in Test_OperationsDB -> apiv2.php
        async with metadata_service.app_live_database_context(app_id="APP000003") as resolved:
            assert resolved == "Test_OperationsDB"
            _, url = api_client_mod.resolve_endpoint("update_row")
            assert url.endswith("/apiv2.php")

        # Live = None in Test_OperationsDB -> apiv2.php
        async with metadata_service.app_live_database_context(app_id="APP000018") as resolved:
            assert resolved == "Test_OperationsDB"
            _, url = api_client_mod.resolve_endpoint("update_row")
            assert url.endswith("/apiv2.php")

        assert get_current_database_target() == "Test_OperationsDB"
    finally:
        reset_current_database_target(test_token)


@pytest.mark.asyncio
async def test_dashboard_rows_route_live_y_to_apiv3_and_non_live_to_apiv2_in_test_db(monkeypatch):
    db_tables = _sample_db_tables()
    fetched_table_urls: dict[str, list[str]] = {}

    async def fake_get_table(table_name: str, filters=None):
        base = api_client_mod.get_active_api_base()
        return list(db_tables[base].get(table_name, []))

    async def fake_resolve_pk(table_name: str):
        return "RowID"

    async def fake_fetch_page(table_name: str, filters=None, page: int = 1, page_size: int = 50):
        _, url = api_client_mod.resolve_endpoint("table_gateway")
        fetched_table_urls.setdefault(table_name, []).append(url)
        return (
            [{"RowID": "101", "S1_PLTimeStamp": "2026-11-18 10:00:00", "S1_ALTimeStamp": ""}],
            1,
            1,
        )

    monkeypatch.setattr(metadata_service.table_api, "get_table", fake_get_table)
    monkeypatch.setattr(metadata_service, "resolve_primary_key", fake_resolve_pk)
    monkeypatch.setattr(dashboard_service, "_fetch_dashboard_api_page", fake_fetch_page)

    # Under Test_OperationsDB, selecting both a Live='Y' FMS and a Live=None FMS
    # must load Z_SalesOrder from apiv3.php and Z_HotShotTransfer from apiv2.php.
    test_token = set_current_database_target("Test_OperationsDB")
    try:
        rows = await dashboard_service.get_dashboard_rows(
            fms_names=["Pack to Dispatch Details", "Hot Shot Transfer"]
        )
        assert len(rows) == 2
        assert all(u.endswith("/apiv3.php") for u in fetched_table_urls["Z_SalesOrder"])
        assert all(u.endswith("/apiv2.php") for u in fetched_table_urls["Z_HotShotTransfer"])
    finally:
        reset_current_database_target(test_token)


@pytest.mark.asyncio
async def test_middleware_live_routing_for_forms_and_dashboard_in_both_dbs(monkeypatch):
    db_tables = _sample_db_tables()

    async def fake_get_table(table_name: str, filters=None):
        base = api_client_mod.get_active_api_base()
        return list(db_tables[base].get(table_name, []))

    monkeypatch.setattr(metadata_service.table_api, "get_table", fake_get_table)

    def make_request(path: str, query_params: dict[str, str], initial_db: str):
        return SimpleNamespace(
            url=SimpleNamespace(path=path),
            query_params=query_params,
            state=SimpleNamespace(target_database=initial_db),
        )

    # OperationsDB session
    prod_token = set_current_database_target("OperationsDB")
    try:
        observed = {}

        async def capture(req):
            observed["db"] = api_client_mod.get_effective_database_target()
            _, observed["url"] = api_client_mod.resolve_endpoint("update_row")
            observed["state_db"] = req.state.target_database
            return "ok"

        await _call_next_with_live_routing(
            make_request("/forms/stage/10/S1", {"app_id": "APP000003"}, "OperationsDB"),
            capture,
        )
        assert observed["db"] == "Test_OperationsDB"
        assert observed["url"].endswith("/apiv2.php")

        observed.clear()
        await _call_next_with_live_routing(
            make_request("/forms/stage/10/S1", {"app_id": "APP000001"}, "OperationsDB"),
            capture,
        )
        assert observed["db"] == "OperationsDB"
        assert observed["url"].endswith("/apiv3.php")

        observed.clear()
        await _call_next_with_live_routing(
            make_request("/dashboard", {"fms_name": "FMS_Lead Time Grant"}, "OperationsDB"),
            capture,
        )
        assert observed["db"] == "OperationsDB"
        assert observed["url"].endswith("/apiv3.php")
    finally:
        reset_current_database_target(prod_token)

    # Test_OperationsDB session -> Live='Y' form routes to apiv3.php, Live='N' stays on apiv2.php
    test_token = set_current_database_target("Test_OperationsDB")
    try:
        observed = {}

        async def capture(req):
            observed["db"] = api_client_mod.get_effective_database_target()
            _, observed["url"] = api_client_mod.resolve_endpoint("update_row")
            observed["state_db"] = req.state.target_database
            return "ok"

        await _call_next_with_live_routing(
            make_request("/forms/stage/10/S1", {"app_id": "APP000001"}, "Test_OperationsDB"),
            capture,
        )
        assert observed["db"] == "OperationsDB"
        assert observed["state_db"] == "OperationsDB"
        assert observed["url"].endswith("/apiv3.php")

        observed.clear()
        await _call_next_with_live_routing(
            make_request("/forms/stage/10/S1", {"app_id": "APP000003"}, "Test_OperationsDB"),
            capture,
        )
        assert observed["db"] == "Test_OperationsDB"
        assert observed["url"].endswith("/apiv2.php")

        # Dashboard stays on Test_OperationsDB so dropdown shows ALL FMS
        observed.clear()
        await _call_next_with_live_routing(
            make_request("/dashboard", {}, "Test_OperationsDB"),
            capture,
        )
        assert observed["db"] == "Test_OperationsDB"
        assert observed["url"].endswith("/apiv2.php")
    finally:
        reset_current_database_target(test_token)
