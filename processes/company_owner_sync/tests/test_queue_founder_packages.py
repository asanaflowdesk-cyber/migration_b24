from queue_founder_packages import _coalesce_and_partition, process_claim


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
        if method == "batch":
            raise RuntimeError("batch unavailable in unit double")
        return next(item for item in self.contacts if int(item["ID"]) == int(payload["id"]))

    def update_contact(self, contact_id, fields):
        row = next(item for item in self.contacts if int(item["ID"]) == int(contact_id))
        row.update(fields)
        return True


def claim():
    return {"claim_id": "claim-1", "items": [{"contact_id": 1, "version": 2}, {"contact_id": 2, "version": 1}]}


def test_claim_uses_one_crm_snapshot_for_multiple_packages(tmp_path):
    client = Client()
    results, failures = process_claim(client, tmp_path, claim())
    assert failures == 0
    assert all(item["success"] for item in results)
    assert len(client.loads) == 4
    assert len(set(client.loads)) == 4


def test_duplicate_contacts_for_one_package_are_coalesced_not_failed(tmp_path):
    client = Client(duplicate=True)
    results, failures = process_claim(client, tmp_path, claim())
    assert failures == 0
    assert all(item["success"] for item in results)
    assert all(item.get("error", "") != "multiple_contacts_for_same_package" for item in results)
    assert {int(item["ASSIGNED_BY_ID"]) for item in client.contacts} == {11}


def test_overlap_partition_assigns_each_entity_to_only_one_job():
    shared_company = {"id": 20, "title": "shared", "owner_id": 1, "leads": [{"id": 30, "title": "shared lead", "owner_id": 1}]}
    jobs = [
        {
            "item": {"updated_at": "2026-09-16T10:00:00Z"},
            "contact_id": 1,
            "version": 1,
            "operation_id": "a",
            "package": {"fio": "A", "contacts": [{"id": 1}], "companies": [shared_company]},
        },
        {
            "item": {"updated_at": "2026-09-16T10:01:00Z"},
            "contact_id": 2,
            "version": 1,
            "operation_id": "b",
            "package": {"fio": "B", "contacts": [{"id": 2}], "companies": [shared_company]},
        },
    ]
    leaders, _aliases = _coalesce_and_partition(jobs)
    owned = [set(job["owned_entity_keys"]) for job in leaders]
    assert owned[0].isdisjoint(owned[1])
    assert ("company", 20) in owned[1]
    assert ("lead", 30) in owned[1]


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
