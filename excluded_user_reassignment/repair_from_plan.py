from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any

from eqazyna_bitrix.bitrix_client import BitrixClient, BitrixError
from eqazyna_bitrix.settings import Settings

from reassign_excluded_users import NEW_STATUS_ID, ReassignmentError, normalized_id


SUPPORTED = {"lead", "company", "contact"}


def build_client() -> BitrixClient:
    settings = Settings.from_env()
    if not settings.bitrix_webhook_url:
        raise ReassignmentError("Не задан TARGET_BITRIX_WEBHOOK_URL")
    return BitrixClient(
        settings.bitrix_webhook_url,
        timeout=settings.bitrix_request_timeout,
        retries=5,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )


def progress_every() -> int:
    try:
        return max(1, int(os.getenv("REASSIGN_PROGRESS_EVERY", "25")))
    except ValueError:
        return 25


def progress(label: str, done: int, total: int, *, ok: int = 0, errors: int = 0) -> None:
    if total <= 0:
        return
    every = progress_every()
    if done not in {1, total} and done % every:
        return
    print(
        f"[{label}] {done}/{total} ({done / total * 100:.1f}%) | успешно: {ok} | ошибок: {errors}",
        flush=True,
    )


def load_plan(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ReassignmentError(f"Файл плана не найден: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"entity_type", "entity_id", "new_owner_id"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ReassignmentError("В плане нет колонок: " + ", ".join(sorted(missing)))
        by_key: dict[tuple[str, int], dict[str, Any]] = {}
        for raw in reader:
            entity_type = str(raw.get("entity_type") or "").strip().casefold()
            entity_id = normalized_id(raw.get("entity_id"))
            new_owner_id = normalized_id(raw.get("new_owner_id"))
            if entity_type not in SUPPORTED or entity_id is None or new_owner_id is None:
                raise ReassignmentError(f"Некорректная строка плана: {raw}")
            key = (entity_type, entity_id)
            target_status = NEW_STATUS_ID if entity_type == "lead" else ""
            row = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "title": str(raw.get("title") or ""),
                "founder_key": str(raw.get("founder_key") or ""),
                "new_owner_id": new_owner_id,
                "new_status_id": target_status,
            }
            previous = by_key.get(key)
            if previous and (
                previous["new_owner_id"] != new_owner_id
                or previous["new_status_id"] != target_status
            ):
                raise ReassignmentError(
                    f"Конфликт целей для {entity_type} ID={entity_id}: {previous} / {row}"
                )
            by_key[key] = row
    rows = list(by_key.values())
    if not rows:
        raise ReassignmentError("План пуст")
    return rows


def load_current(client: BitrixClient) -> dict[str, dict[int, dict[str, Any]]]:
    companies = client.list_all(
        "crm.company.list",
        {"order": {"ID": "ASC"}, "filter": {}, "select": ["ID", "TITLE", "ASSIGNED_BY_ID"]},
    )
    contacts = client.list_all(
        "crm.contact.list",
        {
            "order": {"ID": "ASC"}, "filter": {},
            "select": ["ID", "LAST_NAME", "NAME", "SECOND_NAME", "ASSIGNED_BY_ID"],
        },
    )
    leads = client.list_all(
        "crm.lead.list",
        {"order": {"ID": "ASC"}, "filter": {}, "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID"]},
    )
    def index(rows):
        return {
            entity_id: row
            for row in rows
            if (entity_id := normalized_id(row.get("ID"))) is not None
        }
    return {"company": index(companies), "contact": index(contacts), "lead": index(leads)}


