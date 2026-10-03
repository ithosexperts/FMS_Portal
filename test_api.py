import requests
import json
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL = "https://api.hosexperts.com:85/apiv3.php/api/fms/task/save"

payload = {
    "table": "Z_SalesOrder",
    "task_id": "1814",
    "stage_no": "S1",
    "procedure": "SP_FMS_ALTimeStampStatus",
    "linked_stored_procedure": "SP_FMS_ALTimeStampStatus",
    "data": {
        "PLTimeStamp": "2026-08-12 14:00:00",
        "ALTimeStamp": "2026-08-12 14:30:00",
        "DoerCode": "0017",
        "RowID": 1814,
        "AppID": "APP000001",
        "StageNo": "S1",
        "LeadTimeMins": 30,
        "KPIApplicable": "N",
        "ALHelper": "FMS"
    }
}

print("\n========== PYTHON -> SP ==========")
print(json.dumps(payload, indent=2))
print("==================================\n")

response = requests.post(
    URL,
    json=payload,
    timeout=60,
    verify=False
)

print("HTTP STATUS:", response.status_code)
print("RAW RESPONSE:")
print(response.text)