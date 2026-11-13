"""Conversation journal and task receipts, stored with the existing worker state."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from processes.flowdesk_chatbot import constructor_engine as graph
from processes.flowdesk_chatbot.storage import SessionStore

COLUMNS = "request_id,user_id,dialog_id,version,scenario,step,status,updated_at,task_id,delivery_state,error"
DELIVERY_STATUS = {"lookup": "pending", "sending": "pending", "unknown": "needs_check", "rejected": "failed", "created": "created"}


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def step_label(session):
    if graph.is_custom(session):
        return (graph.node(session).get("label") or graph.node(session)["id"])[:250]
    step = str(session.get("current_screen") or "")
    initiative = session.get("data", {}).get("initiative")
    return step + " · " + str(initiative.get("step", "")) if step.startswith("initiative_") and isinstance(initiative, dict) else step


class FlowStore(SessionStore):
    def _connect(self):
        conn = super()._connect()
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def __init__(self, path):
        super().__init__(str(path))
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS deskflow_requests (
                    request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, dialog_id TEXT NOT NULL,
                    version INTEGER, scenario TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
                    updated_at INTEGER NOT NULL, payload TEXT NOT NULL, confirmation TEXT,
                    task_id TEXT NOT NULL DEFAULT '', delivery_state TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS deskflow_requests_by_user ON deskflow_requests(user_id,status,updated_at);
                CREATE TABLE IF NOT EXISTS deskflow_events (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT NULL,
                    step TEXT NOT NULL, detail TEXT NOT NULL, at INTEGER NOT NULL, UNIQUE(request_id,event_key)
                );
            """)

    @staticmethod
    def _event(db, rid, key, kind, step, detail):
        db.execute("INSERT OR IGNORE INTO deskflow_events(request_id,event_key,kind,step,detail,at) VALUES(?,?,?,?,?,?)",
                   (rid, key, kind, step, dump(detail), int(time.time())))

    def _save(self, db, key, session):
        raw = dump(session)
        self._put_session(db, key, raw)
        if not session.get("request_id") or not session.get("user_id"):
            return
        state = session.get("constructor") or {}
        status = state.get("status", "in_progress")
        if session.get("current_screen") == "initiative_saved":
            status = "draft"
        elif status == "in_progress" and (session.get("current_screen") == "instruction" or (
            state and graph.node(session)["kind"] == "info" and not graph.next_node(graph.node(session), state["answers"])
        )):
            status = "information"
        if session.get("task_id"):
            status = "created"
        db.execute("""INSERT INTO deskflow_requests(request_id,user_id,dialog_id,version,scenario,step,status,updated_at,payload,task_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET dialog_id=excluded.dialog_id,
                        scenario=excluded.scenario,step=excluded.step,payload=excluded.payload,
                        status=CASE WHEN deskflow_requests.delivery_state='' THEN excluded.status ELSE deskflow_requests.status END,
                        updated_at=CASE WHEN excluded.payload<>deskflow_requests.payload THEN excluded.updated_at ELSE deskflow_requests.updated_at END,
                        task_id=COALESCE(NULLIF(excluded.task_id,''),deskflow_requests.task_id)""",
                   (session["request_id"], session["user_id"], session["dialog_id"], state.get("version"),
                    str(session["data"].get("request_type") or "")[:200], step_label(session), status,
                    int(time.time()), raw, str(session.get("task_id") or "")))

    def put_session(self, key, session, event=None):
        with self._connection() as db:
            self._save(db, key, session)
            if event:
                self._event(db, session["request_id"], *event)

    def shown(self, key, session, text):
        step = step_label(session)
        self.put_session(key, session, (f"screen:{session['revision']}", "screen", step, {"text": text}))

    def seen(self, rid, key):
        with self._connection() as db:
            return db.execute("SELECT 1 FROM deskflow_events WHERE request_id=? AND event_key=?", (rid, key)).fetchone() is not None

    def abandon(self, session):
        with self._connection() as db:
            if db.execute("UPDATE deskflow_requests SET status='interrupted' WHERE request_id=? AND status='in_progress' AND delivery_state=''", (session.get("request_id"),)).rowcount:
                self._event(db, session["request_id"], "interrupted", "interrupted", step_label(session), {})

    def mark_idle(self, hours):
        with self._connection() as db:
            db.execute("UPDATE deskflow_requests SET status='interrupted' WHERE status='in_progress' AND updated_at<?", (int(time.time()) - hours * 3600,))

    def drafts(self, uid, limit=100):
        with self._connection() as db:
            rows = db.execute("""SELECT request_id,scenario,step FROM deskflow_requests WHERE user_id=? AND status='draft'
                                  AND version IS NOT NULL ORDER BY updated_at DESC,request_id LIMIT ?""", (uid, limit)).fetchall()
        return [{"id": rid, "label": scenario or step} for rid, scenario, step in rows]

    def request_for_user(self, rid, uid, status):
        with self._connection() as db:
            row = db.execute("SELECT payload FROM deskflow_requests WHERE request_id=? AND user_id=? AND status=?",
                             (rid, uid, status)).fetchone()
        return json.loads(row[0]) if row else None

    def pending_for_user(self, uid):
        with self._connection() as db:
            row = db.execute("""SELECT confirmation FROM deskflow_requests WHERE user_id=? AND task_id=''
                AND delivery_state IN ('lookup','sending','rejected','unknown') ORDER BY updated_at DESC,request_id LIMIT 1""", (uid,)).fetchone()
        return json.loads(row[0]) if row else None

    def delivery(self, rid):
        with self._connection() as db:
            return db.execute("SELECT delivery_state,task_id,error FROM deskflow_requests WHERE request_id=?", (rid,)).fetchone() or ("", "", "")

    def begin_delivery(self, key, session, event):
        rid = session["request_id"]
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._save(db, key, session)
            state, task, confirmed = db.execute("SELECT delivery_state,task_id,confirmation FROM deskflow_requests WHERE request_id=?", (rid,)).fetchone()
            if confirmed is None and not task:
                db.execute("UPDATE deskflow_requests SET confirmation=?,delivery_state='lookup',status='pending' WHERE request_id=?", (dump(session), rid))
                self._event(db, rid, "confirm", "confirmed", step_label(session), {})
                state = "lookup"
            if event:
                self._event(db, rid, *event)
        return state, task, json.loads(confirmed) if confirmed else session

    def claim_delivery(self, rid):
        with self._connection() as db:
            count = db.execute("UPDATE deskflow_requests SET delivery_state='sending',status='pending',error='' WHERE request_id=? AND task_id='' AND delivery_state IN ('lookup','rejected')", (rid,)).rowcount
            if count:
                self._event(db, rid, f"send:{time.time_ns()}", "task_send", "", {})
            return bool(count)

    def delivery_result(self, rid, state, *, task="", error=""):
        with self._connection() as db:
            count = db.execute("UPDATE deskflow_requests SET delivery_state=?,status=?,task_id=?,error=?,updated_at=? WHERE request_id=? AND task_id=''",
                               (state, DELIVERY_STATUS[state], task, error[:500], int(time.time()), rid)).rowcount
            if count:
                self._event(db, rid, f"result:{time.time_ns()}", "task_" + state, "", {"task_id": task, "message": error[:500]})

    def recover_deliveries(self):
        with self._connection() as db:
            db.execute("UPDATE deskflow_requests SET delivery_state='unknown',status='needs_check',error=? WHERE delivery_state='sending' AND task_id=''",
                       ("Отправка прервалась. Проверьте наличие задачи.",))

    def journal(self, query="", status="", offset=0):
        where, values = ["1=1"], []
        if query:
            where.append("""(request_id LIKE ? OR scenario LIKE ? OR CAST(user_id AS TEXT)=? OR EXISTS
                (SELECT 1 FROM deskflow_events e WHERE e.request_id=deskflow_requests.request_id AND e.detail LIKE ?))""")
            values.extend([f"%{query}%", f"%{query}%", query, f"%{query}%"])
        if status:
            where.append("status=?")
            values.append(status)
        predicate = " AND ".join(where)
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            count = db.execute("SELECT COUNT(*) FROM deskflow_requests WHERE " + predicate, values).fetchone()[0]
            rows = db.execute(f"SELECT {COLUMNS} FROM deskflow_requests WHERE {predicate} ORDER BY updated_at DESC,request_id LIMIT 50 OFFSET ?", [*values, offset]).fetchall()
        return {"items": [dict(row) for row in rows], "count": count, "offset": offset}

    def journal_detail(self, rid, after=0):
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(f"SELECT {COLUMNS},COALESCE(confirmation,payload) AS payload FROM deskflow_requests WHERE request_id=?", (rid,)).fetchone()
            events = db.execute("SELECT id,kind,step,detail,at FROM deskflow_events WHERE request_id=? AND id>? ORDER BY id LIMIT 51", (rid, after)).fetchall()
        if not row:
            return None
        result = dict(row)
        session = json.loads(result.pop("payload"))
        result["answers"] = graph.summaries(session) if graph.is_custom(session) else list(session.get("data", {}).items())
        result["events"] = [{**dict(event), "detail": json.loads(event["detail"])} for event in events[:50]]
        result["next"] = events[49]["id"] if len(events) > 50 else None
        return result

    def resource_opened(self, key, rid, uid, revision, option, kind, location):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM sessions WHERE session_key=?", (key,)).fetchone()
            if not row:
                return False
            session = json.loads(row[0])
            if (session.get("request_id"), session.get("user_id"), session.get("revision")) != (rid, uid, revision):
                return False
            self._event(db, rid, f"resource:{revision}:{option}", kind, step_label(session), {"url": location})
            return True


@contextmanager
def worker_lock(path):
    path = Path(path).resolve().with_suffix(".worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
