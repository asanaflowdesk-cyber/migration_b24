from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_authority_sources(output_dir: Path) -> dict[str, int]:
    path = output_dir / "authority_sources.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid_authority_sources")
    return {str(key): int(value) for key, value in data.items() if int(value) > 0}


def remember_authority_sources(output_dir: Path, operations: list[dict[str, Any]]) -> None:
    accepted = [operation for operation in operations if operation.get("status") in {"ACCEPTED", "DONE"} and operation.get("fio") and operation.get("contact_id")]
    if not accepted:
        return
    sources = load_authority_sources(output_dir)
    for operation in accepted:
        sources[str(operation["fio"])] = int(operation["contact_id"])
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / "authority_sources.json.tmp"
    temporary.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_dir / "authority_sources.json")
