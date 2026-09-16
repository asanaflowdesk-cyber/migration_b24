from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fast_package_reconciler import process_package_fast


class BatchClient:
    def __init__(self):
        self.source_owner = 27
        self.owners = {
            ("contact", 101): 16,
            ("company", 201): 14,
            ("company", 202): 32,
            ("lead", 301): 45,
            ("lead", 302): 38,
            ("lead", 303): 38,
        }
        self.batch_calls = 0

    def call(self, method, payload):
        if method == "crm.contact.get":
            return {
                "ID": str(payload["id"]),
                "POST": "Учредитель",
                "COMMENTS": "",
                "ASSIGNED_BY_ID": str(self.source_owner),
            }
        if method != "batch":
            raise AssertionError(method)
        self.batch_calls += 1
        results = {}
        errors = {}
        for key, command in payload["cmd"].items():
            split = urlsplit(command)
            params = parse_qs(split.query)
            parts = split.path.split(".")
            entity = parts[1]
            action = parts[2]
            item_id = int(params["id"][0])
            owner_key = (entity, item_id)
            if owner_key not in self.owners:
                errors[key] = {"error": "NOT_FOUND"}
                continue
            if action == "get":
                results[key] = {"ID": str(item_id), "ASSIGNED_BY_ID": str(self.owners[owner_key])}
            elif action == "update":
                self.owners[owner_key] = int(params["fields[ASSIGNED_BY_ID]"][0])
                results[key] = True
            else:
                raise AssertionError(action)
        return {"result": results, "result_error": errors}


def package():
    return {
        "fio": "Иванов Иван",
        "owner_id": 16,
        "source_contact_id": 100,
        "contacts": [
            {"id": 100, "title": "Иванов Иван", "owner_id": 27},
            {"id": 101, "title": "Иванов Иван", "owner_id": 16},
        ],
        "companies": [
            {
                "id": 201,
                "title": "A",
                "owner_id": 14,
                "leads": [
                    {"id": 301, "title": "L1", "owner_id": 45},
                    {"id": 302, "title": "L2", "owner_id": 38},
                ],
            },
            {
                "id": 202,
                "title": "B",
                "owner_id": 32,
                "leads": [{"id": 303, "title": "L3", "owner_id": 38}],
            },
        ],
    }


def test_fast_reconciler_repairs_all_mixed_residual_owners(tmp_path):
    client = BatchClient()
    result = process_package_fast(
        client,
        tmp_path,
        package(),
        source_contact_id=100,
        operation_id="op-1",
        claim_id="claim-1",
        attempt=1,
    )
    assert result["success"] is True
    assert result["status"] == "DONE"
    assert set(client.owners.values()) == {27}
    assert result["operation"]["planned"] == 6
    assert result["operation"]["updated"] == 6
    # The package is handled in a handful of HTTP batch calls, not one REST
    # request per get/update/verification for every entity.
    assert client.batch_calls <= 5


def test_queue_script_exposes_processing_status_and_batch_progress():
    root = Path(__file__).resolve().parents[3]
    text = (root / "integrations/bitrix_owner_sync_queue/Code.gs").read_text(encoding="utf-8")
    assert "row[2] = 'PROCESSING'" in text
    assert "body.action === 'progress'" in text
    assert "body.action === 'log_operations'" in text
