from pathlib import Path

from sync_founder_packages import build_packages, build_update_rows, person_from_text


def contact(item_id, fio, owner, company_id="", modified="2026-01-01"):
    last, first, *middle = fio.split()
    return {"ID": str(item_id), "LAST_NAME": last, "NAME": first, "SECOND_NAME": " ".join(middle), "POST": "Руководитель", "COMMENTS": "", "COMPANY_ID": str(company_id), "ASSIGNED_BY_ID": str(owner), "DATE_MODIFY": modified}


def company(item_id, owner):
    return {"ID": str(item_id), "TITLE": f"Компания {item_id}", "ASSIGNED_BY_ID": str(owner)}


def lead(item_id, company_id, owner, contact_id=""):
    return {"ID": str(item_id), "TITLE": f"Лид {item_id}", "COMPANY_ID": str(company_id), "CONTACT_ID": str(contact_id), "ASSIGNED_BY_ID": str(owner)}


def requisite(company_id, fio):
    return {"ID": str(company_id), "ENTITY_ID": str(company_id), "ENTITY_TYPE_ID": "4", "RQ_DIRECTOR": fio}


def test_unlinked_company_is_joined_by_requisite_fio():
    packages, skipped = build_packages([company(10, 9)], [lead(1, 10, 9)], [contact(100, "Иванов Иван Иванович", 17)], [requisite(10, "Иванов Иван Иванович")])
    assert skipped == []
    assert [item["id"] for item in packages[0]["companies"]] == [10]
    rows = build_update_rows(packages)
    assert {(row["entity"], row["id"], row["target"]) for row in rows} == {("company", 10, 17), ("lead", 1, 17)}


def test_two_companies_of_same_founder_form_one_package():
    packages, _ = build_packages([company(10, 9), company(20, 8)], [lead(1, 10, 9), lead(2, 20, 8)], [contact(100, "Иванов Иван Иванович", 17, 10)], [requisite(10, "Иванов Иван Иванович"), requisite(20, "Иванов Иван Иванович")])
    assert len(packages) == 1
    assert {item["id"] for item in packages[0]["companies"]} == {10, 20}
    assert {row["target"] for row in build_update_rows(packages)} == {17}


def test_patronymic_optional_when_base_is_unambiguous():
    packages, _ = build_packages([company(10, 9)], [], [contact(100, "Иванов Иван Иванович", 17)], [requisite(10, "Иванов Иван")])
    assert len(packages) == 1
    assert packages[0]["companies"][0]["id"] == 10


def test_missing_patronymic_is_blocked_when_two_people_share_base():
    packages, skipped = build_packages([company(10, 9)], [], [contact(100, "Иванов Иван Иванович", 17), contact(200, "Иванов Иван Петрович", 18)], [requisite(10, "Иванов Иван")])
    assert all(not item["companies"] for item in packages)
    assert any(item["type"] == "missing_patronymic_is_ambiguous" for item in skipped)


def test_latest_changed_contact_is_source_and_other_contact_is_synced():
    packages, _ = build_packages([company(10, 9), company(20, 8)], [], [contact(100, "Иванов Иван Иванович", 17, 10, "2026-01-01"), contact(200, "Иванов Иван Иванович", 18, 20, "2026-02-01")], [requisite(10, "Иванов Иван Иванович"), requisite(20, "Иванов Иван Иванович")])
    assert packages[0]["owner_id"] == 18
    assert ("contact", 100, 18) in {(row["entity"], row["id"], row["target"]) for row in build_update_rows(packages)}


def test_fio_requires_last_and_first_name():
    assert person_from_text("Иванов") is None


def test_workflow_runs_founder_package_module_only():
    workflow = (
        Path(__file__).resolve().parents[3] / ".github" / "workflows" / "31-company-owner-sync.yml"
    ).read_text(encoding="utf-8")
    assert "sync_founder_packages.py" in workflow
    assert "sync_company_owners.py" not in workflow
