from queue_founder_packages import process_claim


class Client:
    def __init__(self, duplicate=False):
        last_names = ["Иванов", "Иванов" if duplicate else "Петров"]
        self.contacts = [
            {"ID": str(index), "LAST_NAME": last_names[index - 1], "NAME": "Иван", "SECOND_NAME": "", "POST": "Учредитель", "COMMENTS": "", "COMPANY_ID": "", "ASSIGNED_BY_ID": str(index + 10), "DATE_MODIFY": "2026-01-01"}
            for index in (1, 2)
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
