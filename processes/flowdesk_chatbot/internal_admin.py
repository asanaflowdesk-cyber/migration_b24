"""Internal DeskFlow editor reached through the authenticated Bitrix portal page."""
from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
from hmac import compare_digest, digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from processes.flowdesk_chatbot import constructor_engine as preview_engine
from processes.flowdesk_chatbot.constructor_schema import (
    insert_before,
    move_before,
    remove_node,
    seed_document,
    validate_document,
)
from processes.flowdesk_chatbot.engine import new_session
from processes.flowdesk_chatbot.field_templates import PROTECTED_FIELDS, STANDARD_FIELDS
from processes.flowdesk_chatbot.resource_links import ResourceLinks

LOG = logging.getLogger("flowdesk_chatbot")
ENTRY_PATH = "/local/tools/deskflow-admin/index.php"
ASSETS = Path(__file__).with_name("internal_admin_assets")
MAX_BODY = 2_000_000
BRIDGE_WINDOW_SECONDS = 60
READ_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/graph.js": ("graph.js", "text/javascript; charset=utf-8"),
    "/api/draft": None,
    "/api/versions": None,
    "/api/access": None,
}
WRITE_ROUTES = {
    "/api/draft", "/api/validate", "/api/graph", "/api/preview",
    "/api/access/grant", "/api/access/revoke",
    "/api/publish", "/api/rollback", "/api/journal", "/api/journal/request", "/api/resource",
}
GENERIC_NOT_FOUND = "Страница не найдена"


class AccessDenied(Exception):
    pass


class RevisionConflict(ValueError):
    pass


def user_id(value: object) -> int:
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(value)):
        raise ValueError("Укажите числовой ID сотрудника из Б24")
    return int(value)


def document_errors(document: dict) -> list[str]:
    try:
        return validate_document(document)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return ["Неверная структура сценария"]


