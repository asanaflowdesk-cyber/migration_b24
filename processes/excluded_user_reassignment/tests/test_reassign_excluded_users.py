import pytest

from reassign_excluded_users import (
    Package,
    add_package_context,
    assign_targets,
    build_packages,
    build_rows,
    parse_id_set,
    plan_packages,
    package_has_nonprotected_majority,
    protected_majority_counts,
    protected_manager_vote_counts,
    select_protected_manager_target,
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


def test_nonexcluded_lead_is_context_but_is_never_changed():
    companies = [company(1, 13)]
    contacts = [contact(11, 1, 13)]
    leads = [lead(101, 1, 11, 15), lead(102, 1, 11, 13)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert eligible[0].target_owner_id in {72, 73}

    rows = build_rows(eligible, [], companies, contacts, leads, {15}, {13: "Manager 13", 15: "Old", 72: "ROP72", 73: "ROP73"})
    lead_ids = {r["entity_id"] for r in rows if r["entity_type"] == "lead"}
    assert lead_ids == {101}


def test_existing_manual_owner_does_not_block_or_anchor_package():
    companies = [company(1, 13)]
    contacts = [contact(11, 1, 13)]
    leads = [lead(101, 1, 11, 15), lead(102, 1, 11, 13)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert eligible[0].target_owner_id in {72, 73}
    assert eligible[0].target_owner_id != 13


def test_existing_partial_rop_assignment_does_not_anchor_package():
    companies = [company(1, 72)]
    contacts = [contact(11, 1, 72)]
    leads = [lead(101, 1, 11, 15)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert eligible[0].target_reason == "balanced_between_rops_72_73"
    assert eligible[0].target_owner_id in {72, 73}


def test_one_protected_lead_does_not_block_when_other_leads_are_majority():
    companies = [company(1, 16)]
    contacts = [contact(11, 1, 16)]
    leads = [
        lead(101, 1, 11, 15),   # excluded, will be moved
        lead(102, 1, 11, 16),   # protected manager
        lead(103, 1, 11, 13),   # other
    ]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert len(eligible) == 1
    assert eligible[0].target_owner_id in {72, 73}
    assert protected_majority_counts(eligible[0], {16, 18, 38}) == (1, 2, 3)


def test_protected_combined_majority_moves_remainder_to_manager_with_most_leads():
    companies = [company(1, 13)]
    contacts = [contact(11, 1, 13)]
    leads = [
        lead(101, 1, 11, 15),
        lead(102, 1, 11, 18),
        lead(103, 1, 11, 18),
        lead(104, 1, 11, 16),
    ]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert len(eligible) == 1
    assert eligible[0].target_owner_id == 18
    assert "protected_majority_to_18" in eligible[0].target_reason
    assert protected_manager_vote_counts(eligible[0], {16, 18, 38}) == {16: 1, 18: 2, 38: 0}


def test_equal_protected_votes_use_lowest_id_tie_break_so_package_does_not_hang():
    companies = [company(1, 16)]
    contacts = [contact(11, 1, 16)]
    leads = [
        lead(101, 1, 11, 15),
        lead(102, 1, 11, 16),
        lead(103, 1, 11, 18),
    ]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    target, counts, tie = select_protected_manager_target(packages[0], {16, 18, 38})
    assert target == 16
    assert counts == {16: 1, 18: 1, 38: 0}
    assert tie is True

    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert eligible[0].target_owner_id == 16
    assert "tie_break=yes" in eligible[0].target_reason


def test_protected_vs_other_tie_moves_remainder_to_existing_protected_manager():
    companies = [company(1, 16)]
    contacts = [contact(11, 1, 16)]
    leads = [lead(101, 1, 11, 15), lead(102, 1, 11, 16)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert len(eligible) == 1
    assert eligible[0].target_owner_id == 16


def test_company_or_contact_owner_16_does_not_vote_when_leads_are_not_on_16_18_38():
    companies = [company(1, 16)]
    contacts = [contact(11, 1, 18)]
    leads = [lead(101, 1, 11, 15), lead(102, 1, 11, 13)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    package = packages[0]
    assert protected_majority_counts(package, {16, 18, 38}) == (0, 2, 2)
    assert package_has_nonprotected_majority(package, {16, 18, 38})
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert len(eligible) == 1


def test_nonexcluded_context_leads_vote_in_majority_but_are_not_changed():
    companies = [company(1, 13)]
    contacts = [contact(11, 1, 13)]
    leads = [
        lead(101, 1, 11, 15),
        lead(102, 1, 11, 16),
        lead(103, 1, 11, 13),
        lead(104, 1, 11, 40),
    ]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert protected_majority_counts(eligible[0], {16, 18, 38}) == (1, 3, 4)
    rows = build_rows(
        eligible, [], companies, contacts, leads, {15},
        {13: "M13", 15: "Old", 16: "M16", 40: "M40", 72: "R72", 73: "R73"},
    )
    changed_leads = {r["entity_id"] for r in rows if r["entity_type"] == "lead"}
    assert changed_leads == {101}


def test_protected_lead_401_skips_whole_package():
    companies = [company(1, 15)]
    contacts = [contact(11, 1, 15)]
    leads = [lead(401, 1, 11, 15), lead(402, 1, 11, 15)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, {401}, (72, 73))
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


def test_plan_balances_all_nonprotected_packages_even_with_mixed_existing_owners():
    companies = [company(1, 13), company(2, 40)]
    contacts = [contact(11, 1, 13, last="Петров"), contact(12, 2, 40, last="Сидоров")]
    leads = [lead(101, 1, 11, 15), lead(102, 2, 12, 15)]
    packages = build_packages(companies, contacts, leads, {15})
    add_package_context(packages, companies, contacts, leads)
    eligible, skipped = plan_packages(packages, {16, 18, 38}, set(), (72, 73))
    assert not skipped
    assert len(eligible) == 2
    assert {p.target_owner_id for p in eligible} == {72, 73}


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
