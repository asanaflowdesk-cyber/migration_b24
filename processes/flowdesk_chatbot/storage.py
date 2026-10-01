from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def put_session(self, session_key: str, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(session_key, payload, updated_at)
                VALUES(?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (session_key, raw),
            )

    def delete_session(self, session_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def set_meta(self, key: str, value: str | int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
