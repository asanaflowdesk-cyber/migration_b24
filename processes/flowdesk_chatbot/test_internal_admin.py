from hmac import digest
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from processes.flowdesk_chatbot.bitrix_worker import Runtime
from processes.flowdesk_chatbot.internal_admin import (
    ENTRY_PATH,
    EditorApplication,
    EditorServer,
    EditorStore,
    response_signature,
    signature_message,
)
from processes.flowdesk_chatbot.storage import SessionStore

OWNER = 101
EDITOR = 202
EMPLOYEE = 303
TEST_KEY = b"\x93" * 32
ENTRY = "https://portal.example" + ENTRY_PATH


def bridge_headers(who, method, path, body=b"", stamp=None, nonce=None):
    stamp = int(time.time()) if stamp is None else stamp
    nonce = secrets.token_hex(16) if nonce is None else nonce
    message = signature_message(who, stamp, nonce, method, path, body)
    return {
        "X-DeskFlow-User": str(who),
        "X-DeskFlow-Time": str(stamp),
        "X-DeskFlow-Nonce": nonce,
        "X-DeskFlow-Signature": digest(TEST_KEY, message, "sha256").hex(),
    }


class InternalAdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "editor.sqlite3"
        self.store = EditorStore(self.db, OWNER)
        self.server = EditorServer(("127.0.0.1", 0), EditorApplication(self.store, TEST_KEY), ENTRY)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def raw(self, path, headers=None, payload=None):
        req = Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=payload,
            headers={**(headers or {}), "Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=5) as result:
                body = result.read()
                expected = response_signature(TEST_KEY, headers["X-DeskFlow-Nonce"], result.status, body)
                self.assertEqual(result.headers.get("X-DeskFlow-Response-Signature"), expected)
                return result.status, body
        except HTTPError as err:
            with err:
                return err.code, err.read()

    def call(self, who, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        method = "POST" if payload is not None else "GET"
        headers = bridge_headers(who, method, path, payload or b"")
        status, value = self.raw(path, headers, payload)
        return status, json.loads(value)

    def test_forwarded_page_assets_and_data_are_hidden_from_employee(self):
        for path in ("/", "/assets/app.js", "/assets/graph.js", "/assets/app.css", "/api/draft", "/api/access", "/api/versions"):
            with self.subTest(path=path):
                signed = bridge_headers(EMPLOYEE, "GET", path)
                self.assertEqual(self.raw(path, signed), self.raw("/missing"))
                self.assertEqual(self.raw(path)[0], 404)
        signed_owner = bridge_headers(OWNER, "GET", "/")
        status, page = self.raw("/", signed_owner)
        self.assertEqual(status, 200)
        self.assertIn("Управление DeskFlow".encode(), page)

    def test_identity_route_and_body_are_signed(self):
        spoofed = bridge_headers(OWNER, "GET", "/api/draft")
        spoofed["X-DeskFlow-User"] = str(EMPLOYEE)
        self.assertEqual(self.raw("/api/draft", spoofed)[0], 404)

        spoofed = bridge_headers(OWNER, "GET", "/api/draft")
        self.assertEqual(self.raw("/api/access", spoofed)[0], 404)

        signed = bridge_headers(OWNER, "POST", "/api/access/grant", b'{"user_id": 202}')
        self.assertEqual(self.raw("/api/access/grant", signed, b'{"user_id": 303}')[0], 404)
        self.assertIsNone(self.store.role(EMPLOYEE))

    def test_expired_and_repeated_signatures_are_rejected(self):
        old = bridge_headers(OWNER, "GET", "/api/draft", stamp=int(time.time()) - 180)
        self.assertEqual(self.raw("/api/draft", old)[0], 404)
        header = bridge_headers(OWNER, "GET", "/api/draft")
        self.assertEqual(self.raw("/api/draft", header)[0], 200)
        self.server.application = EditorApplication(EditorStore(self.db, OWNER), TEST_KEY)
        self.assertEqual(self.raw("/api/draft", header)[0], 404)

    def test_owner_grants_and_revokes_editor_on_existing_page(self):
        self.assertEqual(self.call(EDITOR, "/api/draft")[0], 404)
        status, rights = self.call(OWNER, "/api/access/grant", {"user_id": EDITOR})
        self.assertEqual(status, 200)
        self.assertEqual(rights["editors"][0]["user_id"], EDITOR)
        self.assertEqual(self.call(EDITOR, "/api/draft")[1]["identity"]["role"], "editor")
        self.assertEqual(self.call(EDITOR, "/api/access")[0], 404)
        self.assertEqual(self.call(EDITOR, "/api/access/grant", {"user_id": EMPLOYEE})[0], 404)
        old_request = bridge_headers(EDITOR, "GET", "/api/draft")
        self.assertEqual(self.call(OWNER, "/api/access/revoke", {"user_id": EDITOR})[0], 200)
        self.assertEqual(self.raw("/api/draft", old_request)[0], 404)
        self.assertEqual([uid for _, uid in self.store.access_changes(0)], [EDITOR, EDITOR])

    def test_first_draft_can_be_edited_validated_and_previewed(self):
        status, draft = self.call(OWNER, "/api/draft")
        self.assertEqual(status, 200)
        self.assertEqual(len(draft["document"]["nodes"]), 102)
        doc = draft["document"]
        doc["nodes"][0]["title"] = "Локальный тест"
        status, saved = self.call(OWNER, "/api/draft", {"document": doc, "revision": draft["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], draft["revision"] + 1)
        status, preview = self.call(OWNER, "/api/preview", {"document": doc})
        self.assertEqual(status, 200)
        self.assertIn("Локальный тест", preview["screen"]["text"])
        self.assertEqual(self.call(OWNER, "/api/draft", {"document": doc, "revision": draft["revision"]})[0], 409)
        doc["nodes"][0]["options"][0]["next"] = "missing_node"
        self.assertEqual(self.call(OWNER, "/api/draft", {"document": doc, "revision": saved["revision"]})[0], 400)
        self.assertEqual(self.call(OWNER, "/api/draft")[1]["revision"], saved["revision"])
        self.assertEqual(self.call(OWNER, "/api/publish", {"revision": saved["revision"]})[0], 400)

    def test_task_mapping_publication_respects_access_and_field_catalog(self):
        from processes.flowdesk_chatbot.bitrix_worker import TASK_USER_FIELDS

        self.server.application.fields = lambda: {key: spec["label"] for key, spec in TASK_USER_FIELDS.items()}
        _, draft = self.call(OWNER, "/api/draft")
        self.assertNotIn("UF_FLOWDESK_INITIATOR_ID", draft["task_fields"])
        document = draft["document"]
        document["settings"]["mapping"] = {"UF_UNKNOWN": {"template": "{{request_type}}"}}
        payload = {"revision": draft["revision"], "document": document}
        self.assertEqual(self.call(EMPLOYEE, "/api/publish", payload)[0], 404)
        self.assertEqual(self.call(OWNER, "/api/publish", payload)[0], 400)
        self.assertEqual(self.store.active(), (None, None))
        self.assertEqual(self.call(OWNER, "/api/access/grant", {"user_id": EDITOR})[0], 200)
        document["settings"]["mapping"] = {"DESCRIPTION": {"template": "{{request_type}}\n{{description}}"}}
        status, saved = self.call(EDITOR, "/api/publish", payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.active(), (saved["active_version"], document))
        self.assertEqual(self.call(EDITOR, "/api/publish", payload)[0], 409)

    def test_group_launcher_does_not_receive_private_link(self):
        runtime = Runtime.__new__(Runtime)
        runtime.store = SessionStore(str(Path(self.temp.name) / "worker.sqlite3"))
        runtime.command_name = "flowdesk"
        runtime.bot_id = 777
        runtime.bot_token = "test"
        runtime.internal_editor = SimpleNamespace(store=self.store, entry_url=ENTRY)
        runtime.next_editor_access_check = 0.0
        runtime.last_editor_access_change = 0
        calls = []

        def record(method, params):
            calls.append((method, params))
            return {"id": 20 + len(calls), "result": True}

        runtime.call = record
        runtime.ensure_launcher_message(OWNER, "chat200")
        self.assertEqual(len(calls[-1][1]["fields"]["keyboard"]["BUTTONS"]), 1)
        runtime.ensure_launcher_message(OWNER, str(OWNER))
        self.assertEqual(calls[-1][0], "imbot.v2.Chat.Message.send")
        self.assertEqual(calls[-1][1]["dialogId"], str(OWNER))
        self.assertEqual(calls[-1][1]["fields"]["keyboard"]["BUTTONS"][-1]["LINK"], ENTRY)
        runtime.ensure_launcher_message(EMPLOYEE, str(EMPLOYEE))
        self.assertEqual(len(calls[-1][1]["fields"]["keyboard"]["BUTTONS"]), 1)

        self.store.set_access(OWNER, EMPLOYEE, True)
        runtime.refresh_editor_access()
        self.assertEqual(calls[-1][1]["fields"]["keyboard"]["BUTTONS"][-1]["LINK"], ENTRY)
        self.store.set_access(OWNER, EMPLOYEE, False)
        runtime.next_editor_access_check = 0.0
        runtime.refresh_editor_access()
        self.assertEqual(len(calls[-1][1]["fields"]["keyboard"]["BUTTONS"]), 1)
