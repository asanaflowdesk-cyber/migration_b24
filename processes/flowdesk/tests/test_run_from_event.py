from __future__ import annotations

import json

import run_from_event as flowdesk


def payload() -> dict[str, str]:
    return {
        "flowdesk_request_1": "Обращение",
        "flowdesk_request": "Клиент",
        "flowdesk_department": "Поддержка",
        "flowdesk_attachment": "",
        "resp_id": "17",
        "flowdesk_email": "user@example.kz",
        "flowdesk_complaint_subproduct": "",
        "flowdesk_complaint_description": "Описание",
        "flowdesk_complaint_type": "Жалоба",
        "flowdesk_complaint_product": "Продукт",
        "Datetime": "2026-09-10 10:00:00",
    }


def test_event_key_is_stable_and_content_dependent() -> None:
    first = payload()
    reordered = dict(reversed(list(first.items())))
    assert flowdesk.flowdesk_event_key(first) == flowdesk.flowdesk_event_key(reordered)

    reordered["flowdesk_request"] = "Другой клиент"
    assert flowdesk.flowdesk_event_key(first) != flowdesk.flowdesk_event_key(reordered)


def test_existing_task_stops_before_user_invitation(monkeypatch, capsys) -> None:
    class FakeClient:
        @classmethod
        def from_env(cls):
            return cls()

    monkeypatch.setenv("FLOWDESK_PAYLOAD", json.dumps(payload(), ensure_ascii=False))
    monkeypatch.setattr(flowdesk, "BitrixClient", FakeClient)
    monkeypatch.setattr(flowdesk, "validate_responsible", lambda *_: None)
    monkeypatch.setattr(flowdesk, "find_task_by_event_key", lambda *_: "555")

    invited = False

    def unexpected_invite(*_):
        nonlocal invited
        invited = True
        raise AssertionError("resolve_creator must not run for a duplicate event")

    monkeypatch.setattr(flowdesk, "resolve_creator", unexpected_invite)

    assert flowdesk.main() == 0
    assert invited is False
    assert "ID=555" in capsys.readouterr().out


def test_task_creation_writes_idempotency_key() -> None:
    class FakeClient:
        def __init__(self):
            self.params = None

        def call(self, method, params):
            assert method == "tasks.task.add"
            self.params = params
            return {"task": {"id": "91"}}

    client = FakeClient()
    task_id = flowdesk.create_task(
        client,
        title="Задача",
        description="Описание",
        creator_id=10,
        responsible_id=17,
        project_id=2,
        deadline="2026-09-13T10:00:00+05:00",
        event_key="FLOWDESK_key",
    )

    assert task_id == "91"
    assert client.params["fields"]["XML_ID"] == "FLOWDESK_key"
