from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from package_reconciler import package_items, utc_now, write_operation
from sync_founder_packages import is_founder_contact, normalized_id

BATCH_SIZE = 50


def _chunks(rows: list[Any], size: int = BATCH_SIZE):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _command(method: str, payload: dict[str, Any]) -> str:
    pairs: list[tuple[str, Any]] = []
    for key, value in payload.items():
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                pairs.append((f"{key}[{nested_key}]", nested_value))
        elif isinstance(value, (list, tuple)):
            for index, nested_value in enumerate(value):
                pairs.append((f"{key}[{index}]", nested_value))
        else:
            pairs.append((key, value))
    query = urlencode(pairs, doseq=True)
    return method if not query else f"{method}?{query}"


def _serial_call(client: Any, method: str, payload: dict[str, Any]) -> Any:
    if method.endswith(".update"):
        entity = method.split(".")[1]
        updater = getattr(client, f"update_{entity}", None)
        if updater is not None:
            updater(str(payload["id"]), payload.get("fields") or {})
            return True
    return client.call(method, payload)


def batch_execute(
    client: Any,
    operations: list[tuple[str, str, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Execute independent Bitrix calls in batches of 50.

    Returns successful results and sanitized per-command error names. If a test
    double or older portal cannot execute batch, the same commands fall back to
    serial calls without changing business semantics.
    """
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for group in _chunks(operations):
        commands = {key: _command(method, payload) for key, method, payload in group}
        try:
            response = client.call("batch", {"halt": 0, "cmd": commands})
            if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
                raise RuntimeError("invalid_batch_response")
            result_map = response.get("result") or {}
            error_map = response.get("result_error") or {}
            for key, _method, _payload in group:
                if key in error_map:
                    error_value = error_map[key]
                    if isinstance(error_value, dict):
                        errors[key] = str(error_value.get("error") or "batch_error")
                    else:
                        errors[key] = "batch_error"
                elif key in result_map:
                    results[key] = result_map[key]
                else:
                    errors[key] = "missing_batch_result"
        except Exception:
            for key, method, payload in group:
                try:
                    results[key] = _serial_call(client, method, payload)
                except Exception as exc:  # noqa: BLE001
                    errors[key] = type(exc).__name__
    return results, errors


def bulk_owners(
    client: Any,
    items: list[dict[str, Any]],
) -> tuple[dict[tuple[str, int], int | None], dict[tuple[str, int], str]]:
    operations: list[tuple[str, str, dict[str, Any]]] = []
    keys: list[tuple[str, int, str]] = []
    for index, item in enumerate(items):
        entity = str(item["entity"])
        item_id = int(item["id"])
        key = f"g{index}"
        keys.append((entity, item_id, key))
        operations.append((key, f"crm.{entity}.get", {"id": item_id}))
    raw, raw_errors = batch_execute(client, operations)
    owners: dict[tuple[str, int], int | None] = {}
    errors: dict[tuple[str, int], str] = {}
    for entity, item_id, key in keys:
        value = raw.get(key)
        tuple_key = (entity, item_id)
        if key in raw_errors:
            errors[tuple_key] = raw_errors[key]
            owners[tuple_key] = None
        elif not isinstance(value, dict):
            errors[tuple_key] = "target_not_found"
            owners[tuple_key] = None
        else:
            owners[tuple_key] = normalized_id(value.get("ASSIGNED_BY_ID"))
    return owners, errors


def _source_owner(client: Any, source_contact_id: int) -> tuple[int | None, str]:
    try:
        source = client.call("crm.contact.get", {"id": source_contact_id})
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__
    if not isinstance(source, dict):
        return None, "source_contact_not_found"
    if not is_founder_contact(source):
        return None, "source_missing_or_no_longer_founder"
    return normalized_id(source.get("ASSIGNED_BY_ID")), ""


def apply_rows_fast(
    client: Any,
    rows: list[dict[str, Any]],
    source_contact_id: int,
    target_owner_id: int,
) -> str:
    """Update one package using preflight/update/postflight batches.

    Every write chunk is fenced by a fresh source-owner check. A target changed
    by a human after our baseline is never overwritten.
    """
    for chunk_no, group in enumerate(_chunks(rows), start=1):
        owner_before, source_error = _source_owner(client, source_contact_id)
        if source_error or owner_before != target_owner_id:
            return source_error or "source_owner_changed_during_operation"

        current_owners, current_errors = bulk_owners(client, group)
        eligible: list[dict[str, Any]] = []
        for row in group:
            key = (row["entity"], row["id"])
            if key in current_errors:
                row["status"] = "error"
                row["error"] = current_errors[key]
                continue
            actual = current_owners.get(key)
            if actual == target_owner_id:
                row["status"] = "already_correct"
            elif actual != row["current"]:
                row["status"] = "error"
                row["error"] = "target_owner_changed_since_plan"
            else:
                eligible.append(row)

        operations = [
            (
                f"u{index}",
                f"crm.{row['entity']}.update",
                {"id": row["id"], "fields": {"ASSIGNED_BY_ID": target_owner_id}},
            )
            for index, row in enumerate(eligible)
        ]
        _update_results, update_errors = batch_execute(client, operations)
        for index, row in enumerate(eligible):
            key = f"u{index}"
            if key in update_errors:
                row["status"] = "error"
                row["error"] = update_errors[key]

        verify_rows = [row for row in eligible if row.get("status") != "error"]
        verified_owners, verify_errors = bulk_owners(client, verify_rows)
        for row in verify_rows:
            key = (row["entity"], row["id"])
            if key in verify_errors:
                row["status"] = "error"
                row["error"] = verify_errors[key]
            elif verified_owners.get(key) == target_owner_id:
                row["status"] = "updated"
            else:
                row["status"] = "error"
                row["error"] = "write_verification_failed"

        owner_after, source_error = _source_owner(client, source_contact_id)
        if source_error or owner_after != target_owner_id:
            return source_error or "source_owner_changed_during_write"
    return ""


def _journal_rows(
    items: list[dict[str, Any]],
    owners: dict[tuple[str, int], int | None],
    errors: dict[tuple[str, int], str],
    target_owner_id: int,
) -> list[dict[str, Any]]:
    journal: list[dict[str, Any]] = []
    for item in items:
        key = (item["entity"], item["id"])
        actual = owners.get(key)
        baseline = item.get("initial_owner_id")
        error = errors.get(key, "")
        if error:
            status = "MISSING" if error == "target_not_found" else "FAILED"
        elif actual == target_owner_id:
            status = "DONE"
        elif actual == baseline:
            status = "REMAINING"
        else:
            status = "CONFLICT"
        journal.append({
            "entity": item["entity"],
            "entity_id": item["id"],
            "title": item.get("title", ""),
            "from_owner_id": baseline or "",
            "from_owner_name": "",
            "to_owner_id": target_owner_id,
            "to_owner_name": "",
            "actual_owner_id": actual or "",
            "actual_owner_name": "",
            "status": status,
            "error": error,
            "updated_at": utc_now(),
        })
    return journal


def process_package_fast(
    client: Any,
    output_dir: Path,
    package: dict[str, Any],
    source_contact_id: int,
    operation_id: str,
    claim_id: str,
    attempt: int,
) -> dict[str, Any]:
    package = copy.deepcopy(package)
    owner_id, source_error = _source_owner(client, source_contact_id)
    if source_error == "source_missing_or_no_longer_founder":
        return {"success": True, "retryable": False, "reason": source_error, "status": "IGNORED", "operation": {}, "items": []}
    if source_error or not owner_id:
        return {"success": False, "retryable": False, "reason": source_error or "source_contact_without_owner", "status": "MANUAL_REVIEW", "operation": {}, "items": []}

    target_owner_id = owner_id
    package["source_contact_id"] = source_contact_id
    package["owner_id"] = target_owner_id
    items = package_items(package, source_contact_id)

    # The operation baseline is deliberately captured from live CRM, not from the
    # old package snapshot. This allows a final assignment such as 16→38→27 to
    # repair residual entities still owned by 16/14/32/45/38 without treating
    # those pre-existing differences as conflicts.
    baseline_owners, baseline_errors = bulk_owners(client, items)
    for item in items:
        key = (item["entity"], item["id"])
        item["initial_owner_id"] = baseline_owners.get(key)

    old_owner_ids = sorted({
        int(owner_id)
        for item in items
        if (owner_id := item.get("initial_owner_id")) and owner_id != target_owner_id
    })
    operation = {
        "operation_id": operation_id,
        "claim_id": claim_id,
        "contact_id": source_contact_id,
        "fio": str(package.get("fio") or ""),
        "from_owner_ids": ",".join(str(value) for value in old_owner_ids),
        "from_owner_names": "",
        "to_owner_id": target_owner_id,
        "to_owner_name": "",
        "started_at": utc_now(),
        "finished_at": "",
        "attempt": attempt,
        "package_contacts": max(len(package.get("contacts", [])) - 1, 0),
        "package_companies": len(package.get("companies", [])),
        "package_leads": sum(len(company.get("leads", [])) for company in package.get("companies", [])),
        "planned": 0,
        "updated": 0,
        "already_correct": 0,
        "remaining": 0,
        "conflicts": 0,
        "failed": len(baseline_errors),
        "status": "RUNNING",
        "last_error": "",
    }

    planned_rows: list[dict[str, Any]] = []
    for item in items:
        key = (item["entity"], item["id"])
        if key in baseline_errors:
            continue
        baseline = item.get("initial_owner_id")
        if baseline == target_owner_id:
            operation["already_correct"] += 1
            continue
        operation["planned"] += 1
        planned_rows.append({
            "fio": operation["fio"],
            "entity": item["entity"],
            "id": item["id"],
            "title": item.get("title", ""),
            "current": baseline,
            "target": target_owner_id,
            "source_contact_id": source_contact_id,
            "status": "planned",
        })

    stop_reason = ""
    if not baseline_errors and planned_rows:
        stop_reason = apply_rows_fast(client, planned_rows, source_contact_id, target_owner_id)

    final_owners, final_errors = bulk_owners(client, items)
    journal = _journal_rows(items, final_owners, final_errors, target_owner_id)
    remaining_items = [
        item for item, row in zip(items, journal)
        if row["status"] == "REMAINING"
    ]

    # One immediate reconciliation pass is enough for transient missed writes;
    # anything still remaining is returned as PARTIAL and will be re-queued.
    if remaining_items and not stop_reason:
        retry_rows = [{
            "fio": operation["fio"],
            "entity": item["entity"],
            "id": item["id"],
            "title": item.get("title", ""),
            "current": item.get("initial_owner_id"),
            "target": target_owner_id,
            "source_contact_id": source_contact_id,
            "status": "planned",
        } for item in remaining_items]
        stop_reason = apply_rows_fast(client, retry_rows, source_contact_id, target_owner_id)
        final_owners, final_errors = bulk_owners(client, items)
        journal = _journal_rows(items, final_owners, final_errors, target_owner_id)

    operation["remaining"] = sum(row["status"] == "REMAINING" for row in journal)
    operation["conflicts"] = sum(row["status"] == "CONFLICT" for row in journal)
    operation["failed"] = sum(row["status"] in {"FAILED", "MISSING"} for row in journal)
    operation["updated"] = sum(
        item.get("initial_owner_id") != target_owner_id and row["status"] == "DONE"
        for item, row in zip(items, journal)
    )
    if stop_reason:
        operation["status"] = "MANUAL_REVIEW" if "source_owner_changed" in stop_reason else "PARTIAL"
        operation["last_error"] = stop_reason
    elif operation["conflicts"] or operation["failed"]:
        operation["status"] = "MANUAL_REVIEW"
        operation["last_error"] = "conflict_or_missing_entity"
    elif operation["remaining"]:
        operation["status"] = "PARTIAL"
        operation["last_error"] = "remaining_after_fast_retry"
    else:
        operation["status"] = "DONE"

    operation["finished_at"] = utc_now()
    for row in journal:
        row["operation_id"] = operation_id
    write_operation(output_dir, operation, journal)

    success = operation["status"] == "DONE"
    retryable = operation["status"] == "PARTIAL" and not operation["conflicts"] and not operation["failed"]
    return {
        "success": success,
        "retryable": retryable,
        "reason": "" if success else operation["last_error"] or operation["status"].lower(),
        "status": operation["status"],
        "operation": operation,
        "items": journal,
    }
