import zipfile

import enrich_missing_directors_v4 as v4


DIRECTOR = "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА"


def _row(company_id: int, owner_id: int = 72) -> dict:
    return {
        "company_id": company_id,
        "title": f"Компания {company_id}",
        "owner_id": owner_id,
        "bin": f"{company_id:012d}",
        "requisite_ids": [company_id + 100],
        "status": "accepted",
        "director": DIRECTOR,
        "source": "adata",
        "url": "https://adata/example",
        "confidence": "single_source",
        "director_group_size": 1,
    }


class PlanClient:
    def __init__(self, *, canonical=None, company_owners=None, lead_contacts=None, contact_companies=None):
        self.canonical = canonical
        self.company_owners = company_owners or {}
        self.lead_contacts = lead_contacts or {}
        self.contact_companies = contact_companies or {}
        self.calls = []

    def call(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        assert not method.endswith(".add")
        assert not method.endswith(".update")

        if method == "crm.company.get":
            company_id = int(payload["id"])
            return {
                "ID": str(company_id),
                "ASSIGNED_BY_ID": str(self.company_owners[company_id]),
                "ORIGIN_ID": "",
            }
        if method == "crm.company.contact.items.get":
            return []
        if method == "crm.contact.company.items.get":
            return [
                {"COMPANY_ID": company_id, "IS_PRIMARY": "Y" if idx == 0 else "N"}
                for idx, company_id in enumerate(self.contact_companies.get(int(payload["id"]), []))
            ]
        if method == "crm.lead.contact.items.get":
            lead_id = int(payload["id"])
            return [
                {"CONTACT_ID": contact_id, "IS_PRIMARY": "Y" if idx == 0 else "N"}
                for idx, contact_id in enumerate(self.lead_contacts.get(lead_id, []))
            ]
        if method == "crm.contact.get":
            raise AssertionError("No linked company contacts expected in this fixture")
        raise AssertionError(method)

    def list_all(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "crm.contact.list":
            filter_ = payload.get("filter", {})
            if "LAST_NAME" in filter_:
                return [self.canonical] if self.canonical else []
            return []
        if method == "crm.requisite.list":
            company_id = int(payload["filter"]["ENTITY_ID"])
            return [{
                "ID": str(company_id + 100),
                "ENTITY_ID": str(company_id),
                "RQ_INN": f"{company_id:012d}",
                "RQ_DIRECTOR": "",
            }]
        if method == "crm.lead.list":
            company_id = int(payload["filter"]["COMPANY_ID"])
            return [{"ID": str(company_id * 100 + 1)}, {"ID": str(company_id * 100 + 2)}]
        raise AssertionError(method)


def test_plan_reuses_existing_contact_and_exposes_future_31_reassignment(monkeypatch):
    client = PlanClient(
        canonical={
            "ID": "50",
            "LAST_NAME": "Оспанова",
            "NAME": "Назым",
            "SECOND_NAME": "Батырбековна",
            "POST": "Руководитель",
            "COMMENTS": "",
            "ASSIGNED_BY_ID": "58",
        },
        company_owners={10: 72},
        lead_contacts={1001: [50], 1002: [99]},
        contact_companies={50: [99]},
    )
    monkeypatch.setattr(v4.base, "clone_client", lambda client: client)

    planned = v4.build_dry_run_plan(client, [_row(10)], workers=1)
    row = planned[0]

    assert row["plan_status"] == "READY"
    assert row["planned_contact_action"] == "REUSE"
    assert row["existing_contact_id"] == 50
    assert row["existing_contact_owner_id"] == 58
    assert row["company_owner_id"] == 72
    assert row["owner_mismatch"] == "YES"
    assert row["workflow31_would_reassign"] == "YES"
    assert row["workflow31_target_owner_id"] == 58
    assert row["company_link_action"] == "ADD_LINK"
    assert row["leads_to_link"] == "1002"
    assert row["existing_lead_links"] == "1001"


def test_plan_creates_one_contact_for_two_companies_with_same_owner(monkeypatch):
    client = PlanClient(company_owners={10: 72, 20: 72})
    monkeypatch.setattr(v4.base, "clone_client", lambda client: client)

    rows = [_row(10), _row(20)]
    rows[0]["director_group_size"] = 2
    rows[1]["director_group_size"] = 2
    planned = v4.build_dry_run_plan(client, rows, workers=1)

    assert {row["planned_contact_action"] for row in planned} == {"CREATE"}
    assert {row["planned_contact_id"] for row in planned} == {"NEW"}
    assert {row["planned_contact_owner_id"] for row in planned} == {72}
    assert {row["director_group_company_ids"] for row in planned} == {"10,20"}
    assert {row["companies_to_link"] for row in planned} == {"10,20"}
    assert planned[0]["company_link_action"] == "CREATE_PRIMARY"
    assert planned[1]["company_link_action"] == "ADD_SECONDARY_AFTER_CREATE"
    assert planned[0]["leads_to_link_count"] == 2
    assert planned[1]["leads_to_link_count"] == 2


def test_plan_blocks_new_contact_when_companies_have_different_owners(monkeypatch):
    client = PlanClient(company_owners={10: 72, 20: 58})
    monkeypatch.setattr(v4.base, "clone_client", lambda client: client)

    rows = [_row(10, 72), _row(20, 58)]
    rows[0]["director_group_size"] = 2
    rows[1]["director_group_size"] = 2
    planned = v4.build_dry_run_plan(client, rows, workers=1)

    assert {row["plan_status"] for row in planned} == {"BLOCKED"}
    assert {row["plan_block_reason"] for row in planned} == {"director_owner_conflict"}
    assert {row["planned_contact_action"] for row in planned} == {"BLOCKED"}


def test_plan_is_read_only(monkeypatch):
    client = PlanClient(company_owners={10: 72})
    monkeypatch.setattr(v4.base, "clone_client", lambda client: client)
    v4.build_dry_run_plan(client, [_row(10)], workers=1)

    mutation_methods = [
        method for method, _payload in client.calls
        if method.endswith(".add") or method.endswith(".update") or method.endswith(".delete")
    ]
    assert mutation_methods == []


def test_v4_workbook_has_plan_columns_and_no_business_analyst_columns(tmp_path):
    row = _row(10)
    row.update({
        "plan_status": "READY",
        "planned_contact_action": "CREATE",
        "planned_contact_id": "NEW",
        "planned_contact_owner_id": 72,
        "company_owner_id": 72,
        "owner_mismatch": "NO",
        "workflow31_would_reassign": "NO",
        "workflow31_target_owner_id": 72,
        "director_group_company_ids": "10",
        "companies_to_link": "10",
        "company_link_action": "CREATE_PRIMARY",
        "requisites_to_update": "110",
        "requisites_to_update_count": 1,
        "lead_ids_all": "1001,1002",
        "leads_to_link": "1001,1002",
        "leads_to_link_count": 2,
        "existing_lead_links": "",
        "existing_lead_links_count": 0,
    })
    v4._write_workbook_v4(tmp_path, [row], [])
    with zipfile.ZipFile(tmp_path / "company_director_enrichment.xlsx") as archive:
        shared = archive.read("xl/sharedStrings.xml").decode("utf-8")
    assert "31 потом сменит ответственного" in shared
    assert "Лиды к привязке" in shared
    assert "Бизнес Аналитик" not in shared
