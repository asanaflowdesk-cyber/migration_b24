from __future__ import annotations

from sync_company_owners import build_audit_rows


def test_earliest_lead_owner_is_target_even_if_later_leads_have_other_owners():
    companies = [{"ID": "10", "TITLE": "A", "ASSIGNED_BY_ID": "99"}]
    leads = [
        {
            "ID": "20",
            "TITLE": "later",
            "COMPANY_ID": "10",
            "ASSIGNED_BY_ID": "22",
            "DATE_CREATE": "2026-02-01T10:00:00+05:00",
        },
        {
            "ID": "11",
            "TITLE": "first",
            "COMPANY_ID": "10",
            "ASSIGNED_BY_ID": "16",
            "DATE_CREATE": "2026-01-01T10:00:00+05:00",
        },
    ]

    row = build_audit_rows(companies, leads)[0]

    assert row["first_lead_id"] == 11
    assert row["first_lead_owner_id"] == 16
    assert row["needs_update"] == "Y"


def test_company_already_matching_first_lead_is_not_updated():
    companies = [{"ID": "10", "TITLE": "A", "ASSIGNED_BY_ID": "16"}]
    leads = [
        {
            "ID": "11",
            "COMPANY_ID": "10",
            "ASSIGNED_BY_ID": "16",
            "DATE_CREATE": "2026-01-01T10:00:00+05:00",
        }
    ]

    row = build_audit_rows(companies, leads)[0]

    assert row["needs_update"] == "N"
    assert row["action"] == "already_matches"


def test_three_or_more_distinct_owners_are_counted_independently_of_company_owner():
    companies = [{"ID": "10", "TITLE": "A", "ASSIGNED_BY_ID": "16"}]
    leads = [
        {"ID": "11", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "16", "DATE_CREATE": "2026-01-01"},
        {"ID": "12", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "17", "DATE_CREATE": "2026-01-02"},
        {"ID": "13", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "18", "DATE_CREATE": "2026-01-03"},
        {"ID": "14", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "18", "DATE_CREATE": "2026-01-04"},
    ]

    row = build_audit_rows(companies, leads)[0]

    assert row["unique_owner_count"] == 3
    assert row["lead_count"] == 4
    assert row["needs_update"] == "N"


def test_company_without_linked_leads_is_not_in_audit():
    companies = [{"ID": "10", "TITLE": "A", "ASSIGNED_BY_ID": "16"}]

    assert build_audit_rows(companies, []) == []


def test_first_lead_without_owner_never_becomes_update_target():
    companies = [{"ID": "10", "TITLE": "A", "ASSIGNED_BY_ID": "16"}]
    leads = [
        {"ID": "11", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "", "DATE_CREATE": "2026-01-01"},
        {"ID": "12", "COMPANY_ID": "10", "ASSIGNED_BY_ID": "17", "DATE_CREATE": "2026-01-02"},
    ]

    row = build_audit_rows(companies, leads)[0]

    assert row["first_lead_id"] == 11
    assert row["first_lead_owner_id"] == ""
    assert row["needs_update"] == "N"
    assert row["action"] == "skipped_first_lead_no_owner"

class FakeClient:
    def __init__(self):
        self.updates = []

    def update_company(self, company_id, fields):
        self.updates.append((company_id, fields))


def test_apply_updates_changes_only_mismatches_to_first_lead_owner():
    from sync_company_owners import apply_updates

    rows = [
        {
            "company_id": 10,
            "first_lead_owner_id": 16,
            "needs_update": "Y",
            "action": "pending_update",
            "error": "",
        },
        {
            "company_id": 20,
            "first_lead_owner_id": 17,
            "needs_update": "N",
            "action": "already_matches",
            "error": "",
        },
    ]
    client = FakeClient()

    apply_updates(client, rows)

    assert client.updates == [("10", {"ASSIGNED_BY_ID": 16})]
    assert rows[0]["action"] == "updated"
    assert rows[1]["action"] == "already_matches"
