import pytest

from eqazyna_bitrix.distribution import AssignmentUser, DistributionSnapshot
from reassign_excluded_users import (
    ReassignmentError,
    assign_targets,
    build_change_rows,
    build_owner_groups,
    parse_excluded_user_ids,
    verify_changes,
)


def company(company_id, owner, title="Компания", address="Алматы", origin_id=""):
    return {
        "ID": str(company_id), "TITLE": title, "ASSIGNED_BY_ID": str(owner),
        "ADDRESS": address, "ORIGIN_ID": origin_id,
    }


def director(contact_id, company_id, owner, last="Иванов", name="Иван", second="Иванович"):
    return {
        "ID": str(contact_id), "COMPANY_ID": str(company_id),
        "ASSIGNED_BY_ID": str(owner), "LAST_NAME": last, "NAME": name,
        "SECOND_NAME": second, "POST": "Руководитель", "COMMENTS": "",
    }


def lead(lead_id, company_id, contact_id, owner, status="CONVERTED", semantic="S"):
    return {
        "ID": str(lead_id), "TITLE": f"Лид {lead_id}", "COMPANY_ID": str(company_id),
        "CONTACT_ID": str(contact_id), "ASSIGNED_BY_ID": str(owner),
        "STATUS_ID": status, "STATUS_SEMANTIC_ID": semantic,
    }


def snapshot(*users, fixes=None):
    return DistributionSnapshot(tuple(users), fixes or {})


def user(user_id, department=10, role="Менеджер"):
    return AssignmentUser(user_id, f"User {user_id}", department, "Филиал", role)


def test_parse_ids_accepts_commas_spaces_and_semicolons():
    assert parse_excluded_user_ids("17, 42;86") == {17, 42, 86}


def test_all_founder_companies_and_closed_leads_get_one_owner_and_new_status():
    companies = [company(1, 900), company(2, 77)]
    contacts = [director(11, 1, 900), director(12, 2, 77)]
    leads = [lead(101, 1, 11, 900), lead(102, 2, 12, 77, status="JUNK", semantic="F")]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assert len(groups) == 1
    assert groups[0].company_ids == {1, 2}
    assert groups[0].lead_ids == {101, 102}

    assign_targets(
        groups, snapshot(user(77)), companies, contacts, leads, {}, {900}, {900: {10}}
    )
    rows = build_change_rows(groups, companies, contacts, leads)
    assert {row["new_owner_id"] for row in rows} == {77}
    lead_rows = [row for row in rows if row["entity_type"] == "lead"]
    assert {row["new_status_id"] for row in lead_rows} == {"NEW"}
    assert {row["old_status_id"] for row in lead_rows} == {"CONVERTED", "JUNK"}


def test_company_fix_has_priority():
    companies = [company(1, 900, origin_id="123456789012")]
    contacts = [director(11, 1, 900)]
    leads = [lead(101, 1, 11, 900)]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assign_targets(
        groups, snapshot(user(77), user(88), fixes={"123456789012": 88}),
        companies, contacts, leads, {}, {900}, {900: {10}},
    )
    assert groups[0].target_owner_id == 88
    assert groups[0].assignment_reason == "company_fix"


def test_excluded_user_must_not_remain_in_user_list():
    companies = [company(1, 900)]
    contacts = [director(11, 1, 900)]
    leads = [lead(101, 1, 11, 900)]
    groups = build_owner_groups(companies, contacts, leads, {900})
    with pytest.raises(ReassignmentError, match="всё ещё присутствуют"):
        assign_targets(
            groups, snapshot(user(900)), companies, contacts, leads, {}, {900}, {900: {10}}
        )


def test_every_new_founder_goes_to_different_manager_then_rop():
    companies = [company(index, 900) for index in (1, 2, 3)]
    contacts = [
        director(10 + index, index, 900, last=f"Founder{index}") for index in (1, 2, 3)
    ]
    leads = [lead(100 + index, index, 10 + index, 900) for index in (1, 2, 3)]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assign_targets(
        groups,
        snapshot(user(77), user(88), user(99, role="РОП")),
        companies, contacts, leads, {}, {900}, {900: {10}},
    )
    targets = [group.target_owner_id for group in groups]
    assert set(targets[:2]) == {77, 88}
    assert targets[2] == 99


def test_no_seed_leads_is_blocking():
    with pytest.raises(ReassignmentError, match="не найдено ни одного лида"):
        build_owner_groups([], [], [], {900})


def test_verification_rejects_lead_that_did_not_move_to_new():
    rows = [{
        "entity_type": "lead", "entity_id": 101, "new_owner_id": 77,
        "action": "updated", "error": "",
    }]
    verify_changes(
        rows, [], [],
        [{"ID": "101", "ASSIGNED_BY_ID": "77", "STATUS_ID": "JUNK"}],
    )
    assert rows[0]["action"] == "verify_error"


def test_multiple_founders_of_one_company_are_merged_into_one_package():
    companies = [company(1, 900)]
    contacts = [
        director(11, 1, 900, last="Иванов"),
        director(12, 1, 900, last="Петров"),
    ]
    leads = [lead(101, 1, 11, 900), lead(102, 1, 12, 900)]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assert len(groups) == 1
    assert groups[0].company_ids == {1}
    assert groups[0].contact_ids == {11, 12}
    assert groups[0].lead_ids == {101, 102}
    assert groups[0].key.startswith("founders:")
