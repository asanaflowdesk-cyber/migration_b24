from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _json_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(value)


def _safe_preview_name(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "_" for char in str(value))
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "objects"


def _target_url(portal: str, target_type: str, target_id: Any, payload: Mapping[str, Any]) -> str:
    if not portal or not str(target_id).isdigit() or int(target_id) <= 0:
        return ""
    target_id = int(target_id)
    target_type = str(target_type).upper()
    if target_type == "COMPANY":
        return f"{portal}/crm/company/details/{target_id}/"
    if target_type == "CONTACT":
        return f"{portal}/crm/contact/details/{target_id}/"
    if target_type == "LEAD":
        return f"{portal}/crm/lead/details/{target_id}/"
    if target_type == "DEAL":
        return f"{portal}/crm/deal/details/{target_id}/"
    if target_type == "TASK":
        responsible = payload.get("RESPONSIBLE_ID") or payload.get("responsibleId") or 0
        if str(responsible).isdigit():
            return f"{portal}/company/personal/user/{int(responsible)}/tasks/task/view/{target_id}/"
    if target_type == "ACTIVITY":
        owner_type = str(payload.get("OWNER_TYPE_ID") or "")
        owner_id = payload.get("OWNER_ID")
        if str(owner_id).isdigit():
            prefix = {"1": "lead", "2": "deal", "3": "contact", "4": "company"}.get(owner_type)
            if prefix:
                return f"{portal}/crm/{prefix}/details/{int(owner_id)}/"
    return ""


