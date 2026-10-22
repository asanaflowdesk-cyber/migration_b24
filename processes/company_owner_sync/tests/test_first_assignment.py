from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from first_assignment import DB_NAME, complete_packages, event_cursor, ingest_events, prepare_first_assignments
from package_recovery import repair_package_residuals, receive_owner_events
from queue_founder_packages import process_claim
from test_package_recovery import CRM, assert_owner


def technical_crm():
    crm = CRM()
    for collection in (crm.contacts, crm.companies, crm.leads):
        for row in collection.values():
            row["ASSIGNED_BY_ID"] = "4"
    return crm


def event(seq, entity, entity_id, owner_id, stamp=None, previous=4):
    return {"seq": seq, "entity": entity, "entity_id": entity_id, "owner_id": owner_id,
            "previous_owner_id": previous, "event_ts": stamp or 1000 + seq, "received_at": "2026-10-21T12:00:00Z"}


@pytest.mark.parametrize("entity,item_id", [("lead", 2624), ("company", 933), ("contact", 837), ("contact", 838)])
def test_first_assignment_on_any_packet_element_moves_entire_packet(entity, item_id, tmp_path):
    crm = technical_crm()
    statuses = {key: row["STATUS_ID"] for key, row in crm.leads.items()}
    assert repair_package_residuals(crm, tmp_path)["updated"] == 0
    {"lead": crm.leads, "company": crm.companies, "contact": crm.contacts}[entity][item_id]["ASSIGNED_BY_ID"] = "13"
    ingest_events(tmp_path, [event(1, entity, item_id, 13)])

    result = repair_package_residuals(crm, tmp_path)

    assert result["status"] == "DONE"
    assert_owner(crm, 13)
    assert {key: row["STATUS_ID"] for key, row in crm.leads.items()} == statuses


def test_later_lead_and_changed_founder_cannot_replace_first_manager_after_restart(tmp_path):
    crm = technical_crm()
    repair_package_residuals(crm, tmp_path)
    crm.leads[2624]["ASSIGNED_BY_ID"] = "13"
    repair_package_residuals(crm, tmp_path)
    assert_owner(crm, 13)
    assert (tmp_path / DB_NAME).is_file()
    crm.leads[9999] = {"ID": "9999", "TITLE": "Next lead", "COMPANY_ID": "934", "CONTACT_ID": "837", "ASSIGNED_BY_ID": "14", "STATUS_ID": "NEW"}
    crm.contacts[837]["ASSIGNED_BY_ID"] = "14"
    ingest_events(tmp_path, [event(2, "lead", 9999, 14), event(3, "contact", 837, 14, previous=13)])

    result, failures = process_claim(crm, tmp_path, {"claim_id": "after-restart", "items": [{"contact_id": 837, "version": 2}]})

    assert failures == 0
    assert result[0]["success"]
    assert_owner(crm, 13)
    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 13)


def test_first_journal_event_wins_even_when_two_leads_are_assigned_before_scan(tmp_path):
    crm = technical_crm()
    crm.leads[2625]["ASSIGNED_BY_ID"] = "13"
    crm.leads[2624]["ASSIGNED_BY_ID"] = "14"
    # Input order and card ID must not decide the owner.
    ingest_events(tmp_path, [event(2, "lead", 2624, 14), event(1, "lead", 2625, 13)])

    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 13)
    assert event_cursor(tmp_path) == 2


def test_event_time_precedes_delivery_sequence_for_same_unclaimed_packet(tmp_path):
    crm = technical_crm()
    crm.leads[2624]["ASSIGNED_BY_ID"] = "14"
    crm.leads[2625]["ASSIGNED_BY_ID"] = "13"
    ingest_events(tmp_path, [event(1, "lead", 2624, 14, 2000), event(2, "lead", 2625, 13, 1000)])

    repair_package_residuals(crm, tmp_path)

    assert_owner(crm, 13)


