from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from processes.flowdesk_chatbot.storage import SessionStore


class DeskFlowStorageTests(unittest.TestCase):
    def test_corrupt_session_is_discarded_instead_of_crashing_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            store = SessionStore(str(db))

            with closing(sqlite3.connect(db)) as conn:
                conn.execute(
                    "INSERT INTO sessions(session_key, payload) VALUES(?, ?)",
                    ("chat1:1", "{not-json"),
                )
                conn.commit()

            self.assertIsNone(store.get_session("chat1:1"))
            self.assertIsNone(store.get_session("chat1:1"))

    def test_non_object_session_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            store = SessionStore(str(db))

            with closing(sqlite3.connect(db)) as conn:
                conn.execute(
                    "INSERT INTO sessions(session_key, payload) VALUES(?, ?)",
                    ("chat2:2", "[]"),
                )
                conn.commit()

            self.assertIsNone(store.get_session("chat2:2"))


if __name__ == "__main__":
    unittest.main()
