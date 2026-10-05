from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.bitrix import BitrixError
from processes.flowdesk_chatbot.avatar_asset import SERVICE_CHAT_AVATAR_B64
from processes.flowdesk_chatbot.bitrix_worker import Runtime
from processes.flowdesk_chatbot.engine import new_session
from processes.flowdesk_chatbot.worker_control import rotate_log, tail_text


class FakeStore:
    def __init__(self) -> None:
        self.sessions = {}
        self.meta = {}

    def put_session(self, key, payload) -> None:
        self.sessions[key] = payload

    def get_session(self, key):
        return self.sessions.get(key)

    def get_meta(self, key, default=""):
        return self.meta.get(key, default)

    def set_meta(self, key, value) -> None:
        self.meta[key] = str(value)


class DeskFlowWorkerTests(unittest.TestCase):
    def runtime(self) -> Runtime:
        runtime = Runtime.__new__(Runtime)
        runtime.bot_id = 103
        runtime.bot_token = "test-token"
        runtime.command_name = "flowdesk_test"
        runtime.portal_base = "https://example.test"
        runtime.store = FakeStore()
        runtime.task_user_fields = set()
        runtime.service_chat_avatar_url = (
            "https://bitrix.theeurasia.kz/picture/K2blnYG5Z7Cm0teajIEf"
        )
        runtime._service_chat_avatar_b64 = None
        runtime._service_chat_avatar_version = None
        runtime._text_field_state = {}
        runtime.event_retry_attempts = 3
        runtime.event_retry_delay = 0.0
        return runtime

    def test_service_chat_avatar_url_is_only_fallback(self) -> None:
        runtime = self.runtime()

        class Response:
            content = b"fake-image-bytes"
            headers = {"Content-Type": "image/png"}

            @staticmethod
            def raise_for_status():
                return None

        with (
            patch(
                "processes.flowdesk_chatbot.bitrix_worker.SERVICE_CHAT_AVATAR_B64",
                "",
            ),
            patch(
                "processes.flowdesk_chatbot.bitrix_worker.requests.get",
                return_value=Response(),
            ) as get,
        ):
            first = runtime.load_service_chat_avatar()
            second = runtime.load_service_chat_avatar()

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(
            first[0],
            base64.b64encode(b"fake-image-bytes").decode("ascii"),
        )
        self.assertEqual(get.call_count, 1)

    def test_new_service_chat_gets_avatar_at_creation_time(self) -> None:
        runtime = self.runtime()
        runtime.load_service_chat_avatar = lambda: ("BASE64_AVATAR", "v1")
        calls = []

        def fake_call(method, params=None):
            calls.append((method, params))
            if method == "imbot.v2.Chat.add":
                return {"chat": {"dialogId": "chat501"}}
            if method == "imbot.v2.Chat.update":
                self.assertEqual(params["fields"]["avatar"], "BASE64_AVATAR")
                return {"result": True}
            raise AssertionError(f"Unexpected method: {method}")

        runtime.call = fake_call
        dialog_id = runtime.ensure_service_chat(153, "Иван Иванов")

        self.assertEqual(dialog_id, "chat501")
        add = next(params for method, params in calls if method == "imbot.v2.Chat.add")
        self.assertEqual(add["fields"]["avatar"], "BASE64_AVATAR")
        self.assertEqual(
            runtime.store.get_meta("service_chat_avatar:chat501"),
            "v1",
        )

    def test_existing_service_chat_avatar_is_updated_only_once_per_version(self) -> None:
        runtime = self.runtime()
        runtime.store.set_meta("service_chat:153", "chat501")
        runtime.load_service_chat_avatar = lambda: ("BASE64_AVATAR", "v1")
        methods = []

        def fake_call(method, params=None):
            methods.append((method, params))
            if method == "imbot.v2.Chat.get":
                return {"chat": {"dialogId": "chat501"}}
            if method == "imbot.v2.Chat.update":
                self.assertEqual(params["fields"]["avatar"], "BASE64_AVATAR")
                return True
            raise AssertionError(f"Unexpected method: {method}")

        runtime.call = fake_call

        self.assertEqual(runtime.ensure_service_chat(153), "chat501")
        self.assertEqual(runtime.ensure_service_chat(153), "chat501")

        updates = [item for item in methods if item[0] == "imbot.v2.Chat.update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            runtime.store.get_meta("service_chat_avatar:chat501"),
            "v1",
        )

    def test_bundled_service_chat_avatar_is_valid_jpeg_base64(self) -> None:
        import base64

        payload = base64.b64decode(SERVICE_CHAT_AVATAR_B64, validate=True)

        self.assertTrue(payload.startswith(b"\xff\xd8\xff"))
        self.assertGreater(len(payload), 1000)

    def test_service_chat_avatar_uses_bundle_without_http_request(self) -> None:
        runtime = self.runtime()

        with patch(
            "processes.flowdesk_chatbot.bitrix_worker.requests.get"
        ) as get:
            avatar = runtime.load_service_chat_avatar()

        self.assertIsNotNone(avatar)
        self.assertFalse(get.called)

    def test_avatar_version_is_saved_only_after_confirmed_update(self) -> None:
        runtime = self.runtime()
        runtime.load_service_chat_avatar = lambda: ("BASE64_AVATAR", "v1")
        runtime.call = lambda method, params=None: {"result": False}

        with self.assertRaises(RuntimeError):
            runtime.ensure_service_chat_avatar("chat501", force=True)

        self.assertEqual(
            runtime.store.get_meta("service_chat_avatar:chat501"),
            "",
        )

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
