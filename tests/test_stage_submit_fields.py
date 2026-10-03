from app.routers.forms import ensure_stage_actual_helper_fields


def test_submit_injects_actual_and_alhelper():
    stage_context = {
        "stage": {"STAGE_NO": "S1"},
        "fields": [
            {"field": "S1_ALTimeStamp"},
            {"field": "S1_ALHelper"},
        ],
    }

    payload = {}
    ensure_stage_actual_helper_fields(payload, stage_context, "submit")

    assert payload["S1_ALTimeStamp"]
    assert payload["S1_ALHelper"] == "FMS Submission"


def test_save_injects_actual_and_alhelper_too():
    stage_context = {
        "stage": {"STAGE_NO": "S1"},
        "fields": [
            {"field": "S1_ALTimeStamp"},
            {"field": "S1_ALHelper"},
        ],
    }

    payload = {}
    ensure_stage_actual_helper_fields(payload, stage_context, "save")

    assert payload["S1_ALTimeStamp"]
    assert payload["S1_ALHelper"] == "FMS Submission"