def state_rows(plan: list[dict[str, Any]], current: dict[str, dict[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in plan:
        entity_type = item["entity_type"]
        entity_id = item["entity_id"]
        record = current[entity_type].get(entity_id)
        owner = normalized_id((record or {}).get("ASSIGNED_BY_ID"))
        status = str((record or {}).get("STATUS_ID") or "") if entity_type == "lead" else ""
        owner_ok = owner == item["new_owner_id"]
        status_ok = entity_type != "lead" or status == NEW_STATUS_ID
        action = "already_matches" if record is not None and owner_ok and status_ok else "pending"
        error = "" if record is not None else "Сущность не найдена при контрольном чтении"
        rows.append({
            **item,
            "current_owner_id": owner or "",
            "current_status_id": status,
            "owner_matches": int(owner_ok),
            "status_matches": int(status_ok),
            "action": action,
            "error": error,
        })
    return rows


def prepare_statuses(client: BitrixClient, rows: list[dict[str, Any]]) -> dict[str, int]:
    targets = [
        row for row in rows
        if row["entity_type"] == "lead"
        and row["current_status_id"] != NEW_STATUS_ID
        and not row["error"]
    ]
    ok = errors = 0
    for i, row in enumerate(targets, start=1):
        try:
            client.update_lead(str(row["entity_id"]), {"STATUS_ID": NEW_STATUS_ID})
            ok += 1
        except Exception as exc:  # noqa: BLE001
            row["action"] = "update_error"
            row["error"] = f"STATUS_ID->NEW: {exc}"
            errors += 1
        progress("REPAIR NEW", i, len(targets), ok=ok, errors=errors)
    return {"planned": len(targets), "ok": ok, "errors": errors}


def update_owners(
    client: BitrixClient,
    plan: list[dict[str, Any]],
    current: dict[str, dict[int, dict[str, Any]]],
) -> dict[str, int]:
    methods = {
        "lead": client.update_lead,
        "company": client.update_company,
        "contact": client.update_contact,
    }
    targets = []
    for item in plan:
        record = current[item["entity_type"]].get(item["entity_id"])
        if record is None:
            continue
        if normalized_id(record.get("ASSIGNED_BY_ID")) != item["new_owner_id"]:
            targets.append(item)
    ok = errors = 0
    for i, item in enumerate(targets, start=1):
        try:
            methods[item["entity_type"]](
                str(item["entity_id"]), {"ASSIGNED_BY_ID": int(item["new_owner_id"])}
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            item["repair_owner_error"] = str(exc)
            errors += 1
        progress("REPAIR OWNER", i, len(targets), ok=ok, errors=errors)
    return {"planned": len(targets), "ok": ok, "errors": errors}


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "founder_key", "entity_type", "entity_id", "title",
        "current_owner_id", "new_owner_id", "owner_matches",
        "current_status_id", "new_status_id", "status_matches", "action", "error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def run(client: BitrixClient, plan_path: Path, output_dir: Path, apply: bool) -> dict[str, Any]:
    plan = load_plan(plan_path)
    current = load_current(client)
    before = state_rows(plan, current)
    before_ok = sum(row["action"] == "already_matches" for row in before)
    before_pending = len(before) - before_ok
    print(
        f"План восстановления: {len(plan)} сущностей. Уже правильно: {before_ok}/{len(plan)}; "
        f"требуют исправления: {before_pending}.",
        flush=True,
    )

    status_result = {"planned": 0, "ok": 0, "errors": 0}
    owner_result = {"planned": 0, "ok": 0, "errors": 0}
    if apply:
        status_result = prepare_statuses(client, before)
        stabilize = float(os.getenv("REASSIGN_STABILIZE_SECONDS", "12"))
        if status_result["planned"] and stabilize > 0:
            print(f"Ожидание {stabilize:g} сек. после восстановления NEW...", flush=True)
            time.sleep(stabilize)

        # Re-read after stage robots and only now enforce the owner from the saved plan.
        after_status = load_current(client)
        owner_result = update_owners(client, plan, after_status)
        final_wait = float(os.getenv("REASSIGN_FINAL_WAIT_SECONDS", "5"))
        if owner_result["planned"] and final_wait > 0:
            print(f"Ожидание {final_wait:g} сек. перед итоговой проверкой...", flush=True)
            time.sleep(final_wait)

    final_current = load_current(client)
    final_rows = state_rows(plan, final_current)
    verified = sum(row["action"] == "already_matches" for row in final_rows)
    mismatches = len(final_rows) - verified
    for row in final_rows:
        if row["action"] != "already_matches" and not row["error"]:
            parts = []
            if not row["owner_matches"]:
                parts.append(
                    f"owner={row['current_owner_id']} ожидается {row['new_owner_id']}"
                )
            if not row["status_matches"]:
                parts.append(
                    f"status={row['current_status_id']!r} ожидается {row['new_status_id']!r}"
                )
            row["action"] = "verify_error" if apply else "pending"
            row["error"] = "; ".join(parts)

    write_report(output_dir / "excluded_user_reassignment_repair.csv", final_rows)
    summary = {
        "mode_apply": int(apply),
        "plan_total": len(plan),
        "already_correct_before": before_ok,
        "needed_repair_before": before_pending,
        "status_updates_planned": status_result["planned"],
        "status_updates_ok": status_result["ok"],
        "status_update_errors": status_result["errors"],
        "owner_updates_planned": owner_result["planned"],
        "owner_updates_ok": owner_result["ok"],
        "owner_update_errors": owner_result["errors"],
        "verified_total": verified,
        "verify_errors": mismatches,
    }
    (output_dir / "excluded_user_reassignment_repair_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[ИТОГ REPAIR] подтверждено {verified}/{len(plan)}; осталось расхождений: {mismatches}",
        flush=True,
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Восстановить переназначение строго по сохранённому dry-run плану.")
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", default="output_repair")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(build_client(), Path(args.plan_file), output_dir, args.apply)
    except (ReassignmentError, BitrixError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["status_update_errors"] or summary["owner_update_errors"] or summary["verify_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
