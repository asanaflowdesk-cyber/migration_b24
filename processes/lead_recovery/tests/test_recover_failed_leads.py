from __future__ import annotations

from datetime import datetime, timezone

import pytest

from recover_failed_leads import (
    DEFAULT_FAILURE_REASON_FIELD,
    _utc_cutoff,
    find_failed_leads,
    recover_leads,
    resolve_moved_by_id,
)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.list_params = None
        self.after = {}
        self.get_sequences = {}

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "crm.lead.update":
            lead_id = str(params["id"])
            self.after.setdefault(lead_id, {}).update(params["fields"])
            return True
        if method == "crm.lead.get":
            lead_id = str(params["id"])
            sequence = self.get_sequences.get(lead_id)
            if sequence:
                value = sequence.pop(0)
                self.after[lead_id] = dict(value)
                return value
            return self.after[lead_id]
        raise AssertionError(method)

    def list_all(self, method, params):
        assert method == "crm.lead.list"
        self.list_params = params
        return [{
            "ID": "1",
            "STATUS_ID": "JUNK",
            "STATUS_SEMANTIC_ID": "F",
            "ASSIGNED_BY_ID": "17",
            "MOVED_BY_ID": "77",
            "MOVED_TIME": "2026-09-05T10:00:00+05:00",
            DEFAULT_FAILURE_REASON_FIELD: "901",
        }]


def lead5():
    return {
        "ID": "5",
        "TITLE": "X",
        "STATUS_ID": "JUNK",
        "ASSIGNED_BY_ID": "17",
        "MOVED_BY_ID": "77",
        "MOVED_TIME": "2026-09-01",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }


def test_cutoff_is_exact_days_in_utc():
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert _utc_cutoff(7, now) == "2026-08-31T12:00:00+00:00"


def test_resolve_mover_uses_explicit_id():
    assert resolve_moved_by_id("42", "77") == 42


def test_resolve_mover_uses_repository_variable():
    assert resolve_moved_by_id("", "77") == 77


def test_resolve_mover_never_calls_user_current_and_requires_id():
    with pytest.raises(ValueError, match="Bitrix user ID is required"):
        resolve_moved_by_id("", "")


def test_query_only_current_failed_leads_moved_by_user_in_window_and_reads_reason():
    client = FakeClient()
    rows = find_failed_leads(client, moved_by_id=77, cutoff_iso="2026-08-31T00:00:00+00:00")
    assert len(rows) == 1
    f = client.list_params["filter"]
    assert f["=STATUS_SEMANTIC_ID"] == "F"
    assert f["=MOVED_BY_ID"] == 77
    assert f[">=MOVED_TIME"] == "2026-08-31T00:00:00+00:00"
    assert DEFAULT_FAILURE_REASON_FIELD in client.list_params["select"]


def test_dry_run_changes_nothing_and_reports_reason_clear():
    client = FakeClient()
    rows = recover_leads(client, [lead5()], apply=False, stabilize_seconds=0, repair_wait_seconds=0)
    assert rows[0]["action"] == "would_restore"
    assert rows[0]["FAILURE_REASON_BEFORE"] == "901"
    assert rows[0]["FAILURE_REASON_AFTER"] == ""
    assert client.calls == []


def test_apply_explicitly_preserves_assignee_clears_reason_and_verifies_twice():
    client = FakeClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "17",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    rows = recover_leads(client, [lead5()], apply=True, stabilize_seconds=0, repair_wait_seconds=0)
    updates = [params for method, params in client.calls if method == "crm.lead.update"]
    assert updates[0] == {
        "id": "5",
        "fields": {
            "STATUS_ID": "NEW",
            "ASSIGNED_BY_ID": "17",
            DEFAULT_FAILURE_REASON_FIELD: "",
        },
    }
    gets = [params for method, params in client.calls if method == "crm.lead.get"]
    assert len(gets) == 2
    assert rows[0]["action"] == "restored"
    assert rows[0]["ASSIGNED_BY_ID_AFTER"] == "17"
    assert rows[0]["FAILURE_REASON_AFTER"] == ""


def test_async_robot_reassigns_lead_then_process_restores_original_assignee():
    client = FakeClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "17",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    client.get_sequences["5"] = [
        {
            "ID": "5",
            "ASSIGNED_BY_ID": "99",
            "STATUS_ID": "NEW",
            DEFAULT_FAILURE_REASON_FIELD: "",
        },
    ]
    rows = recover_leads(client, [lead5()], apply=True, stabilize_seconds=0, repair_wait_seconds=0)
    updates = [params for method, params in client.calls if method == "crm.lead.update"]
    assert len(updates) == 2
    assert updates[1] == {"id": "5", "fields": {"ASSIGNED_BY_ID": "17"}}
    assert rows[0]["automation_intervened"] is True
    assert rows[0]["repair_attempted"] is True
    assert rows[0]["action"] == "restored"
    assert rows[0]["ASSIGNED_BY_ID_AFTER"] == "17"


def test_reason_not_cleared_on_first_pass_is_repaired_with_null():
    client = FakeClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "17",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    client.get_sequences["5"] = [
        {
            "ID": "5",
            "ASSIGNED_BY_ID": "17",
            "STATUS_ID": "NEW",
            DEFAULT_FAILURE_REASON_FIELD: "901",
        },
    ]
    rows = recover_leads(client, [lead5()], apply=True, stabilize_seconds=0, repair_wait_seconds=0)
    updates = [params for method, params in client.calls if method == "crm.lead.update"]
    assert updates[1] == {"id": "5", "fields": {DEFAULT_FAILURE_REASON_FIELD: None}}
    assert rows[0]["action"] == "restored"
    assert rows[0]["FAILURE_REASON_AFTER"] is None


def test_stage_changed_by_automation_is_not_force_moved_again_and_fails_loudly():
    client = FakeClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "17",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    client.get_sequences["5"] = [
        {
            "ID": "5",
            "ASSIGNED_BY_ID": "17",
            "STATUS_ID": "IN_PROCESS",
            DEFAULT_FAILURE_REASON_FIELD: "",
        },
    ]
    rows = recover_leads(client, [lead5()], apply=True, stabilize_seconds=0, repair_wait_seconds=0)
    updates = [params for method, params in client.calls if method == "crm.lead.update"]
    assert len(updates) == 1
    assert rows[0]["action"] == "verify_failed"
    assert "stage changed after Bitrix automation" in rows[0]["error"]


def test_final_verification_catches_delayed_second_reassignment():
    client = FakeClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "17",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    client.get_sequences["5"] = [
        {
            "ID": "5",
            "ASSIGNED_BY_ID": "99",
            "STATUS_ID": "NEW",
            DEFAULT_FAILURE_REASON_FIELD: "",
        },
        {
            "ID": "5",
            "ASSIGNED_BY_ID": "88",
            "STATUS_ID": "NEW",
            DEFAULT_FAILURE_REASON_FIELD: "",
        },
    ]
    rows = recover_leads(client, [lead5()], apply=True, stabilize_seconds=0, repair_wait_seconds=0)
    assert rows[0]["action"] == "verify_failed"
    assert "assignee='88'" in rows[0]["error"]


def test_invalid_assignee_blocks_update():
    client = FakeClient()
    lead = lead5()
    lead["ASSIGNED_BY_ID"] = ""
    rows = recover_leads(client, [lead], apply=True, stabilize_seconds=0, repair_wait_seconds=0)
    assert rows[0]["action"] == "error"
    assert "ASSIGNED_BY_ID" in rows[0]["error"]
    assert client.calls == []
