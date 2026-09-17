from reconcile_company_lead_owners import (
    company_lead_items,
    is_director_authority_contact,
    reconcile_package,
    resolve_authority_packages,
)


def contact(item_id, owner, post="Руководитель"):
    return {
        "ID": str(item_id),
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "POST": post,
        "COMMENTS": "",
        "COMPANY_ID": "10",
        "ASSIGNED_BY_ID": "" if owner is None else str(owner),
    }


def package(contact_ids=(100,), company_owner=9, lead_owner=8):
    return {
        "fio": "Иванов Иван Иванович",
        "owner_id": 999,
        "source_contact_id": 999,
        "contacts": [
            {"id": value, "title": "Иванов Иван Иванович", "owner_id": 999}
            for value in contact_ids
        ],
        "companies": [
            {
                "id": 10,
                "title": "Компания 10",
                "owner_id": company_owner,
                "leads": [
                    {"id": 20, "title": "Лид 20", "owner_id": lead_owner},
                ],
            }
        ],
    }


class Client:
    def __init__(self, contacts, company_owner=9, lead_owner=8):
        self.contacts = {int(item["ID"]): dict(item) for item in contacts}
        self.companies = {10: {"ID": "10", "ASSIGNED_BY_ID": str(company_owner)}}
        self.leads = {20: {"ID": "20", "ASSIGNED_BY_ID": str(lead_owner)}}
        self.updated = []

    def call(self, method, payload):
        if method == "batch":
            raise RuntimeError("batch unavailable in unit double")
        entity = method.split(".")[1]
        item_id = int(payload["id"])
        if entity == "contact":
            return dict(self.contacts[item_id])
        if entity == "company":
            return dict(self.companies[item_id])
        if entity == "lead":
            return dict(self.leads[item_id])
        raise AssertionError(method)

    def update_company(self, item_id, fields):
        self.companies[int(item_id)].update(fields)
        self.updated.append(("company", int(item_id), int(fields["ASSIGNED_BY_ID"])))
        return True

    def update_lead(self, item_id, fields):
        self.leads[int(item_id)].update(fields)
        self.updated.append(("lead", int(item_id), int(fields["ASSIGNED_BY_ID"])))
        return True


def test_only_director_contact_is_authority():
    assert is_director_authority_contact(contact(100, 17, "Руководитель"))
    assert is_director_authority_contact(contact(100, 17, "Директор"))
    assert not is_director_authority_contact(contact(100, 17, "Учредитель"))


def test_one_director_owner_becomes_target_without_date_modify_logic():
    packages, skipped = resolve_authority_packages([package()], [contact(100, 17)])
    assert skipped == []
    assert packages[0]["owner_id"] == 17
    assert packages[0]["source_contact_id"] == 100
    assert packages[0]["authority_contact_ids"] == [100]


def test_multiple_director_contacts_are_safe_only_when_owner_is_same():
    packages, skipped = resolve_authority_packages(
        [package((100, 200))],
        [contact(100, 17), contact(200, 17)],
    )
    assert skipped == []
    assert packages[0]["owner_id"] == 17
    assert packages[0]["authority_contact_ids"] == [100, 200]


def test_different_director_owners_are_never_guessed():
    packages, skipped = resolve_authority_packages(
        [package((100, 200))],
        [contact(100, 17), contact(200, 18)],
    )
    assert packages == []
    assert skipped[0]["type"] == "director_contacts_have_different_owners"


def test_company_lead_plan_never_contains_contact_updates():
    items = company_lead_items(package((100, 200)))
    assert {(item["entity"], item["id"]) for item in items} == {
        ("company", 10),
        ("lead", 20),
    }


def test_apply_mirrors_company_and_lead_to_live_director_owner():
    source = package()
    source["authority_contact_ids"] = [100]
    client = Client([contact(100, 17)], company_owner=9, lead_owner=8)

    rows, error = reconcile_package(client, source, apply=True)

    assert error == ""
    assert client.companies[10]["ASSIGNED_BY_ID"] == 17
    assert client.leads[20]["ASSIGNED_BY_ID"] == 17
    assert {item[0] for item in client.updated} == {"company", "lead"}
    assert all(row["target"] == 17 for row in rows)
    assert all(row["status"] == "updated" for row in rows)


def test_dry_run_does_not_write():
    source = package()
    source["authority_contact_ids"] = [100]
    client = Client([contact(100, 17)], company_owner=9, lead_owner=8)

    rows, error = reconcile_package(client, source, apply=False)

    assert error == ""
    assert client.updated == []
    assert {(row["entity"], row["status"]) for row in rows} == {
        ("company", "planned"),
        ("lead", "planned"),
    }