class EditorStore:
    def __init__(self, path: Path, owner_id: int):
        self.path = path
        self.owner_id = user_id(owner_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deskflow_editors (
                    user_id INTEGER PRIMARY KEY,
                    granted_by INTEGER NOT NULL,
                    granted_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deskflow_access_changes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deskflow_bridge_nonces (
                    nonce TEXT PRIMARY KEY,
                    expires INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deskflow_bridge_expiration
                    ON deskflow_bridge_nonces (expires);
                CREATE TABLE IF NOT EXISTS deskflow_editor_draft (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    document TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    saved_by INTEGER NOT NULL,
                    saved_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deskflow_editor_versions (
                    id INTEGER PRIMARY KEY, document TEXT NOT NULL, author INTEGER NOT NULL,
                    applied_at INTEGER NOT NULL, restored_from INTEGER
                );
                CREATE TABLE IF NOT EXISTS deskflow_editor_live (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
                );
            """)
            draft = db.execute("SELECT revision FROM deskflow_editor_draft WHERE singleton=1").fetchone()
            if not draft or draft[0] == 1 and not db.execute("SELECT 1 FROM deskflow_editor_live").fetchone():
                initial = seed_document()
                if document_errors(initial):
                    raise ValueError("Стартовый сценарий не прошёл проверку")
                db.execute(
                    "INSERT INTO deskflow_editor_draft VALUES(1,?,1,?,?) ON CONFLICT(singleton) DO UPDATE SET document=excluded.document WHERE revision=1",
                    (json.dumps(initial, ensure_ascii=False), self.owner_id, int(time.time())),
                )

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            yield db

    def role(self, who: int, db=None) -> str | None:
        if who == self.owner_id:
            return "owner"
        if db is None:
            with self.connection() as db:
                return self.role(who, db)
        if db.execute("SELECT 1 FROM deskflow_editors WHERE user_id=?", (who,)).fetchone():
            return "editor"
        return None

    def members(self, actor: int) -> dict:
        if actor != self.owner_id:
            raise AccessDenied()
        with self.connection() as db:
            rows = db.execute(
                "SELECT user_id,granted_by,granted_at FROM deskflow_editors ORDER BY user_id"
            ).fetchall()
        return {
            "owner_id": self.owner_id,
            "editors": [
                {"user_id": uid, "granted_by": by, "granted_at": at}
                for uid, by, at in rows if uid != self.owner_id
            ],
        }

    def set_access(self, actor: int, target: int, grant: bool) -> dict:
        if actor != self.owner_id:
            raise AccessDenied()
        if target == self.owner_id:
            raise ValueError("ID владельца задаётся на сервере DeskFlow")
        now = int(time.time())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if grant:
                changed = db.execute(
                    "INSERT OR IGNORE INTO deskflow_editors VALUES(?,?,?)", (target, actor, now)
                ).rowcount
            else:
                changed = db.execute("DELETE FROM deskflow_editors WHERE user_id=?", (target,)).rowcount
            if changed:
                db.execute(
                    "INSERT INTO deskflow_access_changes(user_id,actor_id,action,at) VALUES(?,?,?,?)",
                    (target, actor, "grant" if grant else "revoke", now),
                )
        return self.members(actor)

    def access_changes(self, after: int) -> list[tuple[int, int]]:
        with self.connection() as db:
            return db.execute(
                "SELECT seq,user_id FROM deskflow_access_changes WHERE seq>? ORDER BY seq LIMIT 30",
                (after,),
            ).fetchall()

    def latest_change(self) -> int:
        with self.connection() as db:
            return db.execute("SELECT COALESCE(MAX(seq),0) FROM deskflow_access_changes").fetchone()[0]

    def claim_nonce(self, nonce: str, signed_at: int, now: int) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM deskflow_bridge_nonces WHERE expires<?", (now,))
            changed = db.execute(
                "INSERT OR IGNORE INTO deskflow_bridge_nonces VALUES(?,?)",
                (nonce, signed_at + BRIDGE_WINDOW_SECONDS),
            ).rowcount
            if not changed:
                raise AccessDenied()

    def _read_draft(self, db, actor: int) -> dict:
        role = self.role(actor, db)
        if not role:
            raise AccessDenied()
        document, revision = db.execute(
            "SELECT document,revision FROM deskflow_editor_draft WHERE singleton=1"
        ).fetchone()
        active = db.execute("SELECT version FROM deskflow_editor_live WHERE singleton=1").fetchone()
        return {
            "active_version": active[0] if active else None,
            "document": json.loads(document),
            "revision": revision,
            "identity": {"id": actor, "role": role},
        }

    def draft(self, actor: int) -> dict:
        with self.connection() as db:
            return self._read_draft(db, actor)

    def active(self):
        with self.connection() as db:
            row = db.execute("""SELECT v.id,v.document FROM deskflow_editor_live l
                JOIN deskflow_editor_versions v ON l.version=v.id WHERE l.singleton=1""").fetchone()
        return (row[0], json.loads(row[1])) if row else (None, None)

    def active_version(self):
        with self.connection() as db:
            row = db.execute("SELECT version FROM deskflow_editor_live WHERE singleton=1").fetchone()
        return row[0] if row else None

    def versions(self, actor):
        with self.connection() as db:
            if not self.role(actor, db):
                raise AccessDenied()
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT id,author,applied_at,restored_from FROM deskflow_editor_versions ORDER BY id DESC LIMIT 100").fetchall()
        return {"versions": [dict(row) for row in rows]}

    def restore(self, actor, version, revision, validate_fields):
        with self.connection() as db:
            if not self.role(actor, db):
                raise AccessDenied()
            row = db.execute("SELECT document FROM deskflow_editor_versions WHERE id=?", (version,)).fetchone()
        if not row:
            raise ValueError("Версия не найдена")
        document = json.loads(row[0])
        validate_fields(document)
        return self.save(actor, document, revision, publish=True, restored_from=version)

    def save(self, actor: int, document: dict, revision: int, *, publish=False, restored_from=None) -> dict:
        if type(revision) is not int or revision < 1:
            raise ValueError("Обновите редактор и повторите сохранение")
        errors = document_errors(document)
        if errors:
            raise ValueError("\n".join(errors[:12]))
        payload = json.dumps(document, ensure_ascii=False, allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_BODY:
            raise ValueError("Сценарий слишком большой")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.role(actor, db):
                raise AccessDenied()
            changed = db.execute(
                """UPDATE deskflow_editor_draft SET document=?, revision=revision+1,
                    saved_by=?, saved_at=? WHERE singleton=1 AND revision=?""",
                (payload, actor, int(time.time()), revision),
            ).rowcount
            if not changed:
                raise RevisionConflict("Черновик изменился в другом окне. Обновите страницу.")
            if publish:
                version = db.execute("INSERT INTO deskflow_editor_versions(document,author,applied_at,restored_from) VALUES(?,?,?,?)",
                                     (payload, actor, int(time.time()), restored_from)).lastrowid
                db.execute("INSERT INTO deskflow_editor_live VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version", (version,))
            return self._read_draft(db, actor)


def signature_message(who: int, timestamp: int, nonce: str, method: str, path: str, body: bytes) -> bytes:
    return "\n".join((
        "deskflow-admin-v1", str(who), str(timestamp), nonce, method, path, sha256(body).hexdigest(),
    )).encode("ascii")


def response_signature(key: bytes, nonce: str, status: int, data: bytes) -> str:
    message = "\n".join(("deskflow-admin-response-v1", nonce, str(status), sha256(data).hexdigest()))
    return digest(key, message.encode("ascii"), "sha256").hex()


class EditorApplication:
    def __init__(self, store: EditorStore, key: bytes, flow_store=None, portal_base="", fields=None, task_formatter=None):
        self.store = store
        self.key = key
        self.flow_store = flow_store
        self.fields = fields
        self.task_formatter = task_formatter
        self.resources = ResourceLinks(key, portal_base, flow_store) if flow_store else None

    def task_fields(self):
        return {**STANDARD_FIELDS, **{key: label for key, label in (self.fields() if self.fields else {}).items() if key not in PROTECTED_FIELDS}}

    def check_task_fields(self, document):
        if errors := document_errors(document):
            raise ValueError("\n".join(errors[:12]))
        if self.fields is None:
            return
        configured = set(document["settings"].get("mapping", {}))
        configured.update(node["registry_field"] for node in document["nodes"] if node.get("registry_field"))
        missing = configured - self.task_fields().keys()
        if missing:
            raise ValueError("Поля задачи недоступны для записи: " + ", ".join(sorted(missing)))

    def authenticate(self, headers, method: str, path: str, body: bytes) -> int:
        for name in ("User", "Time", "Nonce", "Signature"):
            if len(headers.get_all("X-DeskFlow-" + name, [])) != 1:
                raise AccessDenied()
        try:
            who = user_id(headers["X-DeskFlow-User"])
            timestamp = int(headers["X-DeskFlow-Time"])
        except (ValueError, TypeError):
            raise AccessDenied() from None
        nonce = headers["X-DeskFlow-Nonce"]
        provided = headers["X-DeskFlow-Signature"]
        now = int(time.time())
        if (
            abs(now - timestamp) > BRIDGE_WINDOW_SECONDS
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or not re.fullmatch(r"[0-9a-f]{64}", provided)
        ):
            raise AccessDenied()
        expected = digest(self.key, signature_message(who, timestamp, nonce, method, path, body), "sha256")
        if not compare_digest(expected, bytes.fromhex(provided)) or path != "/api/resource" and not self.store.role(who):
            raise AccessDenied()
        self.store.claim_nonce(nonce, timestamp, now)
        return who

    def dispatch(self, who: int, method: str, path: str, body: dict) -> dict:
        if path == "/api/resource":
            location = self.resources.resolve(who, body.get("token")) if self.resources else None
            if not location:
                raise AccessDenied()
            return {"location": location}
        if path == "/api/publish":
            self.check_task_fields(body["document"])
            return self.store.save(who, body["document"], body["revision"], publish=True)
        if path == "/api/versions":
            return self.store.versions(who)
        if path == "/api/rollback":
            return self.store.restore(who, int(body["version"]), body["revision"], self.check_task_fields)
        if path == "/api/journal":
            offset = max(0, min(1_000_000, int(body.get("offset", 0))))
            return self.flow_store.journal(str(body.get("query", ""))[:200], str(body.get("status", ""))[:40], offset) if self.flow_store else {"items": [], "count": 0, "offset": 0}
        if path == "/api/journal/request":
            rid = body["request_id"]
            detail = self.flow_store.journal_detail(rid, max(0, int(body.get("after", 0)))) if self.flow_store and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", rid) else None
            if not detail:
                raise AccessDenied()
            return detail
        if path == "/api/draft":
            if method == "GET":
                return {**self.store.draft(who), "task_fields": self.task_fields()}
            return self.store.save(who, body["document"], body["revision"])
        if path == "/api/access":
            return self.store.members(who)
        if path in {"/api/access/grant", "/api/access/revoke"}:
            return self.store.set_access(who, user_id(body["user_id"]), path.endswith("/grant"))
        if path == "/api/validate":
            try:
                self.check_task_fields(body["document"])
            except ValueError as exc:
                return {"errors": str(exc).splitlines()}
            return {"errors": []}
        if path == "/api/graph":
            document = body["document"]
            action = body["action"]
            if action == "insert":
                document = insert_before(document, body["target"], body["node"])
            elif action == "delete":
                document = remove_node(document, body["node_id"], body["replacement"])
            elif action == "move":
                document = move_before(document, body["node_id"], body["target"])
            else:
                raise ValueError("Выберите действие со сценарием")
            return self.store.save(who, document, body["revision"])
        if path != "/api/preview":
            raise AccessDenied()

        task_preview = None
        session = body.get("session")
        if session:
            document = session["constructor"]["document"]
            errors = document_errors(document)
            if errors:
                return {"errors": errors}
            if body.get("files") is not None:
                result = preview_engine.attachments(session, body["files"], body.get("text", ""))
            elif body.get("action"):
                result = preview_engine.action(session, body["action"])
            else:
                result = preview_engine.text(session, body.get("text", ""))
            if result["status"] == "restart_requested":
                session = preview_engine.start(new_session(who, "preview"), document, "preview")
            elif result["status"] == "task_ready":
                try:
                    task_preview = self.task_formatter(session) if self.task_formatter else {
                        "TITLE": preview_engine.title(session), "DESCRIPTION": preview_engine.description(session),
                        **preview_engine.registry_fields(session), **preview_engine.task_mapping(session),
                    }
                except ValueError as exc:
                    result = {"status": "validation_error", "message": str(exc)}
                else:
                    task_preview.pop("XML_ID", None)
                    task_preview.pop("UF_TASK_WEBDAV_FILES", None)
                    if session["data"].get("document_file_names"):
                        task_preview["Вложения"] = session["data"]["document_file_names"]
                    preview_engine.created(session, "ПРОБА")
        else:
            document = body["document"]
            errors = document_errors(document)
            if errors:
                return {"errors": errors}
            session = preview_engine.start(new_session(who, "preview"), document, "preview")
            result = {"status": "ok"}
        return {"session": session, "screen": preview_engine.render(session), "result": result, "task_preview": task_preview}


class EditorHandler(BaseHTTPRequestHandler):
    server_version = "Internal"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, *_args) -> None:
        pass

    def respond(self, code: int, content, content_type="application/json; charset=utf-8") -> None:
        data = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.authenticated_nonce:
            signature = response_signature(self.server.application.key, self.authenticated_nonce, code, data)
            self.send_header("X-DeskFlow-Response-Signature", signature)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self) -> None:
        self.authenticated_nonce = None
        allowed = READ_ROUTES if self.command == "GET" else WRITE_ROUTES
        if self.path not in allowed:
            self.respond(404, {"error": GENERIC_NOT_FOUND})
            return
        try:
            sizes = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(sizes) > 1:
                raise AccessDenied()
            length_text = sizes[0] if sizes else "0"
            if not re.fullmatch(r"[0-9]{1,8}", length_text):
                raise AccessDenied()
            size = int(length_text)
            if size > MAX_BODY or (self.command == "GET" and size):
                raise AccessDenied()
            if self.command == "POST" and self.headers.get_content_type() != "application/json":
                raise AccessDenied()
            body = self.rfile.read(size)
            if len(body) != size:
                raise AccessDenied()
            app = self.server.application
            who = app.authenticate(self.headers, self.command, self.path, body)
            self.authenticated_nonce = self.headers["X-DeskFlow-Nonce"]
            asset = READ_ROUTES.get(self.path)
            if asset:
                filename, mime = asset
                self.respond(200, (ASSETS / filename).read_bytes(), mime)
                return
            data = json.loads(body) if self.command == "POST" else {}
            if not isinstance(data, dict):
                raise ValueError("Ожидался объект JSON")
            self.respond(200, app.dispatch(who, self.command, self.path, data))
        except AccessDenied:
            self.respond(404, {"error": GENERIC_NOT_FOUND})
        except RevisionConflict as exc:
            self.respond(409, {"error": str(exc)})
        except (json.JSONDecodeError, UnicodeError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            self.respond(400, {"error": "Проверьте данные запроса"})
        except ValueError as exc:
            self.respond(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except (OSError, sqlite3.Error) as exc:
            LOG.error("DeskFlow editor request failed (%s)", type(exc).__name__)
            self.respond(503, {"error": "Редактор временно недоступен"})

    do_GET = handle_request
    do_POST = handle_request


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, application: EditorApplication, entry_url: str):
        self.application = application
        self.store = application.store
        self.entry_url = entry_url
        super().__init__(address, EditorHandler)


def start_editor(portal_base: str, state_path: str, flow_store=None, fields=None, task_formatter=None) -> EditorServer | None:
    raw_owner = os.getenv("FLOWDESK_ADMIN_OWNER_ID", "").strip()
    if not raw_owner:
        if Path(state_path).with_name("deskflow_editor.sqlite3").exists():
            raise ValueError("Восстановите ID владельца и ключ существующего редактора")
        return None
    owner_id = user_id(raw_owner)
    raw_key = os.getenv("FLOWDESK_ADMIN_BRIDGE_KEY", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw_key):
        raise ValueError("FLOWDESK_ADMIN_BRIDGE_KEY должен содержать 64 шестнадцатеричных символа")
    portal = urlsplit(portal_base)
    if portal.scheme != "https" or not portal.hostname or portal.username or portal.password:
        raise ValueError("Для страницы управления требуется HTTPS-адрес Б24")
    port = int(os.getenv("FLOWDESK_ADMIN_LISTEN_PORT", "").strip() or "8765")
    if not 1 <= port <= 65535:
        raise ValueError("Неверный порт редактора")

    store = EditorStore(Path(state_path).with_name("deskflow_editor.sqlite3"), owner_id)
    server = EditorServer(
        ("127.0.0.1", port), EditorApplication(store, bytes.fromhex(raw_key), flow_store, portal_base, fields, task_formatter),
        portal_base.rstrip("/") + ENTRY_PATH,
    )
    try:
        threading.Thread(target=server.serve_forever, name="deskflow-admin", daemon=True).start()
    except Exception:
        server.server_close()
        raise
    return server
