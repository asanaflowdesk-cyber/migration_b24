import json
from pathlib import Path

import director_frozen_plan as frozen

DIRECTOR = "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА"


def accepted_row(company_id: int = 10, owner_id: int = 72) -> dict:
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
        "adata_status": "found",
        "adata_director": DIRECTOR,
        "adata_url": "https://adata/example",
        "kompra_status": "no_director",
        "kompra_director": "",
        "kompra_url": "https://kompra/example",
        "director_group_size": 1,
    }


class MutableClient:
    def __init__(self):
        self.companies = {
            10: {"ID": "10", "TITLE": "Компания 10", "ASSIGNED_BY_ID": "72", "ORIGIN_ID": ""},
            20: {"ID": "20", "TITLE": "Компания 20", "ASSIGNED_BY_ID": "72", "ORIGIN_ID": ""},
        }
        self.requisites = {
            10: [{"ID": "110", "ENTITY_ID": "10", "RQ_INN": "000000000010", "RQ_DIRECTOR": ""}],
            20: [{"ID": "120", "ENTITY_ID": "20", "RQ_INN": "000000000020", "RQ_DIRECTOR": ""}],
        }
        self.contacts = {}
        self.company_contacts = {10: [], 20: []}
        self.contact_companies = {}
        self.leads = {
            1001: {"ID": "1001", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "72"},
            1002: {"ID": "1002", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "58"},
        }
        self.lead_contacts = {1001: [], 1002: [99]}
        self.next_contact_id = 500
        self.calls = []

    def add_contact(
        self,
        contact_id: int,
        *,
        director: bool = True,
        owner_id: int | None = 72,
        comments: str = "",
    ):
        self.contacts[contact_id] = {
            "ID": str(contact_id),
            "COMPANY_ID": "",
            "LAST_NAME": "Оспанова",
            "NAME": "Назым",
            "SECOND_NAME": "Батырбековна",
            "POST": "Руководитель" if director else "",
            "COMMENTS": comments,
            "ASSIGNED_BY_ID": str(owner_id) if owner_id is not None else "",
        }
        self.contact_companies.setdefault(contact_id, [])

    def call(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "crm.company.get":
            return dict(self.companies[int(payload["id"])])

        if method == "crm.company.contact.items.get":
            company_id = int(payload["id"])
            return [
                {"CONTACT_ID": cid, "IS_PRIMARY": "Y" if idx == 0 else "N"}
                for idx, cid in enumerate(self.company_contacts.get(company_id, []))
            ]

        if method == "crm.contact.company.items.get":
            contact_id = int(payload["id"])
            return [
                {"COMPANY_ID": cid, "IS_PRIMARY": "Y" if idx == 0 else "N"}
                for idx, cid in enumerate(self.contact_companies.get(contact_id, []))
            ]

        if method == "crm.contact.company.add":
            contact_id = int(payload["id"])
            company_id = int(payload["fields"]["COMPANY_ID"])
            if company_id not in self.contact_companies.setdefault(contact_id, []):
                self.contact_companies[contact_id].append(company_id)
            if contact_id not in self.company_contacts.setdefault(company_id, []):
                self.company_contacts[company_id].append(contact_id)
            return True

        if method == "crm.lead.contact.items.get":
            lead_id = int(payload["id"])
            return [
                {"CONTACT_ID": cid, "IS_PRIMARY": "Y" if idx == 0 else "N"}
                for idx, cid in enumerate(self.lead_contacts.get(lead_id, []))
            ]

        if method == "crm.lead.contact.add":
            lead_id = int(payload["id"])
            contact_id = int(payload["fields"]["CONTACT_ID"])
            if contact_id not in self.lead_contacts.setdefault(lead_id, []):
                self.lead_contacts[lead_id].append(contact_id)
            return True

        if method == "crm.contact.get":
            contact = self.contacts.get(int(payload["id"]))
            return dict(contact) if contact else None

        if method == "crm.contact.add":
            contact_id = self.next_contact_id
            self.next_contact_id += 1
            fields = dict(payload["fields"])
            primary_company = int(fields.get("COMPANY_ID") or 0)
            self.contacts[contact_id] = {
                "ID": str(contact_id),
                **{key: str(value) for key, value in fields.items()},
            }
            self.contact_companies[contact_id] = []
            if primary_company:
                self.contact_companies[contact_id].append(primary_company)
                self.company_contacts.setdefault(primary_company, []).append(contact_id)
            return contact_id

        if method == "crm.contact.update":
            contact_id = int(payload["id"])
            for key, value in payload["fields"].items():
                self.contacts[contact_id][key] = str(value)
            return True

        if method == "crm.requisite.update":
            req_id = int(payload["id"])
            for rows in self.requisites.values():
                for row in rows:
                    if int(row["ID"]) == req_id:
                        row.update({key: str(value) for key, value in payload["fields"].items()})
                        return True
            raise AssertionError(f"missing requisite {req_id}")

        raise AssertionError(method)

    def list_all(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        filter_ = payload.get("filter", {})

        if method == "crm.contact.list":
            if "LAST_NAME" in filter_:
                last = str(filter_["LAST_NAME"]).casefold()
                return [
                    dict(row)
                    for row in self.contacts.values()
                    if str(row.get("LAST_NAME") or "").casefold() == last
                ]
            if "COMPANY_ID" in filter_:
                company_id = int(filter_["COMPANY_ID"])
                return [
                    dict(row)
                    for row in self.contacts.values()
                    if str(row.get("COMPANY_ID") or "") == str(company_id)
                ]
            return [dict(row) for row in self.contacts.values()]

        if method == "crm.requisite.list":
            company_id = int(filter_["ENTITY_ID"])
            return [dict(row) for row in self.requisites.get(company_id, [])]

        if method == "crm.lead.list":
            company_id = int(filter_["COMPANY_ID"])
            return [
                dict(row)
                for row in self.leads.values()
                if int(row["COMPANY_ID"]) == company_id
            ]

        raise AssertionError(method)


def test_source_502_is_not_silently_called_no_result():
    rows = frozen.mark_source_unavailable([
        {
            "company_id": 1,
            "status": "no_result",
            "adata_status": "HTTP_502",
            "kompra_status": "no_director",
        }
    ])
    assert rows[0]["status"] == "source_unavailable"


def test_one_404_and_one_no_director_stays_no_result():
    rows = frozen.mark_source_unavailable([
        {
            "company_id": 1,
            "status": "no_result",
            "adata_status": "HTTP_404",
            "kompra_status": "no_director",
        }
    ])
    assert rows[0]["status"] == "no_result"


def test_ordinary_exact_fio_contact_is_blocked():
    client = MutableClient()
    client.add_contact(50, director=False)
    row = frozen.build_exact_dry_run_plan(client, [accepted_row()], workers=1)[0]
    assert row["plan_status"] == "BLOCKED"
    assert row["plan_block_reason"] == "ordinary_contact_same_fio_requires_review"


def test_multiple_exact_fio_contacts_are_blocked():
    client = MutableClient()
    client.add_contact(50, director=True)
    client.add_contact(60, director=True)
    row = frozen.build_exact_dry_run_plan(client, [accepted_row()], workers=1)[0]
    assert row["plan_status"] == "BLOCKED"
    assert row["plan_block_reason"] == "ambiguous_multiple_exact_fio_contacts"


def test_frozen_plan_hash_round_trip_and_tamper_detection(tmp_path: Path):
    client = MutableClient()
    rows = frozen.build_exact_dry_run_plan(client, [accepted_row()], workers=1)
    plan = frozen.create_frozen_plan(rows, [], "abc123", "999")
    path = frozen.save_frozen_plan(tmp_path, plan)

    loaded = frozen.load_frozen_plan(path, plan["plan_id"], "abc123")
    assert loaded["plan_id"] == plan["plan_id"]

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["rows"][0]["company_owner_id"] = 999
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    try:
        frozen.load_frozen_plan(path, plan["plan_id"], "abc123")
    except RuntimeError as exc:
        assert str(exc) == "plan_hash_invalid"
    else:
        raise AssertionError("tampered plan was accepted")


def test_plan_from_other_code_sha_is_rejected(tmp_path: Path):
    client = MutableClient()
    rows = frozen.build_exact_dry_run_plan(client, [accepted_row()], workers=1)
    plan = frozen.create_frozen_plan(rows, [], "sha-old", "999")
    path = frozen.save_frozen_plan(tmp_path, plan)
    try:
        frozen.load_frozen_plan(path, plan["plan_id"], "sha-new")
    except RuntimeError as exc:
        assert str(exc).startswith("plan_code_sha_mismatch:")
    else:
        raise AssertionError("stale code plan was accepted")


def test_preflight_stops_everything_if_new_lead_appears():
    client = MutableClient()
    rows = frozen.build_exact_dry_run_plan(client, [accepted_row()], workers=1)
    plan = frozen.create_frozen_plan(rows, [], "abc", "1")

    client.leads[1003] = {"ID": "1003", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "72"}
    client.lead_contacts[1003] = []

    client.calls.clear()
    result_rows, errors = frozen.apply_frozen_plan(client, plan)
    assert errors
    assert any("lead_set_changed" in error for error in errors)
    assert result_rows[0]["verification_status"] == "PREFLIGHT_FAILED"
    assert not any(
        method.endswith(".add") or method.endswith(".update")
        for method, _payload in client.calls
    )


def test_apply_is_exact_and_idempotent_for_create_plan():
    client = MutableClient()
    rows = frozen.build_exact_dry_run_plan(client, [accepted_row()], workers=1)
    plan = frozen.create_frozen_plan(rows, [], "abc", "1")

    first_rows, first_errors = frozen.apply_frozen_plan(client, plan)
    assert first_errors == []
    assert first_rows[0]["verification_status"] == "VERIFIED"
    assert len(client.contacts) == 1
    contact_id = next(iter(client.contacts))
    assert f"DIRECTOR_PLAN_ID: {plan['plan_id']}" in client.contacts[contact_id]["COMMENTS"]
    assert contact_id in client.company_contacts[10]
    assert contact_id in client.lead_contacts[1001]
    assert contact_id in client.lead_contacts[1002]
    assert client.requisites[10][0]["RQ_DIRECTOR"] == DIRECTOR

    second_rows, second_errors = frozen.apply_frozen_plan(client, plan)
    assert second_errors == []
    assert second_rows[0]["verification_status"] == "VERIFIED"
    assert len(client.contacts) == 1
    assert client.lead_contacts[1001].count(contact_id) == 1
    assert client.lead_contacts[1002].count(contact_id) == 1


def test_apply_never_adds_a_lead_not_in_frozen_plan():
    client = MutableClient()
    rows = frozen.build_exact_dry_run_plan(client, [accepted_row()], workers=1)
    plan = frozen.create_frozen_plan(rows, [], "abc", "1")
    planned_leads = set(frozen._ids(rows[0]["lead_ids_all"]))
    assert planned_leads == {1001, 1002}

    result_rows, errors = frozen.apply_frozen_plan(client, plan)
    assert errors == []
    contact_id = int(result_rows[0]["contact_id"])
    linked_leads = {
        lead_id
        for lead_id, contacts in client.lead_contacts.items()
        if contact_id in contacts
    }
    assert linked_leads == planned_leads
