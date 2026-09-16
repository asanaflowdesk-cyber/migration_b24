from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sync_founder_packages import apply_updates, build_update_rows, is_founder_contact, normalized_id

ProgressCallback = Callable[[dict[str, Any], list[dict[str, Any]]], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def user_name(client: Any, user_id: int | None, cache: dict[int, str]) -> str:
    if not user_id:
        return ""
    if user_id in cache:
        return cache[user_id]
    try:
        user = client.get_user(user_id)
    except Exception:
        user = None
    if not isinstance(user, dict):
        cache[user_id] = str(user_id)
        return cache[user_id]
    parts = [
        str(user.get("LAST_NAME") or "").strip(),
        str(user.get("NAME") or "").strip(),
        str(user.get("SECOND_NAME") or "").strip(),
    ]
    cache[user_id] = " ".join(part for part in parts if part) or str(user_id)
    return cache[user_id]


def package_items(package: dict[str, Any], source_contact_id: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for contact in package.get("contacts", []):
        item_id = normalized_id(contact.get("id"))
        if item_id and item_id != source_contact_id:
            rows.append({"entity": "contact", "id": item_id, "title": str(contact.get("title") or "").strip(), "initial_owner_id": normalized_id(contact.get("owner_id"))})
    for company in package.get("companies", []):
        company_id = normalized_id(company.get("id"))
        if company_id:
            rows.append({"entity": "company", "id": company_id, "title": str(company.get("title") or "").strip(), "initial_owner_id": normalized_id(company.get("owner_id"))})
        for lead in company.get("leads", []):
            lead_id = normalized_id(lead.get("id"))
            if lead_id:
                rows.append({"entity": "lead", "id": lead_id, "title": str(lead.get("title") or "").strip(), "initial_owner_id": normalized_id(lead.get("owner_id"))})
    return rows


def live_owner(client: Any, entity: str, item_id: int) -> tuple[int | None, str]:
    try:
        current = client.call(f"crm.{entity}.get", {"id": item_id})
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__
    if not isinstance(current, dict):
        return None, "target_not_found"
    return normalized_id(current.get("ASSIGNED_BY_ID")), ""


def reconcile_items(
    client: Any,
    items: list[dict[str, Any]],
    source_contact_id: int,
    fio: str,
    target_owner_id: int,
    user_cache: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    retry_rows: list[dict[str, Any]] = []
    journal_items: list[dict[str, Any]] = []
    for item in items:
        actual_owner_id, error = live_owner(client, item["entity"], item["id"])
        initial_owner_id = item.get("initial_owner_id")
        if error:
            status = "MISSING" if error == "target_not_found" else "FAILED"
        elif actual_owner_id == target_owner_id:
            status = "DONE"
        elif actual_owner_id == initial_owner_id:
            status = "REMAINING"
            retry_rows.append({
                "fio": fio,
                "entity": item["entity"],
                "id": item["id"],
                "title": item.get("title", ""),
                "current": actual_owner_id,
                "target": target_owner_id,
                "source_contact_id": source_contact_id,
                "status": "planned",
            })
        else:
            status = "CONFLICT"
        journal_items.append({
            "entity": item["entity"],
            "entity_id": item["id"],
            "title": item.get("title", ""),
            "from_owner_id": initial_owner_id or "",
            "from_owner_name": user_name(client, initial_owner_id, user_cache),
            "to_owner_id": target_owner_id,
            "to_owner_name": user_name(client, target_owner_id, user_cache),
            "actual_owner_id": actual_owner_id or "",
            "actual_owner_name": user_name(client, actual_owner_id, user_cache),
            "status": status,
            "error": error,
            "updated_at": utc_now(),
        })
    return retry_rows, journal_items


def write_operation(output_dir: Path, operation: dict[str, Any], items: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "operation.json").write_text(
        json.dumps({"operation": operation, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def process_package(
    client: Any,
    output_dir: Path,
    package: dict[str, Any],
    source_contact_id: int,
    operation_id: str,
    claim_id: str,
    attempt: int,
    progress: ProgressCallback | None = None,
    max_reconcile_rounds: int = 3,
) -> dict[str, Any]:
    package = copy.deepcopy(package)
    source = client.call("crm.contact.get", {"id": source_contact_id})
    if not isinstance(source, dict):
        return {"success": False, "retryable": False, "reason": "source_contact_not_found", "status": "MANUAL_REVIEW", "operation": {}, "items": []}
    if not is_founder_contact(source):
        return {"success": True, "retryable": False, "reason": "not_founder_or_director", "status": "IGNORED", "operation": {}, "items": []}
    target_owner_id = normalized_id(source.get("ASSIGNED_BY_ID"))
    if not target_owner_id:
        return {"success": False, "retryable": False, "reason": "source_contact_without_owner", "status": "MANUAL_REVIEW", "operation": {}, "items": []}

    package["source_contact_id"] = source_contact_id
    package["owner_id"] = target_owner_id
    fio = str(package.get("fio") or "")
    items = package_items(package, source_contact_id)
    user_cache: dict[int, str] = {}
    old_owner_ids = sorted({int(owner_id) for item in items if (owner_id := item.get("initial_owner_id")) and owner_id != target_owner_id})
    operation = {
        "operation_id": operation_id,
        "claim_id": claim_id,
        "contact_id": source_contact_id,
        "fio": fio,
        "from_owner_ids": ",".join(str(value) for value in old_owner_ids),
        "from_owner_names": "; ".join(user_name(client, value, user_cache) for value in old_owner_ids),
        "to_owner_id": target_owner_id,
        "to_owner_name": user_name(client, target_owner_id, user_cache),
        "started_at": utc_now(),
        "finished_at": "",
        "attempt": attempt,
        "package_contacts": max(len(package.get("contacts", [])) - 1, 0),
        "package_companies": len(package.get("companies", [])),
        "package_leads": sum(len(company.get("leads", [])) for company in package.get("companies", [])),
        "planned": sum(item.get("initial_owner_id") != target_owner_id for item in items),
        "updated": 0,
        "already_correct": sum(item.get("initial_owner_id") == target_owner_id for item in items),
        "remaining": 0,
        "conflicts": 0,
        "failed": 0,
        "status": "RUNNING",
        "last_error": "",
    }

    _, journal_items = reconcile_items(client, items, source_contact_id, fio, target_owner_id, user_cache)
    write_operation(output_dir, operation, journal_items)
    if progress:
        progress(operation, journal_items)

    planned_rows = build_update_rows([package])
    if planned_rows:
        apply_updates(client, planned_rows)
    last_error = next((str(row.get("error") or "") for row in planned_rows if row.get("status") == "error"), "")

    for round_no in range(max_reconcile_rounds + 1):
        source_now = client.call("crm.contact.get", {"id": source_contact_id})
        if not isinstance(source_now, dict) or not is_founder_contact(source_now):
            operation["status"] = "MANUAL_REVIEW"
            operation["last_error"] = "source_missing_or_no_longer_founder"
            break
        if normalized_id(source_now.get("ASSIGNED_BY_ID")) != target_owner_id:
            operation["status"] = "MANUAL_REVIEW"
            operation["last_error"] = "source_owner_changed_during_operation"
            break

        retry_rows, journal_items = reconcile_items(client, items, source_contact_id, fio, target_owner_id, user_cache)
        operation["remaining"] = sum(item["status"] == "REMAINING" for item in journal_items)
        operation["conflicts"] = sum(item["status"] == "CONFLICT" for item in journal_items)
        operation["failed"] = sum(item["status"] in {"FAILED", "MISSING"} for item in journal_items)
        operation["updated"] = sum(item.get("initial_owner_id") != target_owner_id and row["status"] == "DONE" for item, row in zip(items, journal_items))
        operation["last_error"] = last_error
        write_operation(output_dir, operation, journal_items)
        if progress:
            progress(operation, journal_items)

        if operation["conflicts"] or operation["failed"]:
            operation["status"] = "MANUAL_REVIEW"
            operation["last_error"] = operation["last_error"] or "conflict_or_missing_entity"
            break
        if not retry_rows:
            operation["status"] = "DONE"
            operation["last_error"] = ""
            break
        if round_no >= max_reconcile_rounds:
            operation["status"] = "PARTIAL"
            operation["last_error"] = operation["last_error"] or "remaining_after_retries"
            break

        apply_updates(client, retry_rows)
        last_error = next((str(row.get("error") or "") for row in retry_rows if row.get("status") == "error"), last_error)

    operation["finished_at"] = utc_now()
    _, journal_items = reconcile_items(client, items, source_contact_id, fio, target_owner_id, user_cache)
    operation["remaining"] = sum(item["status"] == "REMAINING" for item in journal_items)
    operation["conflicts"] = sum(item["status"] == "CONFLICT" for item in journal_items)
    operation["failed"] = sum(item["status"] in {"FAILED", "MISSING"} for item in journal_items)
    operation["updated"] = sum(item.get("initial_owner_id") != target_owner_id and row["status"] == "DONE" for item, row in zip(items, journal_items))
    if operation["status"] == "DONE" and (operation["remaining"] or operation["conflicts"] or operation["failed"]):
        operation["status"] = "PARTIAL"
        operation["last_error"] = "final_reconciliation_not_clean"
    for row in journal_items:
        row["operation_id"] = operation_id
    write_operation(output_dir, operation, journal_items)
    if progress:
        progress(operation, journal_items)

    success = operation["status"] == "DONE"
    retryable = operation["status"] == "PARTIAL" and operation["conflicts"] == 0 and operation["failed"] == 0
    return {
        "success": success,
        "retryable": retryable,
        "reason": "" if success else operation.get("last_error") or operation["status"].lower(),
        "status": operation["status"],
        "operation": operation,
        "items": journal_items,
    }
