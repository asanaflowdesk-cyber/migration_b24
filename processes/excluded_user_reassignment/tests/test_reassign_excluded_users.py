import pytest

from reassign_excluded_users import (
    OwnerGroup,
    ReassignmentError,
    assign_targets,
    build_change_rows,
    build_owner_groups,
    collect_linkage_issues,
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


def linked_contact(contact_id, company_id, owner, last="Иванов", name="Иван", second="Иванович"):
    row = director(contact_id, company_id, owner, last, name, second)
    row["POST"] = ""
    row["COMMENTS"] = ""
    return row


def lead(lead_id, company_id, contact_id, owner, status="CONVERTED", semantic="S"):
    return {
        "ID": str(lead_id), "TITLE": f"Лид {lead_id}", "COMPANY_ID": str(company_id),
        "CONTACT_ID": str(contact_id), "ASSIGNED_BY_ID": str(owner),
        "STATUS_ID": status, "STATUS_SEMANTIC_ID": semantic,
    }


def test_parse_ids_accepts_commas_spaces_and_semicolons():
    assert parse_excluded_user_ids("17, 42;86") == {17, 42, 86}


def test_foreign_owner_entities_in_same_founder_package_are_protected():
    companies = [company(1, 900), company(2, 77)]
    contacts = [director(11, 1, 900), director(12, 2, 77)]
    leads = [lead(101, 1, 11, 900), lead(102, 2, 12, 77, status="JUNK", semantic="F")]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assert len(groups) == 1
    assert groups[0].company_ids == {1, 2}
    assert groups[0].lead_ids == {101}

    assign_targets(groups, (72, 73))
    rows = build_change_rows(
        groups, companies, contacts, leads, excluded_user_ids={900}
    )
    assert {row["new_owner_id"] for row in rows} == {72}
    lead_rows = [row for row in rows if row["entity_type"] == "lead"]
    assert {row["entity_id"] for row in lead_rows} == {101}
    assert {row["new_status_id"] for row in lead_rows} == {"NEW"}
    assert {row["entity_id"] for row in rows if row["entity_type"] == "company"} == {1}
    assert {row["entity_id"] for row in rows if row["entity_type"] == "contact"} == {11}


def test_only_explicitly_excluded_owners_are_changed_in_shared_package():
    protected_owner_ids = {13, 16, 18, 38, 40, 58}
    companies = [company(1, 900)]
    contacts = [director(11, 1, 900)]
    leads = [lead(101, 1, 11, 900)] + [
        lead(200 + owner_id, 1, 11, owner_id, status="JUNK", semantic="F")
        for owner_id in sorted(protected_owner_ids)
    ]

    groups = build_owner_groups(companies, contacts, leads, {900})
    assign_targets(groups, (72, 73))
    rows = build_change_rows(
        groups, companies, contacts, leads, excluded_user_ids={900}
    )

    lead_rows = [row for row in rows if row["entity_type"] == "lead"]
    assert {row["entity_id"] for row in lead_rows} == {101}
    assert all(row["old_owner_id"] == 900 for row in rows)
    assert lead_rows[0]["new_status_id"] == "NEW"


def test_packages_are_balanced_by_lead_count_between_rops():
    groups = [
        OwnerGroup("a", {900}, set(), set(), set(range(1, 6))),
        OwnerGroup("b", {900}, set(), set(), set(range(10, 14))),
        OwnerGroup("c", {900}, set(), set(), set(range(20, 23))),
        OwnerGroup("d", {900}, set(), set(), set(range(30, 32))),
    ]
    assign_targets(groups, (72, 73))
    totals = {
        rop_id: sum(len(group.lead_ids) for group in groups if group.target_owner_id == rop_id)
        for rop_id in (72, 73)
    }
    assert totals == {72: 7, 73: 7}
    assert {group.assignment_reason for group in groups} == {"balanced_between_rops_72_73"}


def test_exactly_two_rops_are_required():
    with pytest.raises(ReassignmentError, match="ровно два"):
        assign_targets([], (72,))


def test_no_seed_leads_is_blocking():
    with pytest.raises(ReassignmentError, match="не найдено ни одного лида"):
        build_owner_groups([], [], [], {900})


def test_company_branch_does_not_change_fixed_rop_pool():
    companies = [company(224, 900, address="Талдыкорган")]
    contacts = [director(11, 224, 900)]
    leads = [lead(101, 224, 11, 900)]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assign_targets(groups, (72, 73))
    assert groups[0].target_owner_id in {72, 73}
    assert groups[0].assignment_reason == "balanced_between_rops_72_73"


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


def test_founders_are_merged_when_shared_company_exists_only_in_lead_links():
    companies = [company(391, 900), company(501, 900), company(502, 900)]
    contacts = [
        director(11, 501, 900, last="Қабдрашев", name="Дидар", second="Айдарханұлы"),
        director(12, 502, 900, last="Есенбаев", name="Мурат", second="Оразалынович"),
    ]
    leads = [lead(101, 391, 11, 900), lead(102, 391, 12, 900)]

    groups = build_owner_groups(companies, contacts, leads, {900})

    assert len(groups) == 1
    assert groups[0].company_ids == {391, 501, 502}
    assert groups[0].contact_ids == {11, 12}
    assert groups[0].lead_ids == {101, 102}
    assert "қабдрашев" in groups[0].key
    assert "есенбаев" in groups[0].key

    assign_targets(groups, (72, 73))
    rows = build_change_rows(
        groups, companies, contacts, leads, excluded_user_ids={900}
    )
    assert len({row["new_owner_id"] for row in rows}) == 1
    assert {row["new_owner_id"] for row in rows} <= {72, 73}
    assert {row["new_status_id"] for row in rows if row["entity_type"] == "lead"} == {"NEW"}


def test_contact_linked_directly_to_lead_is_founder_without_service_marker():
    companies = [company(1, 900, origin_id="123456789012")]
    contacts = [linked_contact(11, 1, 900, last="Петров", name="Пётр")]
    leads = [lead(101, 1, 11, 900)]

    groups = build_owner_groups(companies, contacts, leads, {900})

    assert len(groups) == 1
    assert groups[0].key == "fio:петров|петр|иванович"
    assert groups[0].contact_ids == {11}
    assert groups[0].company_ids == {1}


def test_linkage_validation_reports_missing_company_and_contact():
    leads = [lead(101, "", "", 900)]

    issues = collect_linkage_issues([], [], leads, {101})

    assert len(issues) == 1
    assert issues[0]["action"] == "skipped_missing_company"
    assert "COMPANY_ID" in issues[0]["error"]
    assert "CONTACT_ID" in issues[0]["error"]


def test_lead_without_founder_is_skipped_without_moving_its_company():
    companies = [company(1, 900), company(2, 900)]
    contacts = [linked_contact(12, 2, 900, last="Петров", name="Пётр")]
    leads = [
        lead(101, 1, "", 900),
        lead(102, 2, 12, 900),
    ]
    issues = collect_linkage_issues(companies, contacts, leads, {101, 102})
    assert len(issues) == 1
    assert issues[0]["lead_id"] == 101
    assert issues[0]["action"] == "skipped_missing_founder"

    groups = build_owner_groups(
        companies, contacts, leads, {900}, skip_lead_ids={101}
    )

    assert len(groups) == 1
    assert groups[0].lead_ids == {102}
    assert groups[0].company_ids == {2}


def test_all_incomplete_leads_may_be_skipped_without_blocking():
    leads = [lead(101, "", "", 900)]

    groups = build_owner_groups(
        [], [], leads, {900}, skip_lead_ids={101}
    )

    assert groups == []


def test_extended_report_contains_decision_fields_on_lead_row():
    companies = [company(1, 900, title="ТОО Тест", origin_id="123456789012")]
    contacts = [linked_contact(11, 1, 900, last="Петров", name="Пётр")]
    leads = [lead(101, 1, 11, 900, status="JUNK")]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assign_targets(groups, (72, 73))

    rows = build_change_rows(
        groups, companies, contacts, leads,
        excluded_user_ids={900},
        user_names={900: "Старый Менеджер", 72: "РОП 72"},
        status_names={"JUNK": "Некачественный", "NEW": "Новый лид"},
    )
    lead_row = next(row for row in rows if row["entity_type"] == "lead")

    assert lead_row["source_excluded_user_ids"] == "900"
    assert lead_row["founder_names"] == "Петров Пётр Иванович"
    assert lead_row["lead_company_id"] == 1
    assert lead_row["lead_company_bin"] == "123456789012"
    assert lead_row["lead_contact_id"] == 11
    assert lead_row["lead_founder_name"] == "Петров Пётр Иванович"
    assert lead_row["old_status_semantic_id"] == "S"
    assert lead_row["old_owner_name"] == "Старый Менеджер"
    assert lead_row["new_owner_name"] == "РОП 72"
    assert lead_row["old_status_name"] == "Некачественный"
    assert lead_row["new_status_name"] == "Новый лид"
