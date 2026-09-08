from __future__ import annotations

from datetime import datetime, timezone

from reopen_failed_leads import (
    Candidate,
    apply_candidates,
    build_candidate_filter,
    load_candidates,
)


class FakeClient:
    def __init__(self, rows=None, leads=None):
        self.rows = list(rows or [])
        self.leads = {str(key): dict(value) for key, value in (leads or {}).items()}
        self.updates = []
        self.last_list_payload = None

    def list_all(self, method, payload):
        assert method == "crm.lead.list"
        self.last_list_payload = payload
        return list(self.rows)

    def call(self, method, payload=None):
        if method == "crm.lead.get":
            return dict(self.leads.get(str(payload["id"]), {})) or None
        raise AssertionError(method)

    def update_lead(self, lead_id, fields):
        self.updates.append((str(lead_id), dict(fields)))
        lead = self.leads.setdefault(str(lead_id), {"ID": str(lead_id)})
        lead.update(fields)


def test_filter_uses_stage_mover_and_stage_moved_time_not_date_modify():
    result = build_candidate_filter(1, "JUNK", "2026-09-01T12:00:00+00:00")

    assert result == {
        "STATUS_ID": "JUNK",
        "MOVED_BY_ID": 1,
        ">=MOVED_TIME": "2026-09-01T12:00:00+00:00",
    }
    assert "DATE_MODIFY" not in result
    assert "MODIFY_BY_ID" not in result


def test_load_candidates_keeps_only_exact_current_failure_moves():
    cutoff = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    rows = [
        {
            "ID": "10",
            "TITLE": "OK",
            "STATUS_ID": "JUNK",
            "ASSIGNED_BY_ID": "17",
            "MOVED_BY_ID": "1",
            "MOVED_TIME": "2026-09-03T12:00:00+00:00",
        },
        {
            "ID": "11",
            "TITLE": "wrong mover",
            "STATUS_ID": "JUNK",
            "ASSIGNED_BY_ID": "18",
            "MOVED_BY_ID": "2",
            "MOVED_TIME": "2026-09-03T12:00:00+00:00",
        },
        {
            "ID": "12",
            "TITLE": "too old",
            "STATUS_ID": "JUNK",
            "ASSIGNED_BY_ID": "19",
            "MOVED_BY_ID": "1",
            "MOVED_TIME": "2026-08-30T12:00:00+00:00",
        },
        {
            "ID": "13",
            "TITLE": "not failed",
            "STATUS_ID": "NEW",
            "ASSIGNED_BY_ID": "20",
            "MOVED_BY_ID": "1",
            "MOVED_TIME": "2026-09-03T12:00:00+00:00",
        },
    ]
    client = FakeClient(rows=rows)

    result = load_candidates(
        client,
        moved_by_user_id=1,
        failed_status_id="JUNK",
        cutoff=cutoff,
        cutoff_iso=cutoff.isoformat(),
    )

    assert [item.lead_id for item in result] == ["10"]
    assert result[0].assigned_by_id == "17"


def test_apply_changes_status_and_resends_same_assignee():
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    client = FakeClient(
        leads={
            "10": {
                "ID": "10",
                "STATUS_ID": "JUNK",
                "ASSIGNED_BY_ID": "17",
                "MOVED_BY_ID": "1",
                "MOVED_TIME": "2026-09-03T12:00:00+00:00",
            }
        }
    )
    rows = [Candidate("10", "Lead", "17", "1", "2026-09-03T12:00:00+00:00", "JUNK")]

    result = apply_candidates(
        client,
        rows,
        moved_by_user_id=1,
        failed_status_id="JUNK",
        target_status_id="NEW",
        cutoff=cutoff,
        verify_delay_seconds=0,
    )

    assert client.updates == [("10", {"STATUS_ID": "NEW", "ASSIGNED_BY_ID": 17})]
    assert result[0].action == "moved_to_new"


def test_apply_rechecks_current_stage_before_touching_lead():
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    client = FakeClient(
        leads={
            "10": {
                "ID": "10",
                "STATUS_ID": "NEW",
                "ASSIGNED_BY_ID": "17",
                "MOVED_BY_ID": "1",
                "MOVED_TIME": "2026-09-03T12:00:00+00:00",
            }
        }
    )
    rows = [Candidate("10", "Lead", "17", "1", "2026-09-03T12:00:00+00:00", "JUNK")]

    result = apply_candidates(
        client,
        rows,
        moved_by_user_id=1,
        failed_status_id="JUNK",
        target_status_id="NEW",
        cutoff=cutoff,
        verify_delay_seconds=0,
    )

    assert client.updates == []
    assert result[0].action == "skipped"
    assert result[0].note == "status_changed_before_apply"


class RobotChangesAssigneeClient(FakeClient):
    def __init__(self):
        super().__init__(
            leads={
                "10": {
                    "ID": "10",
                    "STATUS_ID": "JUNK",
                    "ASSIGNED_BY_ID": "17",
                    "MOVED_BY_ID": "1",
                    "MOVED_TIME": "2026-09-03T12:00:00+00:00",
                }
            }
        )
        self.get_count = 0

    def call(self, method, payload=None):
        if method == "crm.lead.get":
            self.get_count += 1
            lead = dict(self.leads[str(payload["id"])])
            if self.get_count >= 2:
                lead["ASSIGNED_BY_ID"] = "99"
                self.leads[str(payload["id"])]["ASSIGNED_BY_ID"] = "99"
            return lead
        return super().call(method, payload)


def test_post_stage_verification_restores_assignee_changed_by_robot():
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    client = RobotChangesAssigneeClient()
    rows = [Candidate("10", "Lead", "17", "1", "2026-09-03T12:00:00+00:00", "JUNK")]

    result = apply_candidates(
        client,
        rows,
        moved_by_user_id=1,
        failed_status_id="JUNK",
        target_status_id="NEW",
        cutoff=cutoff,
        verify_delay_seconds=0,
    )

    assert client.updates == [
        ("10", {"STATUS_ID": "NEW", "ASSIGNED_BY_ID": 17}),
        ("10", {"ASSIGNED_BY_ID": 17, "STATUS_ID": "NEW"}),
    ]
    assert result[0].action == "moved_to_new_assignee_restored"
