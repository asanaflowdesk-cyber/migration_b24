from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESS_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROCESS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_from_event import event_marker, find_task_by_marker, validate_project, validate_responsible


class FakeClient:
    def __init__(self):
        self.calls = []

    def list_all(self, method, params=None):
        self.calls.append((method, params))
        if method == "user.get":
            return [{"ID": "9", "ACTIVE": "Y"}]
        if method == "tasks.task.list":
            return [{"id": "77", "description": "body\n\n[FLOWDESK_EVENT:abc]"}]
        raise AssertionError(method)

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "sonet_group.get":
            return [{"ID": "2", "NAME": "Project"}]
        raise AssertionError(method)


def test_event_marker_is_stable_for_same_payload():
    payload = {"a": "1", "b": "2"}
    assert event_marker(payload) == event_marker(dict(reversed(list(payload.items()))))


def test_read_only_preflight_validates_user_and_project():
    client = FakeClient()
    validate_responsible(client, 9)
    validate_project(client, 2)
    assert [method for method, _ in client.calls] == ["user.get", "sonet_group.get"]


def test_existing_marker_is_reused():
    client = FakeClient()
    assert find_task_by_marker(client, 2, "[FLOWDESK_EVENT:abc]") == "77"