def test_owner_change_without_journal_is_recovered_only_when_unambiguous(tmp_path):
    crm = technical_crm()
    crm.leads[2624]["ASSIGNED_BY_ID"] = "13"

    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 13)


def test_unknown_order_is_reported_without_writing_or_guessing(tmp_path):
    crm = technical_crm()
    crm.leads[2624]["ASSIGNED_BY_ID"] = "13"
    crm.leads[2625]["ASSIGNED_BY_ID"] = "14"
    before = deepcopy((crm.contacts, crm.companies, crm.leads))

    result = repair_package_residuals(crm, tmp_path)

    assert result["status"] == "PARTIAL"
    assert crm.writes == []
    assert (crm.contacts, crm.companies, crm.leads) == before
    assert json.loads((tmp_path / "recovery.json").read_text())["skipped"][0]["type"] == "first_assignment_order_unknown"


def test_packet_without_founder_contact_uses_company_and_all_leads(tmp_path):
    crm = technical_crm()
    crm.contacts = {}
    crm.requisites = []
    for lead in crm.leads.values():
        lead["CONTACT_ID"] = ""
        lead["COMPANY_ID"] = "933"
    crm.companies = {933: crm.companies[933]}
    crm.leads[2624]["ASSIGNED_BY_ID"] = "13"
    ingest_events(tmp_path, [event(1, "lead", 2624, 13)])

    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 13)
    crm.leads[2625]["ASSIGNED_BY_ID"] = "14"
    repair_package_residuals(crm, tmp_path)
    assert_owner(crm, 13)


def test_lead_only_packet_can_be_assigned_and_keeps_its_first_manager(tmp_path):
    crm = technical_crm()
    crm.companies = {}
    crm.contacts = {}
    crm.requisites = []
    crm.leads = {2624: crm.leads[2624]}
    crm.leads[2624]["COMPANY_ID"] = ""
    crm.leads[2624]["CONTACT_ID"] = ""
    repair_package_residuals(crm, tmp_path)
    crm.leads[2624]["ASSIGNED_BY_ID"] = "13"
    ingest_events(tmp_path, [event(1, "lead", 2624, 13)])

    repair_package_residuals(crm, tmp_path)
    crm.leads[2624]["ASSIGNED_BY_ID"] = "14"
    repair_package_residuals(crm, tmp_path)

    assert_owner(crm, 13)


def test_first_claim_is_saved_before_failed_crm_write_and_retry_keeps_manager(tmp_path):
    crm = technical_crm()
    crm.leads[2624]["ASSIGNED_BY_ID"] = "13"
    original = crm.update_lead
    crm.update_lead = lambda *args: (_ for _ in ()).throw(RuntimeError("write_failed"))
    assert repair_package_residuals(crm, tmp_path)["status"] == "PARTIAL"
    crm.update_lead = original
    crm.leads[2624]["ASSIGNED_BY_ID"] = "14"

    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 13)


def test_dry_run_does_not_create_or_change_persistent_claim(tmp_path):
    package = {"fio": "Иванов Иван", "contacts": [{"id": 1, "owner_id": 4}], "companies": [{"id": 2, "owner_id": 4, "leads": [{"id": 3, "owner_id": 13}]}]}
    preview, errors = prepare_first_assignments([package], tmp_path, apply=False)
    assert not errors and preview[0]["locked_owner_id"] == 13
    assert not (tmp_path / DB_NAME).exists()
    prepare_first_assignments([package], tmp_path, apply=True)
    before = (tmp_path / DB_NAME).read_bytes()
    prepare_first_assignments([package], tmp_path, apply=False)
    assert (tmp_path / DB_NAME).read_bytes() == before


def test_concurrent_processes_cannot_replace_committed_first_claim(tmp_path):
    package = {"fio": "Иванов Иван", "contacts": [{"id": 1, "owner_id": 4}], "companies": [{"id": 2, "owner_id": 4, "leads": [{"id": 3, "owner_id": 13}]}]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare_first_assignments([package], tmp_path, apply=True)[0][0]["locked_owner_id"], range(2)))
    assert results == [13, 13]
    package["companies"][0]["leads"][0]["owner_id"] = 14
    assert prepare_first_assignments([package], tmp_path, apply=True)[0][0]["locked_owner_id"] == 13


