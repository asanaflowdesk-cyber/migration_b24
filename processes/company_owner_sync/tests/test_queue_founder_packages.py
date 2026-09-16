from queue_founder_packages import process_claim


class Client:
    def __init__(self, duplicate=False, ordinary_ids=(), missing_ids=()):
        last_names = ["Иванов", "Иванов" if duplicate else "Петров"]
        self.contacts = [
            {"ID": str(index), "LAST_NAME": last_names[index - 1], "NAME": "Иван", "SECOND_NAME": "", "POST": "Клиент" if index in ordinary_ids else "Учредитель", "COMMENTS": "", "COMPANY_ID": "", "ASSIGNED_BY_ID": str(index + 10), "DATE_MODIFY": "2026-01-01"}
            for index in (1, 2)
            if index not in missing_ids
        ]
        self.loads = []

    def list_all(self, method, payload):
        self.loads.append(method)
        return self.contacts if method == "crm.contact.list" else []

    def call(self, method, payload):
        return next(item for item in self.contacts if int(item["ID"]) == int(payload["id"]))


def claim():
    return {"claim_id": "claim-1", "items": [{"contact_id": 1, "version": 2}, {"contact_id": 2, "version": 1}]}


def test_claim_uses_one_crm_snapshot_for_multiple_packages(tmp_path):
    client = Client()
    results, failures = process_claim(client, tmp_path, claim())
    assert failures == 0
    assert all(item["success"] for item in results)
    assert len(client.loads) == 4
    assert len(set(client.loads)) == 4


def test_duplicate_contacts_for_one_package_fail_without_writes(tmp_path):
    results, failures = process_claim(Client(duplicate=True), tmp_path, claim())
    assert failures == 2
    assert {item["error"] for item in results} == {"multiple_contacts_for_same_package"}


def test_ordinary_contact_is_ignored_without_failing_claim(tmp_path):
    results, failures = process_claim(Client(ordinary_ids=(2,)), tmp_path, claim())
    ordinary = next(item for item in results if item["contact_id"] == 2)
    assert failures == 0
    assert ordinary == {
        "contact_id": 2,
        "version": 1,
        "success": True,
        "error": "",
        "outcome": "ignored",
    }


def test_missing_contact_remains_a_failure(tmp_path):
    results, failures = process_claim(Client(missing_ids=(2,)), tmp_path, claim())
    missing = next(item for item in results if item["contact_id"] == 2)
    assert failures == 1
    assert missing["success"] is False
    assert missing["error"] == "source_contact_not_found"
    assert missing["outcome"] == "failed"
