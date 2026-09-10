from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESS_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROCESS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import register_users_from_excel as mod


class FakeClient:
    def __init__(self, add_result=101):
        self.add_result = add_result
        self.add_calls = 0

    def list_all(self, method, params):
        if method == "department.get":
            return [{"ID": "10", "NAME": "Sales"}]
        if method == "user.get":
            return []
        raise AssertionError(method)

    def call(self, method, fields):
        assert method == "user.add"
        self.add_calls += 1
        return self.add_result


def make_user(row: int, email: str) -> mod.InputUser:
    return mod.InputUser(
        row_number=row,
        last_name="Иванов",
        name="Иван",
        second_name="Иванович",
        email=email,
        department="Sales",
        department_id="10",
    )


def test_extract_positive_id_rejects_empty_or_zero():
    assert mod.extract_positive_id("42") == "42"
    assert mod.extract_positive_id({"ID": 7}) == "7"
    with pytest.raises(RuntimeError):
        mod.extract_positive_id(0)
    with pytest.raises(RuntimeError):
        mod.extract_positive_id({})


def test_apply_does_not_create_two_accounts_for_same_fio(monkeypatch, tmp_path: Path):
    client = FakeClient(add_result=101)
    monkeypatch.setattr(mod, "read_users", lambda _: [make_user(2, "one@example.kz"), make_user(3, "two@example.kz")])
    monkeypatch.setattr(mod.BitrixClient, "from_env", lambda: client)
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(sys, "argv", ["register", "--file", "dummy.xlsx", "--mode", "apply"])

    rc = mod.main()

    assert rc == 1
    assert client.add_calls == 1
    rows = list(csv.DictReader((tmp_path / "out" / "actions.csv").open(encoding="utf-8-sig")))
    assert [row["status"] for row in rows] == ["OK", "ERROR"]
    # PII is redacted in reports by default.
    assert rows[0]["email"] == "[REDACTED]"
    assert rows[0]["full_name"] == "[REDACTED]"


def test_invalid_user_add_result_is_error(monkeypatch, tmp_path: Path):
    client = FakeClient(add_result=None)
    monkeypatch.setattr(mod, "read_users", lambda _: [make_user(2, "one@example.kz")])
    monkeypatch.setattr(mod.BitrixClient, "from_env", lambda: client)
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(sys, "argv", ["register", "--file", "dummy.xlsx", "--mode", "apply"])

    assert mod.main() == 1
    rows = list(csv.DictReader((tmp_path / "out" / "actions.csv").open(encoding="utf-8-sig")))
    assert rows[0]["status"] == "ERROR"
