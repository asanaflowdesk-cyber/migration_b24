from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Report:
    MAP_NAMES = (
        "users", "companies", "contacts", "leads", "deals", "requisites", "addresses",
        "tasks", "activities", "files", "task_comments", "checklist_items",
    )

    def __init__(self, output_dir: str | Path):
        self.dir = Path(output_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.actions: list[dict[str, Any]] = []
        self.maps: dict[str, dict[str, int]] = {name: {} for name in self.MAP_NAMES}
        self.extra: dict[str, Any] = {}
        self._load_existing_maps()

    def _load_existing_maps(self) -> None:
        path = self.dir / "maps.json"
        if not path.exists():
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(value, dict):
            return
        for name in self.MAP_NAMES:
            current = value.get(name)
            if isinstance(current, dict):
                self.maps[name].update({str(k): int(v) for k, v in current.items() if str(v).isdigit()})

    def add(
        self,
        operation: str,
        source_type: str,
        source_id: Any,
        target_type: str,
        target_id: Any = "",
        status: str = "OK",
        message: str = "",
    ) -> None:
        self.actions.append({
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "source_type": source_type,
            "source_id": str(source_id),
            "target_type": target_type,
            "target_id": str(target_id),
            "status": status,
            "message": message,
        })

    def save(self) -> None:
        fields = ["time_utc", "operation", "source_type", "source_id", "target_type", "target_id", "status", "message"]
        with (self.dir / "actions.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.actions)
        errors = [row for row in self.actions if row["status"] not in {"OK", "SKIP", "DRY_RUN"}]
        with (self.dir / "errors.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(errors)
        (self.dir / "maps.json").write_text(json.dumps(self.maps, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "counts_by_status": dict(Counter(row["status"] for row in self.actions)),
            "counts_by_operation": dict(Counter(row["operation"] for row in self.actions)),
            "map_sizes": {name: len(values) for name, values in self.maps.items()},
            **self.extra,
        }
        (self.dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
