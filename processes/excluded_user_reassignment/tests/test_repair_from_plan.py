import csv

from repair_from_plan import run


class FakeClient:
    def __init__(self):
        self.leads = {101: {"ID": "101", "TITLE": "Лид 101", "ASSIGNED_BY_ID": "15", "STATUS_ID": "JUNK"}}
        self.companies = {201: {"ID": "201", "TITLE": "Компания", "ASSIGNED_BY_ID": "15"}}
        self.contacts = {301: {"ID": "301", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "", "ASSIGNED_BY_ID": "15"}}
        self.calls = []

    def list_all(self, method, params):
        if method == "crm.lead.list":
            return [dict(v) for v in self.leads.values()]
        if method == "crm.company.list":
            return [dict(v) for v in self.companies.values()]
        if method == "crm.contact.list":
            return [dict(v) for v in self.contacts.values()]
        raise AssertionError(method)

    def update_lead(self, entity_id, fields):
        entity_id = int(entity_id)
        self.calls.append(("lead", entity_id, dict(fields)))
        self.leads[entity_id].update({k: str(v) for k, v in fields.items()})
        # Simulate a Bitrix robot attached to entering NEW that overwrites owner.
        if fields == {"STATUS_ID": "NEW"}:
            self.leads[entity_id]["ASSIGNED_BY_ID"] = "999"

    def update_company(self, entity_id, fields):
        entity_id = int(entity_id)
        self.calls.append(("company", entity_id, dict(fields)))
        self.companies[entity_id].update({k: str(v) for k, v in fields.items()})

    def update_contact(self, entity_id, fields):
        entity_id = int(entity_id)
        self.calls.append(("contact", entity_id, dict(fields)))
        self.contacts[entity_id].update({k: str(v) for k, v in fields.items()})


def write_plan(path):
    fields = ["founder_key", "entity_type", "entity_id", "title", "new_owner_id", "new_status_id"]
    rows = [
        {"founder_key": "fio:test", "entity_type": "lead", "entity_id": 101, "title": "Лид 101", "new_owner_id": 72, "new_status_id": "NEW"},
        {"founder_key": "fio:test", "entity_type": "company", "entity_id": 201, "title": "Компания", "new_owner_id": 72, "new_status_id": ""},
        {"founder_key": "fio:test", "entity_type": "contact", "entity_id": 301, "title": "Иванов", "new_owner_id": 72, "new_status_id": ""},
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_repair_reasserts_owner_after_new_stage_robot(tmp_path, monkeypatch):
    monkeypatch.setenv("REASSIGN_STABILIZE_SECONDS", "0")
    monkeypatch.setenv("REASSIGN_FINAL_WAIT_SECONDS", "0")
    monkeypatch.setenv("REASSIGN_PROGRESS_EVERY", "1")
    plan = tmp_path / "plan.csv"
    write_plan(plan)
    client = FakeClient()

    summary = run(client, plan, tmp_path / "out", apply=True)

    assert summary["plan_total"] == 3
    assert summary["verified_total"] == 3
    assert summary["verify_errors"] == 0
    assert client.leads[101]["STATUS_ID"] == "NEW"
    assert client.leads[101]["ASSIGNED_BY_ID"] == "72"
    assert client.companies[201]["ASSIGNED_BY_ID"] == "72"
    assert client.contacts[301]["ASSIGNED_BY_ID"] == "72"
    assert client.calls[0] == ("lead", 101, {"STATUS_ID": "NEW"})
    assert ("lead", 101, {"ASSIGNED_BY_ID": 72}) in client.calls[1:]
