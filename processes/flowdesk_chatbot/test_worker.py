from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from common.bitrix import BitrixError
from processes.flowdesk_chatbot.bitrix_worker import Runtime
from processes.flowdesk_chatbot.engine import new_session
from processes.flowdesk_chatbot.worker_control import rotate_log, tail_text


class FakeStore:
    def __init__(self) -> None:
        self.sessions = {}

    def put_session(self, key, payload) -> None:
        self.sessions[key] = payload

    def get_session(self, key):
        return self.sessions.get(key)

    def get_meta(self, key, default=""):
        return default

    def set_meta(self, key, value) -> None:
        pass


class DeskFlowWorkerTests(unittest.TestCase):
    def runtime(self) -> Runtime:
        runtime = Runtime.__new__(Runtime)
        runtime.bot_id = 103
        runtime.bot_token = "test-token"
        runtime.command_name = "flowdesk_test"
        runtime.portal_base = "https://example.test"
        runtime.store = FakeStore()
        runtime.task_user_fields = set()
        runtime._text_field_state = {}
        runtime.event_retry_attempts = 3
        runtime.event_retry_delay = 0.0
        return runtime

    def test_text_field_toggle_is_not_repeated_when_state_is_unchanged(self) -> None:
        runtime = self.runtime()
        calls = []

        def fake_call(method, params=None):
            calls.append((method, params))
            return True

        runtime.call = fake_call

        self.assertTrue(runtime.set_text_field("chat77", False))
        self.assertTrue(runtime.set_text_field("chat77", False))
        self.assertTrue(runtime.set_text_field("chat77", True))
        self.assertTrue(runtime.set_text_field("chat77", True))

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["enabled"], False)
        self.assertEqual(calls[1][1]["enabled"], True)

    def test_missing_active_message_is_replaced_instead_of_wedging_flow(self) -> None:
        runtime = self.runtime()
        session = new_session(153, "chat77")
        session["current_screen"] = "target"
        session["active_message_id"] = 111

        runtime.set_text_field = lambda dialog_id, enabled: True
        runtime.update_message = lambda *args, **kwargs: (_ for _ in ()).throw(
            BitrixError(
                "imbot.v2.Chat.Message.update",
                "MESSAGE_NOT_FOUND",
                "Message not found",
            )
        )
        sent = []

        def fake_send(dialog_id, text, **kwargs):
            sent.append((dialog_id, text))
            return 999

        runtime.send = fake_send

        runtime.render_current(session)

        self.assertEqual(session["active_message_id"], 999)
        self.assertEqual(len(sent), 1)

    def test_event_is_retried_then_succeeds(self) -> None:
        runtime = self.runtime()
        attempts = {"count": 0}

        def flaky(event):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("temporary failure")

        runtime.handle_event = flaky
        ok = runtime.process_event_with_retry(
            {"eventId": 17, "type": "ONIMBOTV2COMMANDADD", "data": {}}
        )

        self.assertTrue(ok)
        self.assertEqual(attempts["count"], 3)

    def test_poison_event_is_bounded_and_does_not_retry_forever(self) -> None:
        runtime = self.runtime()
        attempts = {"count": 0}

        def broken(event):
            attempts["count"] += 1
            raise ValueError("permanent bad payload")

        runtime.handle_event = broken
        ok = runtime.process_event_with_retry(
            {"eventId": 18, "type": "ONIMBOTV2COMMANDADD", "data": {}}
        )

        self.assertFalse(ok)
        self.assertEqual(attempts["count"], 3)

    def test_history_prefers_dedicated_initiator_field(self) -> None:
        runtime = self.runtime()
        runtime.task_user_fields = {"UF_FLOWDESK_INITIATOR_ID"}
        seen_filters = []

        def fake_call(method, params=None):
            self.assertEqual(method, "tasks.task.list")
            seen_filters.append(dict(params["filter"]))
            return {
                "tasks": [
                    {
                        "id": "500",
                        "title": "Запрос | Юристы",
                        "xmlId": "FLOWDESK_CHAT_abc",
                    }
                ]
            }

        runtime.call = fake_call
        rows = runtime.recent_flowdesk_tasks(153, 1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            seen_filters[0]["UF_FLOWDESK_INITIATOR_ID"],
            "153",
        )

    def test_existing_task_prevents_duplicate_task_add(self) -> None:
        runtime = self.runtime()
        calls = []

        def fake_call(method, params=None):
            calls.append(method)
            if method == "tasks.task.list":
                xml_id = params["filter"]["=XML_ID"]
                return {
                    "tasks": [
                        {
                            "id": "777",
                            "xmlId": xml_id,
                        }
                    ]
                }
            raise AssertionError(f"Unexpected mutation: {method}")

        runtime.call = fake_call
        session = new_session(153, "chat77")
        session["data"]["request_type"] = "Другое"
        task_id = runtime.create_task(session)

        self.assertEqual(task_id, "777")
        self.assertEqual(calls, ["tasks.task.list"])

    def test_log_rotation_and_tail_do_not_read_unbounded_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker.err.log"
            path.write_text("A" * 5000 + "TAIL", encoding="utf-8")

            self.assertTrue(tail_text(path, max_bytes=20).endswith("TAIL"))
            rotate_log(path, max_bytes=100)

            self.assertFalse(path.exists())
            backup = path.with_suffix(path.suffix + ".1")
            self.assertTrue(backup.exists())


if __name__ == "__main__":
    unittest.main()
