import pytest

from reassign_excluded_users import (
    Package,
    ReassignmentError,
    add_package_context,
    assign_targets,
    build_packages,
    build_rows,
    choose_existing_owner,
    package_taldyk_owners,
    parse_id_set,
    plan_packages,
    update_owner_safely,
)


def company(cid, owner, title="Компания"):
    return {"ID": str(cid), "TITLE": title, "ASSIGNED_BY_ID": str(owner), "ORIGIN_ID": ""}


def contact(cid, company_id, owner, last="Иванов", first="Иван", second="Иванович"):
    return {
        "ID": str(cid), "COMPANY_ID": str(company_id), "ASSIGNED_BY_ID": str(owner),
        "LAST_NAME": last, "NAME": first, "SECOND_NAME": second,
        "POST": "Руководитель", "COMMENTS": "",
    }


def lead(lid, company_id, contact_id, owner, status="JUNK"):
    return {
        "ID": str(lid), "TITLE": f"Лид {lid}", "COMPANY_ID": str(company_id) if company_id else "",
        "CONTACT_ID": str(contact_id) if contact_id else "", "ASSIGNED_BY_ID": str(owner),
        "STATUS_ID": status, "DATE_CREATE": "2026-09-11",
    }


def test_parse_ids():
    assert parse_id_set("15, 17;19", label="x") == {15, 17, 19}


def test_missing_founder_is_company_plus_leads_package():
    companies = [company(1, 15)]
    leads = [lead(101, 1, None, 15), lead(102, 1, None, 15)]
    packages = build_packages(companies, [], leads, {15})
    assert len(packages) == 1
    package = packages[0]
    assert package.key == "company:1"
    assert package.company_ids == {1}
    assert package.lead_ids == {101, 102}
    assert package.contact_ids == set()
    assert "сокращённый пакет" in package.warning


def test_missing_company_is_single_lead_package():
    packages = build_packages([], [], [lead(101, None, None, 15)], {15})
    assert len(packages) == 1
    assert packages[0].key == "lead:101"
    assert packages[0].lead_ids == {101}


def test_same_founder_across_companies_is_one_package():
    companies = [company(1, 15), company(2, 15)]
    contacts = [
        contact(11, 1, 15, last="Петров", first="Петр"),
        contact(12, 2, 15, last="Петров", first="Петр"),
    ]
    leads = [lead(101, 1, 11, 15), lead(102, 2, 12, 15)]
    packages = build_packages(companies, contacts, leads, {15})
    assert len(packages) == 1
    assert packages[0].company_ids == {1, 2}
    assert packages[0].contact_ids == {11, 12}
    assert packages[0].lead_ids == {101, 102}


