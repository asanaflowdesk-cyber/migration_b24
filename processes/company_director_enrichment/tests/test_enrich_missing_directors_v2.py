from enrich_missing_directors_v2 import (
    _annotate_director_groups,
    _choose_canonical_contact,
    _ensure_contact_company_link,
    _ensure_lead_contact_links,
)


class FakeClient:
    def __init__(self):
        self.contact_companies = {10: [{"COMPANY_ID": 100, "IS_PRIMARY": "Y"}]}
        self.lead_contacts = {
            1000: [],
            1001: [{"CONTACT_ID": 99, "IS_PRIMARY": "Y"}],
            1002: [{"CONTACT_ID": 10, "IS_PRIMARY": "Y"}],
        }
        self.calls = []

    def call(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "crm.contact.company.items.get":
            return list(self.contact_companies.get(int(payload["id"]), []))
        if method == "crm.contact.company.add":
            contact_id = int(payload["id"])
            fields = payload["fields"]
            self.contact_companies.setdefault(contact_id, []).append(
                {"COMPANY_ID": int(fields["COMPANY_ID"]), "IS_PRIMARY": fields["IS_PRIMARY"]}
            )
            return True
        if method == "crm.lead.contact.items.get":
            return list(self.lead_contacts.get(int(payload["id"]), []))
        if method == "crm.lead.contact.add":
            lead_id = int(payload["id"])
            fields = payload["fields"]
            self.lead_contacts.setdefault(lead_id, []).append(
                {"CONTACT_ID": int(fields["CONTACT_ID"]), "IS_PRIMARY": fields["IS_PRIMARY"]}
            )
            return True
        raise AssertionError(method)

    def list_all(self, method, payload=None):
        if method == "crm.lead.list":
            return [{"ID": "1000"}, {"ID": "1001"}, {"ID": "1002"}]
        raise AssertionError(method)


def test_same_director_rows_are_grouped_together():
    rows = _annotate_director_groups([
        {"company_id": 1, "status": "accepted", "director": "Иванов Иван Иванович"},
        {"company_id": 2, "status": "accepted", "director": "ИВАНОВ ИВАН ИВАНОВИЧ"},
        {"company_id": 3, "status": "accepted", "director": "Петров Петр Петрович"},
    ])
    assert [row["director_group_size"] for row in rows] == [2, 2, 1]


def test_canonical_contact_prefers_existing_director_then_oldest_id():
    canonical, duplicates = _choose_canonical_contact([
        {"ID": "90", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович", "POST": "", "COMMENTS": ""},
        {"ID": "70", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович", "POST": "Руководитель", "COMMENTS": ""},
        {"ID": "80", "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович", "POST": "Директор", "COMMENTS": ""},
    ])
    assert canonical["ID"] == "70"
    assert duplicates == [80, 90]


def test_second_company_is_added_without_switching_primary_company():
    client = FakeClient()
    assert _ensure_contact_company_link(client, 10, 200)
    assert client.contact_companies[10] == [
        {"COMPANY_ID": 100, "IS_PRIMARY": "Y"},
        {"COMPANY_ID": 200, "IS_PRIMARY": "N"},
    ]
    assert not _ensure_contact_company_link(client, 10, 200)


def test_director_is_added_to_all_company_leads_without_overwriting_other_contacts():
    client = FakeClient()
    added, already, verified = _ensure_lead_contact_links(client, 100, 10)
    assert added == 2
    assert already == 1
    assert verified == [1000, 1001, 1002]
    assert client.lead_contacts[1000] == [{"CONTACT_ID": 10, "IS_PRIMARY": "Y"}]
    assert client.lead_contacts[1001] == [
        {"CONTACT_ID": 99, "IS_PRIMARY": "Y"},
        {"CONTACT_ID": 10, "IS_PRIMARY": "N"},
    ]
    assert client.lead_contacts[1002] == [{"CONTACT_ID": 10, "IS_PRIMARY": "Y"}]