class Report:
    MAP_NAMES = (
        "users", "companies", "contacts", "leads", "deals", "requisites", "addresses",
        "tasks", "activities", "files", "task_comments", "checklist_items",
    )

    def __init__(self, output_dir: str | Path):
        self.dir = Path(output_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.actions: list[dict[str, Any]] = []
        self.transfers: list[dict[str, Any]] = []
        self.transfer_fields: list[dict[str, Any]] = []
        self.relations: list[dict[str, Any]] = []
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
                self.maps[name].update({str(k): int(v) for k, v in current.items() if str(v).lstrip("-").isdigit()})

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

    def add_transfer(
        self,
        *,
        operation: str,
        source_type: str,
        source_id: Any,
        target_type: str,
        target_id: Any,
        status: str,
        payload: Mapping[str, Any],
        route: str = "",
    ) -> None:
        clean_payload = {
            str(code): value
            for code, value in payload.items()
            if value not in (None, "", [], {})
        }
        title = clean_payload.get("TITLE") or clean_payload.get("NAME") or clean_payload.get("SUBJECT") or ""
        payload_json = json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":"), default=str)
        base = {
            "operation": operation,
            "source_type": str(source_type),
            "source_id": str(source_id),
            "target_type": str(target_type),
            "target_id": str(target_id),
            "status": str(status),
            "route": str(route),
            "title": str(title),
            "field_count": len(clean_payload),
            "payload_json": payload_json,
        }
        self.transfers.append(base)
        for code, value in clean_payload.items():
            self.transfer_fields.append({
                "operation": operation,
                "source_type": str(source_type),
                "source_id": str(source_id),
                "target_type": str(target_type),
                "target_id": str(target_id),
                "status": str(status),
                "route": str(route),
                "field_code": str(code),
                "field_value": _json_value(value),
            })

    def add_relation(
        self,
        *,
        relation_type: str,
        source_from_type: str,
        source_from_id: Any,
        source_to_type: str,
        source_to_id: Any,
        target_from_type: str = "",
        target_from_id: Any = "",
        target_to_type: str = "",
        target_to_id: Any = "",
        status: str = "DRY_RUN",
        details: Any = "",
    ) -> None:
        self.relations.append({
            "relation_type": str(relation_type),
            "source_from_type": str(source_from_type),
            "source_from_id": str(source_from_id),
            "source_to_type": str(source_to_type),
            "source_to_id": str(source_to_id),
            "target_from_type": str(target_from_type),
            "target_from_id": str(target_from_id),
            "target_to_type": str(target_to_type),
            "target_to_id": str(target_to_id),
            "status": str(status),
            "details": _json_value(details),
        })

    @staticmethod
    def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def save(self) -> None:
        action_fields = ["time_utc", "operation", "source_type", "source_id", "target_type", "target_id", "status", "message"]
        self._write_csv(self.dir / "actions.csv", action_fields, self.actions)

        skipped = [row for row in self.actions if row["status"] == "SKIP"]
        warnings = [row for row in self.actions if row["status"] == "WARN"]
        errors = [row for row in self.actions if row["status"] in {"ERROR", "FATAL"}]
        self._write_csv(self.dir / "skipped.csv", action_fields, skipped)
        self._write_csv(self.dir / "warnings.csv", action_fields, warnings)
        self._write_csv(self.dir / "errors.csv", action_fields, errors)

        portal = str(self.extra.get("target_portal") or "").rstrip("/")
        for transfer in self.transfers:
            try:
                payload = json.loads(str(transfer.get("payload_json") or "{}"))
            except json.JSONDecodeError:
                payload = {}
            transfer["target_url"] = _target_url(
                portal,
                str(transfer.get("target_type") or ""),
                transfer.get("target_id"),
                payload,
            )

        transfer_fields = [
            "operation", "source_type", "source_id", "target_type", "target_id",
            "status", "route", "title", "field_count", "target_url", "payload_json",
        ]
        self._write_csv(self.dir / "transfer_register.csv", transfer_fields, self.transfers)
        created_rows = [
            {
                key: transfer.get(key, "")
                for key in (
                    "operation", "source_type", "source_id", "target_type",
                    "target_id", "status", "title", "target_url",
                )
            }
            for transfer in self.transfers
            if str(transfer.get("target_id") or "").isdigit()
            and int(str(transfer.get("target_id"))) > 0
        ]
        self._write_csv(
            self.dir / "created_objects.csv",
            [
                "operation", "source_type", "source_id", "target_type",
                "target_id", "status", "title", "target_url",
            ],
            created_rows,
        )
        field_fields = [
            "operation", "source_type", "source_id", "target_type", "target_id",
            "status", "route", "field_code", "field_value",
        ]
        self._write_csv(self.dir / "field_register.csv", field_fields, self.transfer_fields)
        relation_fields = [
            "relation_type", "source_from_type", "source_from_id", "source_to_type", "source_to_id",
            "target_from_type", "target_from_id", "target_to_type", "target_to_id", "status", "details",
        ]
        self._write_csv(self.dir / "relation_register.csv", relation_fields, self.relations)

        # Full dry-run/apply preview. actions.csv intentionally remains a compact
        # technical journal; these files contain the exact transformed payloads
        # that are sent (or would be sent) to the target Bitrix24.
        payload_rows: list[dict[str, Any]] = []
        grouped_previews: dict[str, list[dict[str, Any]]] = {}
        for transfer in self.transfers:
            try:
                payload = json.loads(str(transfer.get("payload_json") or "{}"))
            except json.JSONDecodeError:
                payload = {"_payload_json": transfer.get("payload_json", "")}
            record = {
                "operation": transfer.get("operation", ""),
                "source_type": transfer.get("source_type", ""),
                "source_id": transfer.get("source_id", ""),
                "target_type": transfer.get("target_type", ""),
                "target_id": transfer.get("target_id", ""),
                "status": transfer.get("status", ""),
                "route": transfer.get("route", ""),
                "title": transfer.get("title", ""),
                "target_url": transfer.get("target_url", ""),
                "payload": payload,
            }
            payload_rows.append(record)

            wide = {
                "operation": record["operation"],
                "source_type": record["source_type"],
                "source_id": record["source_id"],
                "target_type": record["target_type"],
                "target_id": record["target_id"],
                "status": record["status"],
                "route": record["route"],
                "target_url": record["target_url"],
            }
            for code, value in payload.items():
                wide[str(code)] = _json_value(value)
            grouped_previews.setdefault(str(record["target_type"]), []).append(wide)

        (self.dir / "full_transfer_preview.json").write_text(
            json.dumps(payload_rows, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        preview_index: list[dict[str, Any]] = []
        base_columns = [
            "operation", "source_type", "source_id", "target_type",
            "target_id", "status", "route", "target_url",
        ]
        for target_type, rows in sorted(grouped_previews.items()):
            dynamic_columns = sorted({key for row in rows for key in row if key not in base_columns})
            columns = base_columns + dynamic_columns
            filename = f"preview_{_safe_preview_name(target_type)}.csv"
            self._write_csv(self.dir / filename, columns, rows)
            preview_index.append({
                "target_type": target_type,
                "rows": len(rows),
                "fields": len(dynamic_columns),
                "file": filename,
            })
        self._write_csv(
            self.dir / "preview_index.csv",
            ["target_type", "rows", "fields", "file"],
            preview_index,
        )

        (self.dir / "maps.json").write_text(json.dumps(self.maps, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "counts_by_status": dict(Counter(row["status"] for row in self.actions)),
            "counts_by_operation": dict(Counter(row["operation"] for row in self.actions)),
            "skipped_total": len(skipped),
            "warnings_total": len(warnings),
            "errors_total": len(errors),
            "transfer_objects_total": len(self.transfers),
            "transfer_fields_total": len(self.transfer_fields),
            "relations_total": len(self.relations),
            "full_preview_files": preview_index,
            "transfer_objects_by_target_type": dict(Counter(row["target_type"] for row in self.transfers)),
            "transfer_fields_by_code": dict(Counter(row["field_code"] for row in self.transfer_fields)),
            "relations_by_type": dict(Counter(row["relation_type"] for row in self.relations)),
            "map_sizes": {name: len(values) for name, values in self.maps.items()},
            **self.extra,
        }
        (self.dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
