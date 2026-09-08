from __future__ import annotations

from datetime import datetime, timezone

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
        self.current = {"ID": "77"}
        self.after = {}

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "user.current":
            return self.current
        if method == "crm.lead.update":
            lead_id = str(params["id"])
            self.after.setdefault(lead_id, {}).update(params["fields"])
            return True
        if method == "crm.lead.get":
            lead_id = str(params["id"])
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


def test_cutoff_is_exact_days_in_utc():
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert _utc_cutoff(7, now) == "2026-08-31T12:00:00+00:00"


def test_resolve_mover_uses_explicit_id():
    client = FakeClient()
    assert resolve_moved_by_id(client, "42") == 42
    assert client.calls == []


def test_resolve_mover_falls_back_to_webhook_user():
    client = FakeClient()
    assert resolve_moved_by_id(client, "") == 77
    assert client.calls == [("user.current", None)]


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
    leads = [{
        "ID": "5",
        "TITLE": "X",
        "STATUS_ID": "JUNK",
        "ASSIGNED_BY_ID": "17",
        "MOVED_BY_ID": "77",
        "MOVED_TIME": "2026-09-01",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }]
    rows = recover_leads(client, leads, apply=False)
    assert rows[0]["action"] == "would_restore"
    assert rows[0]["FAILURE_REASON_BEFORE"] == "901"
    assert rows[0]["FAILURE_REASON_AFTER"] == ""
    assert client.calls == []


def test_apply_updates_status_clears_failure_reason_and_preserves_assignee():
    client = FakeClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "17",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    leads = [{
        "ID": "5",
        "TITLE": "X",
        "STATUS_ID": "JUNK",
        "ASSIGNED_BY_ID": "17",
        "MOVED_BY_ID": "77",
        "MOVED_TIME": "2026-09-01",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }]
    rows = recover_leads(client, leads, apply=True)
    update = next(params for method, params in client.calls if method == "crm.lead.update")
    assert update == {
        "id": "5",
        "fields": {
            "STATUS_ID": "NEW",
            DEFAULT_FAILURE_REASON_FIELD: "",
        },
    }
    assert "ASSIGNED_BY_ID" not in update["fields"]
    assert rows[0]["action"] == "restored"
    assert rows[0]["ASSIGNED_BY_ID_AFTER"] == "17"
    assert rows[0]["FAILURE_REASON_AFTER"] == ""


def test_verification_fails_if_assignee_changes():
    client = FakeClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "99",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    leads = [{
        "ID": "5",
        "TITLE": "X",
        "STATUS_ID": "JUNK",
        "ASSIGNED_BY_ID": "17",
        "MOVED_BY_ID": "77",
        "MOVED_TIME": "2026-09-01",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }]
    rows = recover_leads(client, leads, apply=True)
    assert rows[0]["action"] == "verify_failed"
    assert "assignee changed" in rows[0]["error"]


def test_verification_fails_if_failure_reason_is_not_cleared():
    class StickyReasonClient(FakeClient):
        def call(self, method, params=None):
            result = super().call(method, params)
            if method == "crm.lead.update":
                lead_id = str(params["id"])
                self.after[lead_id][DEFAULT_FAILURE_REASON_FIELD] = "901"
            return result

    client = StickyReasonClient()
    client.after["5"] = {
        "ID": "5",
        "ASSIGNED_BY_ID": "17",
        "STATUS_ID": "JUNK",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }
    leads = [{
        "ID": "5",
        "TITLE": "X",
        "STATUS_ID": "JUNK",
        "ASSIGNED_BY_ID": "17",
        "MOVED_BY_ID": "77",
        "MOVED_TIME": "2026-09-01",
        DEFAULT_FAILURE_REASON_FIELD: "901",
    }]
    rows = recover_leads(client, leads, apply=True)
    assert rows[0]["action"] == "verify_failed"
    assert "failure reason was not cleared" in rows[0]["error"]