def test_event_delivery_is_idempotent_and_sequence_reset_is_detected(tmp_path):
    item = event(1, "lead", 3, 13)
    assert ingest_events(tmp_path, [item]) == 1
    assert ingest_events(tmp_path, [item]) == 0
    with pytest.raises(ValueError, match="sequence_reused"):
        ingest_events(tmp_path, [event(1, "lead", 3, 14)])


def test_new_owner_events_are_saved_even_before_their_package_is_visible(tmp_path):
    ingest_events(tmp_path, [event(1, "lead", 2624, 13)])
    crm = technical_crm()
    crm.leads[2624]["ASSIGNED_BY_ID"] = "13"
    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 13)


def test_owner_event_download_checkpoints_after_durable_write(monkeypatch, tmp_path):
    import queue_founder_packages

    seen = []
    def queue(url, key, action, **kwargs):
        seen.append(kwargs["after"])
        return {"events": [event(1, "lead", 3, 13)] if kwargs["after"] == 0 else [], "has_more": True}
    monkeypatch.setattr(queue_founder_packages, "queue_call", queue)

    assert receive_owner_events(tmp_path, "url", "key") == 1
    assert seen == [0, 1]
    assert event_cursor(tmp_path) == 1


def test_old_queue_without_owner_journal_keeps_periodic_recovery(monkeypatch, tmp_path):
    import queue_founder_packages

    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"ok": False, "error": "unknown_action"}

    monkeypatch.setattr(queue_founder_packages.requests, "post", lambda *a, **kw: Response())
    assert receive_owner_events(tmp_path, "url", "key") == 0


def test_existing_founder_with_a_new_technical_lead_keeps_normal_contact_authority(tmp_path):
    package = {"fio": "Иванов Иван", "contacts": [{"id": 1, "owner_id": 13}], "companies": [{"id": 2, "owner_id": 13, "leads": [{"id": 3, "owner_id": 4}]}]}
    result, errors = prepare_first_assignments([package], tmp_path, apply=True)
    assert not errors
    assert "locked_owner_id" not in result[0]


def test_minimal_packets_do_not_bypass_conflicting_company_fio():
    snapshot = {"contacts": [], "companies": [{"ID": "2", "ASSIGNED_BY_ID": "4"}], "leads": [{"ID": "3", "COMPANY_ID": "2", "ASSIGNED_BY_ID": "13"}], "requisites": []}
    assert complete_packages([], snapshot, [{"type": "company_conflicting_fio", "company_id": 2}]) == []


def test_minimal_company_packet_can_recover_company_assignment_before_first_scan(tmp_path):
    crm = technical_crm()
    crm.contacts = {}
    crm.requisites = []
    for lead in crm.leads.values():
        lead["CONTACT_ID"] = ""
        lead["COMPANY_ID"] = "933"
    crm.companies = {933: crm.companies[933]}
    crm.companies[933]["ASSIGNED_BY_ID"] = "13"

    repair_package_residuals(crm, tmp_path)

    assert_owner(crm, 13)


def test_unlinked_technical_duplicate_does_not_lock_a_previously_assigned_packet(tmp_path):
    crm = technical_crm()
    for collection in (crm.contacts, crm.companies, crm.leads):
        for row in collection.values():
            row["ASSIGNED_BY_ID"] = "13"
    crm.contacts[838]["ASSIGNED_BY_ID"] = "4"

    result = repair_package_residuals(crm, tmp_path)

    assert result["first_assigned_packages"] == 0
    assert_owner(crm, 13)
    crm.contacts[837]["ASSIGNED_BY_ID"] = "14"
    repair_package_residuals(crm, tmp_path)
    assert_owner(crm, 14)