def test_nonexcluded_lead_is_context_not_changed():
    companies = [company(1, 13)]
    contacts = [contact(11, 1, 13)]
    leads = [lead(101, 1, 11, 15), lead(102, 1, 11, 13)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    target, reason = choose_existing_owner(packages[0], companies, contacts, leads, {15}, (72, 73))
    assert target == 13
    assert reason.startswith("keep_existing")

    packages[0].target_owner_id = 13
    packages[0].target_reason = reason
    rows = build_rows(packages, [], companies, contacts, leads, {15}, {13: "Manager 13", 15: "Old"})
    lead_ids = {r["entity_id"] for r in rows if r["entity_type"] == "lead"}
    assert lead_ids == {101}
    assert next(r for r in rows if r["entity_type"] == "lead")["new_owner_id"] == 13


def test_existing_manual_owner_beats_partial_rop_assignment():
    companies = [company(1, 13)]
    contacts = [contact(11, 1, 72)]
    leads = [lead(101, 1, 11, 15), lead(102, 1, 11, 13)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    target, _ = choose_existing_owner(packages[0], companies, contacts, leads, {15}, (72, 73))
    assert target == 13


def test_partial_rop_package_keeps_existing_rop():
    companies = [company(1, 72)]
    contacts = [contact(11, 1, 72)]
    leads = [lead(101, 1, 11, 15)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    target, reason = choose_existing_owner(packages[0], companies, contacts, leads, {15}, (72, 73))
    assert target == 72
    assert reason == "continue_existing_rop_package"


def test_taldyk_package_is_skipped_even_if_other_branch_also_present():
    companies = [company(1, 15)]
    contacts = [contact(11, 1, 15)]
    leads = [lead(101, 1, 11, 15), lead(102, 1, 11, 13)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(
        packages, companies, contacts, leads, {15}, (72, 73),
        {15: {"Талдыкорганский филиал"}, 13: {"Алматинский филиал"}}, set(),
    )
    assert eligible == []
    assert len(skipped) == 1
    assert skipped[0].skip_reason.startswith("taldykorgan:")


def test_rop_department_is_ignored_for_taldyk_gate():
    package = Package("x", lead_ids={101}, context_owner_ids={15, 72})
    result = package_taldyk_owners(
        package,
        {15: {"Алматы"}, 72: {"Талдыкорганский филиал"}},
        {72, 73},
    )
    assert result == set()


def test_protected_lead_401_skips_whole_package():
    companies = [company(1, 15)]
    contacts = [contact(11, 1, 15)]
    leads = [lead(401, 1, 11, 15), lead(402, 1, 11, 15)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(
        packages, companies, contacts, leads, {15}, (72, 73), {15: {"Алматы"}}, {401}
    )
    assert eligible == []
    assert len(skipped) == 1
    assert skipped[0].skip_reason == "protected_lead:401"


def test_unassigned_packages_balance_by_lead_count():
    packages = [
        Package("a", lead_ids={1, 2, 3, 4}),
        Package("b", lead_ids={5, 6, 7}),
        Package("c", lead_ids={8, 9}),
        Package("d", lead_ids={10}),
    ]
    assign_targets(packages, (72, 73))
    loads = {
        rop: sum(len(p.lead_ids) for p in packages if p.target_owner_id == rop)
        for rop in (72, 73)
    }
    assert loads == {72: 5, 73: 5}


def test_build_rows_never_changes_status():
    companies = [company(1, 15)]
    contacts = [contact(11, 1, 15)]
    leads = [lead(101, 1, 11, 15, status="JUNK")]
    packages = build_packages(companies, contacts, leads, {15})
    packages[0].target_owner_id = 72
    packages[0].target_reason = "balanced_between_rops_72_73"
    rows = build_rows(packages, [], companies, contacts, leads, {15}, {15: "Old", 72: "ROP"})
    lead_row = next(r for r in rows if r["entity_type"] == "lead")
    assert lead_row["lead_status_id"] == "JUNK"
    assert "new_status_id" not in lead_row


class AmbiguousClient:
    def __init__(self):
        self.owner = 15
        self.update_calls = 0

    def call(self, method, payload):
        if method.endswith(".update"):
            self.update_calls += 1
            self.owner = int(payload["fields"]["ASSIGNED_BY_ID"])
            raise RuntimeError("timeout after commit")
        if method.endswith(".get"):
            return {"ASSIGNED_BY_ID": str(self.owner)}
        raise AssertionError(method)


def test_ambiguous_timeout_is_accepted_when_readback_confirms_commit():
    client = AmbiguousClient()
    update_owner_safely(client, "lead", 101, 72, attempts=3)
    assert client.owner == 72
    assert client.update_calls == 1


def test_multiple_manual_owners_without_unique_company_anchor_is_blocking():
    companies = [company(1, 13), company(2, 70)]
    contacts = [
        contact(11, 1, 13, last="Петров"),
        contact(12, 2, 70, last="Петров"),
    ]
    leads = [lead(101, 1, 11, 15), lead(102, 2, 12, 15)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    with pytest.raises(ReassignmentError, match="несколькими действующими владельцами"):
        choose_existing_owner(packages[0], companies, contacts, leads, {15}, (72, 73))
