from copy import deepcopy
from pathlib import Path

from sync_founder_packages import apply_updates


class Client:
    def __init__(self):
        self.source_owner = 17
        self.owner = 9
        self.writes = 0
        self.fail_write = False
        self.change_source = False

    def call(self, method, payload):
        if method == "crm.contact.get":
            return {"ID": "100", "POST": "Учредитель", "ASSIGNED_BY_ID": self.source_owner}
        return {"ID": payload["id"], "ASSIGNED_BY_ID": self.owner}

    def update_company(self, item_id, fields):
        self.writes += 1
        if not self.fail_write:
            self.owner = fields["ASSIGNED_BY_ID"]
        if self.change_source:
            self.source_owner = 22

    update_contact = update_company
    update_lead = update_company


def rows():
    return [{"entity": "company", "id": 10, "source_contact_id": 100,
             "target": 17, "current": 9, "status": "planned"}]


def test_write_verified_and_repeat_is_noop():
    client = Client()
    first = rows()
    apply_updates(client, first)
    assert first[0]["status"] == "updated"
    second = rows()
    apply_updates(client, second)
    assert second[0]["status"] == "already_correct"
    assert client.writes == 1


def test_changed_source_prevents_any_write():
    client = Client()
    client.source_owner = 22
    plan = rows()
    apply_updates(client, plan)
    assert plan[0]["status"] == "error"
    assert client.writes == 0


def test_changed_target_is_not_overwritten():
    client = Client()
    client.owner = 33
    plan = rows()
    apply_updates(client, plan)
    assert plan[0]["status"] == "error"
    assert client.writes == 0


def test_failed_verification_stops_remaining_package():
    client = Client()
    client.fail_write = True
    plan = rows() + deepcopy(rows())
    apply_updates(client, plan)
    assert all(row["status"] == "error" for row in plan)
    assert client.writes == 1


def test_source_change_during_write_is_error():
    client = Client()
    client.change_source = True
    plan = rows()
    apply_updates(client, plan)
    assert plan[0]["status"] == "error"


def test_workflows_share_bounded_serial_queue():
    root = Path(__file__).resolve().parents[3]
    for filename in ("31-company-owner-sync.yml", "31a-company-owner-event.yml"):
        text = (root / ".github/workflows" / filename).read_text(encoding="utf-8")
        assert "group: founder-package-owner-writes" in text
        assert "queue: max" in text
        assert "cancel-in-progress: false" in text


def test_event_queue_has_manual_and_fast_recovery_triggers():
    root = Path(__file__).resolve().parents[3]
    text = (root / ".github/workflows" / "31a-company-owner-event.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert 'cron: "*/5 * * * *"' in text
