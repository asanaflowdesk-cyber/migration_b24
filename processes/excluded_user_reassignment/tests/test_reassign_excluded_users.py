import pytest

from reassign_excluded_users import (
    OwnerGroup,
    ReassignmentError,
    assign_targets,
    build_change_rows,
    build_owner_groups,
    collect_linkage_issues,
    parse_excluded_user_ids,
    prepare_lead_statuses,
    apply_owner_changes,
    split_reassignment_groups,
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




class RecordingClient:
    def __init__(self):
        self.calls = []

    def update_lead(self, entity_id, fields):
        self.calls.append(("lead", str(entity_id), dict(fields)))

    def update_company(self, entity_id, fields):
        self.calls.append(("company", str(entity_id), dict(fields)))

    def update_contact(self, entity_id, fields):
        self.calls.append(("contact", str(entity_id), dict(fields)))


def test_lead_status_is_prepared_before_owner_reassignment():
    client = RecordingClient()
    rows = [
        {
            "entity_type": "lead", "entity_id": 101, "new_owner_id": 72,
            "old_status_id": "JUNK", "action": "pending", "error": "",
        },
        {
            "entity_type": "company", "entity_id": 201, "new_owner_id": 72,
            "old_status_id": "", "action": "pending", "error": "",
        },
    ]

    status_result = prepare_lead_statuses(client, rows)
    assert status_result == {"planned": 1, "updated": 1, "errors": 0}
    assert client.calls == [("lead", "101", {"STATUS_ID": "NEW"})]
    assert rows[0]["action"] == "pending"

    owner_result = apply_owner_changes(client, rows)
    assert owner_result == {"planned": 2, "updated": 2, "errors": 0}
    assert client.calls[1:] == [
        ("lead", "101", {"ASSIGNED_BY_ID": 72}),
        ("company", "201", {"ASSIGNED_BY_ID": 72}),
    ]
    assert all(row["action"] == "updated" for row in rows)


def test_lead_already_new_skips_status_preparation_but_still_moves_owner():
    client = RecordingClient()
    rows = [{
        "entity_type": "lead", "entity_id": 101, "new_owner_id": 73,
        "old_status_id": "NEW", "action": "pending", "error": "",
    }]

    assert prepare_lead_statuses(client, rows) == {"planned": 0, "updated": 0, "errors": 0}
    assert client.calls == []
    assert apply_owner_changes(client, rows) == {"planned": 1, "updated": 1, "errors": 0}
    assert client.calls == [("lead", "101", {"ASSIGNED_BY_ID": 73})]


def test_parse_ids_accepts_commas_spaces_and_semicolons():
    assert parse_excluded_user_ids("17, 42;86") == {17, 42, 86}


def test_foreign_owner_in_same_founder_package_skips_whole_package():
    companies = [company(1, 900), company(2, 77)]
    contacts = [director(11, 1, 900), director(12, 2, 77)]
    leads = [lead(101, 1, 11, 900), lead(102, 2, 12, 77, status="JUNK", semantic="F")]
    groups = build_owner_groups(companies, contacts, leads, {900})
    assert len(groups) == 1
    assert groups[0].company_ids == {1, 2}
    assert groups[0].lead_ids == {101}

    eligible, skipped = split_reassignment_groups(
        groups, companies, contacts, leads, {900}
    )

    assert eligible == []
    assert len(skipped) == 1
    assert skipped[0]["lead_id"] == 101
    assert "skipped_existing_owner" in skipped[0]["action"]
    assert "77" in skipped[0]["error"]


def test_any_non_excluded_lead_owner_protects_entire_package():
    protected_owner_ids = {13, 16, 18, 38, 40, 58}
    companies = [company(1, 900)]
    contacts = [director(11, 1, 900)]
    leads = [lead(101, 1, 11, 900)] + [
        lead(200 + owner_id, 1, 11, owner_id, status="JUNK", semantic="F")
        for owner_id in sorted(protected_owner_ids)
    ]

    groups = build_owner_groups(companies, contacts, leads, {900})
    eligible, skipped = split_reassignment_groups(
        groups, companies, contacts, leads, {900}
    )

    assert eligible == []
    assert len(skipped) == 1
    assert skipped[0]["lead_id"] == 101
    assert "skipped_existing_owner" in skipped[0]["action"]
    for owner_id in protected_owner_ids:
        assert str(owner_id) in skipped[0]["error"]


def test_missing_founder_becomes_short_company_lead_package():
    companies = [company(1, 900)]
    contacts = []
    leads = [lead(101, 1, 0, 900)]
    leads[0]["CONTACT_ID"] = ""

    groups = build_owner_groups(companies, contacts, leads, {900})

    assert len(groups) == 1
    assert groups[0].key == "company:1"
    assert groups[0].company_ids == {1}
    assert groups[0].lead_ids == {101}
    assert groups[0].contact_ids == set()
    assert "Учредитель не определён" in groups[0].warning


def test_missing_company_and_founder_becomes_single_lead_package():
    companies = []
    contacts = []
    row = lead(101, 0, 0, 900)
    row["COMPANY_ID"] = ""
    row["CONTACT_ID"] = ""

    groups = build_owner_groups(companies, contacts, [row], {900})

    assert len(groups) == 1
    assert groups[0].key == "lead:101"
    assert groups[0].company_ids == set()
    assert groups[0].contact_ids == set()
    assert groups[0].lead_ids == {101}


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


def test_any_taldykorgan_package_is_skipped():
    companies = [company(224, 900)]
    contacts = [director(11, 224, 900)]
    leads = [lead(101, 224, 11, 900)]
    groups = build_owner_groups(companies, contacts, leads, {900})

    eligible, skipped = split_reassignment_groups(
        groups, companies, contacts, leads, {900}, {900: {"УП г. Талдыкорган"}}
    )

    assert eligible == []
    assert len(skipped) == 1
    assert skipped[0]["lead_id"] == 101
    assert "skipped_taldykorgan" in skipped[0]["action"]


def test_taldykorgan_plus_other_branch_skips_whole_package():
    companies = [company(224, 900), company(225, 901)]
    contacts = [
        director(11, 224, 900, last="Иванов"),
        director(12, 225, 901, last="Иванов"),
    ]
    leads = [lead(101, 224, 11, 900), lead(102, 225, 12, 901)]
    groups = build_owner_groups(companies, contacts, leads, {900, 901})
    assert len(groups) == 1

    eligible, skipped = split_reassignment_groups(
        groups,
        companies,
        contacts,
        leads,
        {900, 901},
        {900: {"УП г. Талдыкорган"}, 901: {"УП г. Алматы"}},
    )

    assert eligible == []
    assert {row["lead_id"] for row in skipped} == {101, 102}
    assert all("skipped_taldykorgan" in row["action"] for row in skipped)
    assert all("связан с Талдыкорганом" in row["error"] for row in skipped)


def test_company_address_does_not_define_branch_for_taldykorgan_rule():
    companies = [
        company(224, 900, address="Талдыкорган"),
        company(225, 901, address="Алматы"),
    ]
    contacts = [
        director(11, 224, 900, last="Иванов"),
        director(12, 225, 901, last="Иванов"),
    ]
    leads = [lead(101, 224, 11, 900), lead(102, 225, 12, 901)]
    groups = build_owner_groups(companies, contacts, leads, {900, 901})

    eligible, skipped = split_reassignment_groups(
        groups,
        companies,
        contacts,
        leads,
        {900, 901},
        {900: {"УП г. Алматы"}, 901: {"УП г. Алматы"}},
    )

    assert len(eligible) == 1
    assert skipped == []


def test_package_owned_only_by_excluded_users_can_be_redistributed():
    companies = [company(1, 900), company(2, 901)]
    contacts = [
        director(11, 1, 900, last="Иванов"),
        director(12, 2, 901, last="Иванов"),
    ]
    leads = [lead(101, 1, 11, 900), lead(102, 2, 12, 901)]
    groups = build_owner_groups(companies, contacts, leads, {900, 901})

    eligible, skipped = split_reassignment_groups(
        groups, companies, contacts, leads, {900, 901}
    )

    assert len(eligible) == 1
    assert eligible[0].lead_ids == {101, 102}
    assert skipped == []


def test_verification_keeps_owner_success_separate_from_status_robot_drift():
    rows = [{
        "entity_type": "lead", "entity_id": 101, "new_owner_id": 77,
        "action": "updated", "error": "",
    }]
    verify_changes(
        rows, [], [],
        [{"ID": "101", "ASSIGNED_BY_ID": "77", "STATUS_ID": "JUNK"}],
    )
    assert rows[0]["action"] == "updated"
    assert "STATUS_ID='JUNK'" in rows[0]["warning"]


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
