"""Run the editor smoke test on a local portal fixture with synthetic accounts."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from processes.flowdesk_chatbot.bitrix_worker import TASK_USER_FIELDS
from processes.flowdesk_chatbot.engine import new_session
from processes.flowdesk_chatbot.flow_store import FlowStore
from processes.flowdesk_chatbot.internal_admin import EditorApplication, EditorServer, EditorStore
from processes.flowdesk_chatbot.test_internal_admin import ENTRY, OWNER, TEST_KEY, bridge_headers


def run_portal(editor):
    class FixturePortal(BaseHTTPRequestHandler):
        def handler(self):
            route = parse_qs(urlsplit(self.path).query).get("r", ["/"])[0]
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            uid = int(cookies["test_uid"].value) if "test_uid" in cookies else 0
            if not uid or self.command == "POST" and self.headers.get("X-DeskFlow-CSRF") != f"csrf-{uid}":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = Request(f"http://127.0.0.1:{editor.server_port}{route}",
                              data=body if self.command == "POST" else None,
                              headers={**bridge_headers(uid, self.command, route, body), "Content-Type": "application/json"})
            try:
                result = urlopen(request, timeout=5)
            except HTTPError as error:
                result = error
            with result:
                response = result.read()
                if route == "/" and result.status == 200:
                    response = response.replace(b"__DESKFLOW_CSRF__", f"csrf-{uid}".encode())
                self.send_response(result.status)
                self.send_header("Content-Type", result.headers.get("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(response)

        def log_message(self, *_args):
            pass

        do_GET = handler
        do_POST = handler

    with ThreadingHTTPServer(("127.0.0.1", 0), FixturePortal) as portal:
        threading.Thread(target=portal.serve_forever, daemon=True).start()
        try:
            return subprocess.run(["node", "qa/check_ui.cjs"], cwd=Path(__file__).resolve().parents[1],
                                  env={**os.environ, "FIXTURE_PORT": str(portal.server_port)}, check=False, timeout=25).returncode
        finally:
            portal.shutdown()


def main():
    with TemporaryDirectory() as temp:
        state = FlowStore(Path(temp) / "state.sqlite3")
        state.shown("chat303:303", new_session(303, "chat303"), "Тестовый экран сотрудника")
        store = EditorStore(Path(temp) / "editor.sqlite3", OWNER)
        app = EditorApplication(store, TEST_KEY, state, "https://portal.example",
                                lambda: {key: spec["label"] for key, spec in TASK_USER_FIELDS.items()})
        with EditorServer(("127.0.0.1", 0), app, ENTRY) as editor:
            threading.Thread(target=editor.serve_forever, daemon=True).start()
            try:
                return run_portal(editor)
            finally:
                editor.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
