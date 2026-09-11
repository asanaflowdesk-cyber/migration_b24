from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESS_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROCESS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import create_departments as mod


def test_extract_created_id_shapes():
    assert mod.extract_created_id(12) == 12
    assert mod.extract_created_id({"ID": "13"}) == 13
    assert mod.extract_created_id({"department": {"id": "14"}}) == 14
    assert mod.extract_created_id({}) is None


def test_load_config_rejects_duplicate(monkeypatch, tmp_path: Path):
    # load_config is intentionally confined to BASE_DIR. Put a temporary file there.
    path = mod.BASE_DIR / "_test_departments_duplicate.json"
    path.write_text(json.dumps([
        {"name": "Sales", "parent": 1},
        {"name": " sales ", "parent": 1},
    ]), encoding="utf-8")
    monkeypatch.setenv("INPUT_FILE_PATH", path.name)
    try:
        with pytest.raises(ValueError, match="Дубль"):
            mod.load_config()
    finally:
        path.unlink(missing_ok=True)


def test_load_config_rejects_path_escape(monkeypatch):
    monkeypatch.setenv("INPUT_FILE_PATH", "../../outside.json")
    with pytest.raises(ValueError, match="внутри processes/departments"):
        mod.load_config()
