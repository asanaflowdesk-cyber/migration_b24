from __future__ import annotations

from sync_company_owners import (
    apply_updates,
    build_company_update_rows,
    build_desync_tree,
)


def company(company_id, title, owner):
    return {"ID": str(company_id), "TITLE": title, "ASSIGNED_BY_ID": str(owner) if owner else ""}


def director(contact_id, fio, company_id, owner):
    last, name, *rest = fio.split(" ")
    return {
        "ID": str(contact_id),
        "LAST_NAME": last,
        "NAME": name,
        "SECOND_NAME": " ".join(rest),
        "POST": "Руководитель",
        "COMPANY_ID": str(company_id),
        "ASSIGNED_BY_ID": str(owner) if owner else "",
        "COMMENTS": "",
    }


def lead(lead_id, title, company_id, contact_id, owner, date="2026-01-01"):
    return {
        "ID": str(lead_id),
        "TITLE": title,
        "COMPANY_ID": str(company_id),
        "CONTACT_ID": str(contact_id),
        "ASSIGNED_BY_ID": str(owner) if owner else "",
        "DATE_CREATE": date,
    }


def test_same_director_across_two_companies_is_one_tree():
    companies = [company(10, "Компания 1", 17), company(20, "Компания 2", 18)]
    contacts = [
        director(100, "Иванов Иван Иванович", 10, 17),
        director(200, "Иванов Иван Иванович", 20, 17),
    ]
    leads = [
        lead(1, "Лид 1", 10, 100, 17),
        lead(2, "Лид 2", 20, 200, 18),
    ]

    tree = build_desync_tree(companies, leads, contacts)

    assert len(tree) == 1
    assert tree[0]["director_name"] == "Иванов Иван Иванович"
    assert tree[0]["director_owner_id"] == 17
    assert {node["company_title"] for node in tree[0]["companies"]} == {"Компания 1", "Компания 2"}


def test_fully_synchronized_director_tree_is_not_reported():
    companies = [company(10, "Компания 1", 17)]
    contacts = [director(100, "Иванов Иван Иванович", 10, 17)]
    leads = [lead(1, "Лид 1", 10, 100, 17)]

    assert build_desync_tree(companies, leads, contacts) == []


def test_one_bad_lead_includes_whole_director_tree_for_context():
    companies = [company(10, "Компания 1", 17), company(20, "Компания 2", 17)]
    contacts = [
        director(100, "Иванов Иван Иванович", 10, 17),
        director(200, "Иванов Иван Иванович", 20, 17),
    ]
    leads = [
        lead(1, "Лид 1", 10, 100, 17),
        lead(2, "Лид 2", 20, 200, 99),
        lead(3, "Лид 3", 20, 200, 17),
    ]

    tree = build_desync_tree(companies, leads, contacts)

    assert len(tree) == 1
    assert len(tree[0]["companies"]) == 2
    second = next(node for node in tree[0]["companies"] if node["company_id"] == 20)
    assert [item["lead_title"] for item in second["leads"]] == ["Лид 2", "Лид 3"]
    assert [item["lead_mismatch"] for item in second["leads"]] == [True, False]


def test_company_owner_is_compared_to_director_owner_not_first_lead():
    companies = [company(10, "Компания 1", 99)]
    contacts = [director(100, "Иванов Иван Иванович", 10, 17)]
    # First lead happens to be 99, but the director owner 17 is authoritative.
    leads = [lead(1, "Лид 1", 10, 100, 99)]

    tree = build_desync_tree(companies, leads, contacts)
    node = tree[0]["companies"][0]

    assert tree[0]["director_owner_id"] == 17
    assert node["company_mismatch"] is True
    assert node["leads"][0]["lead_mismatch"] is True


def test_oldest_director_contact_with_owner_is_canonical_for_same_fio():
    companies = [company(10, "Компания 1", 17), company(20, "Компания 2", 18)]
    contacts = [
        director(100, "Иванов Иван Иванович", 10, 17),
        director(200, "Иванов Иван Иванович", 20, 18),
    ]
    leads = []

    tree = build_desync_tree(companies, leads, contacts)

    assert len(tree) == 1
    assert tree[0]["director_owner_id"] == 17
    assert tree[0]["director_contact_owner_count"] == 2


class FakeClient:
    def __init__(self):
        self.updates = []

    def update_company(self, company_id, fields):
        self.updates.append((company_id, fields))


def test_apply_updates_company_to_director_owner():
    tree = [
        {
            "director_owner_id": 17,
            "companies": [
                {
                    "company_id": 10,
                    "company_owner_id": 99,
                    "unique_lead_owner_count": 1,
                }
            ],
        }
    ]
    rows = build_company_update_rows(tree)
    client = FakeClient()

    apply_updates(client, rows)

    assert client.updates == [("10", {"ASSIGNED_BY_ID": 17})]
    assert rows[0]["action"] == "updated"


def test_apply_skips_company_with_three_or_more_distinct_lead_owners():
    tree = [
        {
            "director_owner_id": 17,
            "companies": [
                {
                    "company_id": 30,
                    "company_owner_id": 99,
                    "unique_lead_owner_count": 3,
                }
            ],
        }
    ]
    rows = build_company_update_rows(tree)
    client = FakeClient()

    apply_updates(client, rows)

    assert client.updates == []
    assert rows[0]["action"] == "manual_review_3plus"


def test_apply_never_changes_leads_or_contacts():
    tree = [
        {
            "director_owner_id": 17,
            "companies": [
                {
                    "company_id": 10,
                    "company_owner_id": 99,
                    "unique_lead_owner_count": 2,
                }
            ],
        }
    ]
    rows = build_company_update_rows(tree)
    client = FakeClient()

    apply_updates(client, rows)

    assert client.updates == [("10", {"ASSIGNED_BY_ID": 17})]
