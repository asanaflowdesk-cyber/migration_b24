from __future__ import annotations

import argparse
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings
from fast_package_reconciler import batch_execute, bulk_owners
from sync_founder_packages import (
    build_packages,
    load,
    normalized_id,
    write_report,
    write_summary,
)


MAX_WORKERS = 8
MAX_STABILIZE_ROUNDS = 3


def is_director_authority_contact(contact: dict[str, Any]) -> bool:
    """Only an actual director/manager contact may define the package owner.

    A generic founder contact is deliberately not enough for the bulk repair:
    the invariant requested for workflow 31 is company/lead owner == current
    owner of the director contact.
    """
    post = str(contact.get("POST") or "").casefold()
    comments = str(contact.get("COMMENTS") or "")
    return (
        "руковод" in post
        or "директор" in post
        or "eqazyna_director:" in comments.casefold()
    )


def _clone_client(client: BitrixClient) -> BitrixClient:
    return BitrixClient(
        client.webhook_url,
        timeout=client.timeout,
        retries=client.retries,
        polite_delay_seconds=0.0,
        verify_ssl=client.verify_ssl,
    )


def load_snapshot_fast(client: BitrixClient) -> dict[str, list[dict[str, Any]]]:
    specs = {
        "companies": ("crm.company.list", ["ID", "TITLE", "ASSIGNED_BY_ID"], {}),
        "leads": ("crm.lead.list", ["ID", "TITLE", "COMPANY_ID", "CONTACT_ID", "ASSIGNED_BY_ID"], {}),
        "contacts": (
            "crm.contact.list",
            ["ID", "LAST_NAME", "NAME", "SECOND_NAME", "POST", "COMPANY_ID", "ASSIGNED_BY_ID", "COMMENTS", "DATE_MODIFY"],
            {},
        ),
        "requisites": ("crm.requisite.list", ["ID", "ENTITY_ID", "ENTITY_TYPE_ID", "RQ_DIRECTOR"], {"ENTITY_TYPE_ID": 4}),
    }

    if not isinstance(client, BitrixClient):
        return {
            name: load(client, method, select, filter_)
            for name, (method, select, filter_) in specs.items()
        }

    def read(spec: tuple[str, list[str], dict[str, Any]]) -> list[dict[str, Any]]:
        method, select, filter_ = spec
        local = _clone_client(client)
        return load(local, method, select, filter_)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="owner-full-snapshot") as pool:
        futures = {name: pool.submit(read, spec) for name, spec in specs.items()}
        return {name: futures[name].result() for name in specs}


def resolve_authority_packages(
    packages: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve one deterministic owner for every package without DATE_MODIFY.

    Rules:
    - authority is only a director/manager contact;
    - every authority contact in one FIO package must have an owner;
    - if there are several authority contacts, all must point to the same owner;
    - otherwise the package is not touched. We never guess by newest card, role,
      ROP list or user id.
    """
    contacts_by_id = {
        contact_id: contact
        for contact in contacts
        if (contact_id := normalized_id(contact.get("ID"))) is not None
    }
    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for package in packages:
        authority: list[dict[str, Any]] = []
        for item in package.get("contacts", []):
            contact_id = normalized_id(item.get("id"))
            contact = contacts_by_id.get(contact_id or 0)
            if contact and is_director_authority_contact(contact):
                authority.append(contact)

        if not authority:
            skipped.append({
                "type": "director_contact_not_found",
                "fio": package.get("fio", ""),
            })
            continue

        missing_owner = [
            normalized_id(contact.get("ID"))
            for contact in authority
            if not normalized_id(contact.get("ASSIGNED_BY_ID"))
        ]
        if missing_owner:
            skipped.append({
                "type": "director_contact_without_owner",
                "fio": package.get("fio", ""),
                "company_title": "contact_ids=" + ",".join(str(value) for value in missing_owner if value),
            })
            continue

        owner_ids = sorted({
            normalized_id(contact.get("ASSIGNED_BY_ID"))
            for contact in authority
            if normalized_id(contact.get("ASSIGNED_BY_ID"))
        })
        if len(owner_ids) != 1:
            skipped.append({
                "type": "director_contacts_have_different_owners",
                "fio": package.get("fio", ""),
                "company_title": "owner_ids=" + ",".join(str(value) for value in owner_ids),
            })
            continue

        authority_ids = sorted(
            normalized_id(contact.get("ID"))
            for contact in authority
            if normalized_id(contact.get("ID"))
        )
        if not authority_ids:
            skipped.append({"type": "director_contact_not_found", "fio": package.get("fio", "")})
            continue

        item = copy.deepcopy(package)
        item["owner_id"] = owner_ids[0]
        item["source_contact_id"] = authority_ids[0]
        item["authority_contact_ids"] = authority_ids
        resolved.append(item)

    return resolved, skipped


def company_lead_items(package: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for company in package.get("companies", []):
        company_id = normalized_id(company.get("id"))
        if company_id:
            items.append({
                "entity": "company",
                "id": company_id,
                "title": str(company.get("title") or ""),
            })
        for lead in company.get("leads", []):
            lead_id = normalized_id(lead.get("id"))
            if lead_id:
                items.append({
                    "entity": "lead",
                    "id": lead_id,
                    "title": str(lead.get("title") or ""),
                })
    return items


def live_authority_owner(
    client: Any,
    contact_ids: list[int],
) -> tuple[int | None, int | None, str]:
    operations = [
        (f"c{index}", "crm.contact.get", {"id": contact_id})
        for index, contact_id in enumerate(contact_ids)
    ]
    values, errors = batch_execute(client, operations)
    if errors:
        return None, None, "director_contact_read_failed"

    contacts: list[dict[str, Any]] = []
    for index, contact_id in enumerate(contact_ids):
        value = values.get(f"c{index}")
        if not isinstance(value, dict):
            return None, None, "director_contact_missing"
        if normalized_id(value.get("ID")) not in {None, contact_id}:
            return None, None, "director_contact_identity_changed"
        if not is_director_authority_contact(value):
            return None, None, "director_contact_no_longer_authority"
        if not normalized_id(value.get("ASSIGNED_BY_ID")):
            return None, None, "director_contact_without_owner"
        contacts.append(value)

    owners = sorted({normalized_id(item.get("ASSIGNED_BY_ID")) for item in contacts})
    owners = [owner for owner in owners if owner]
    if len(owners) != 1:
        return None, None, "director_contacts_have_different_owners"
    return owners[0], min(contact_ids), ""


def _rows_from_live(
    package: dict[str, Any],
    items: list[dict[str, Any]],
    owners: dict[tuple[str, int], int | None],
    owner_errors: dict[tuple[str, int], str],
    target: int,
    source_contact_id: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        key = (str(item["entity"]), int(item["id"]))
        current = owners.get(key)
        error = owner_errors.get(key, "")
        rows.append({
            "fio": package.get("fio", ""),
            "entity": item["entity"],
            "id": item["id"],
            "title": item.get("title", ""),
            "current": current,
            "target": target,
            "source_contact_id": source_contact_id,
            "status": "error" if error else ("already_correct" if current == target else "planned"),
            "error": error,
        })
    return rows


def reconcile_package(client: Any, package: dict[str, Any], apply: bool) -> tuple[list[dict[str, Any]], str]:
    items = company_lead_items(package)
    authority_ids = [int(value) for value in package.get("authority_contact_ids", [])]
    if not items:
        return [], ""

    target, source_contact_id, source_error = live_authority_owner(client, authority_ids)
    if source_error or not target or not source_contact_id:
        return [], source_error or "director_owner_unresolved"

    owners, owner_errors = bulk_owners(client, items)
    rows = _rows_from_live(package, items, owners, owner_errors, target, source_contact_id)
    if owner_errors or not apply:
        return rows, "target_read_failed" if owner_errors else ""

    for _round in range(MAX_STABILIZE_ROUNDS):
        target, source_contact_id, source_error = live_authority_owner(client, authority_ids)
        if source_error or not target or not source_contact_id:
            return rows, source_error or "director_owner_unresolved"

        owners, owner_errors = bulk_owners(client, items)
        rows = _rows_from_live(package, items, owners, owner_errors, target, source_contact_id)
        if owner_errors:
            return rows, "target_read_failed"

        pending = [row for row in rows if row["status"] == "planned"]
        if pending:
            operations = [
                (
                    f"u{index}",
                    f"crm.{row['entity']}.update",
                    {"id": row["id"], "fields": {"ASSIGNED_BY_ID": target}},
                )
                for index, row in enumerate(pending)
            ]
            _values, update_errors = batch_execute(client, operations)
            for index, row in enumerate(pending):
                if f"u{index}" in update_errors:
                    row["status"] = "error"
                    row["error"] = update_errors[f"u{index}"]

        target_after, source_after, source_error = live_authority_owner(client, authority_ids)
        if source_error or not target_after or not source_after:
            return rows, source_error or "director_owner_unresolved_after_write"
        if target_after != target:
            # The manager changed the director while this package was running.
            # Do not preserve the stale target: immediately rebuild from fresh state.
            continue

        final_owners, final_errors = bulk_owners(client, items)
        final_rows = _rows_from_live(package, items, final_owners, final_errors, target_after, source_after)
        for row in final_rows:
            if row["error"]:
                row["status"] = "error"
            elif row["current"] == target_after:
                before = next((item for item in rows if item["entity"] == row["entity"] and item["id"] == row["id"]), None)
                row["status"] = "already_correct" if before and before.get("status") == "already_correct" else "updated"
            else:
                row["status"] = "error"
                row["error"] = "write_verification_failed"
        if not any(row["status"] == "error" for row in final_rows):
            return final_rows, ""
        return final_rows, "write_verification_failed"

    return rows, "director_owner_changed_repeatedly"


def run(client: BitrixClient, output_dir: Path, apply: bool) -> dict[str, Any]:
    started = time.monotonic()
    print("[RECONCILE] snapshot: start", flush=True)
    snapshot = load_snapshot_fast(client)
    print(
        "[RECONCILE] snapshot: ready; "
        f"companies={len(snapshot['companies'])}; leads={len(snapshot['leads'])}; "
        f"contacts={len(snapshot['contacts'])}; elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )

    raw_packages, raw_skipped = build_packages(
        snapshot["companies"],
        snapshot["leads"],
        snapshot["contacts"],
        snapshot["requisites"],
    )
    packages, authority_skipped = resolve_authority_packages(raw_packages, snapshot["contacts"])
    skipped = raw_skipped + authority_skipped

    print(
        f"[RECONCILE] packages: resolved={len(packages)}; authority_skipped={len(authority_skipped)}; mode={'apply' if apply else 'dry_run'}",
        flush=True,
    )

    rows: list[dict[str, Any]] = []
    package_errors: list[dict[str, Any]] = []
    workers = max(1, min(int(os.getenv("OWNER_SYNC_WORKERS", str(MAX_WORKERS)) or MAX_WORKERS), 16, len(packages) or 1))

    def work(package: dict[str, Any]):
        local = _clone_client(client) if isinstance(client, BitrixClient) else client
        return package, *reconcile_package(local, package, apply)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="owner-full-reconcile") as pool:
        futures = [pool.submit(work, package) for package in packages]
        done = 0
        for future in as_completed(futures):
            package, package_rows, error = future.result()
            rows.extend(package_rows)
            if error:
                package_errors.append({
                    "type": "reconcile_error",
                    "fio": package.get("fio", ""),
                    "company_title": error,
                })
            done += 1
            if done == 1 or done % 10 == 0 or done == len(packages):
                print(
                    f"[RECONCILE] progress={done}/{len(packages)}; errors={len(package_errors)}; elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )

    skipped.extend(package_errors)
    rows.sort(key=lambda row: (str(row.get("fio") or ""), str(row.get("entity") or ""), int(row.get("id") or 0)))
    output_dir.mkdir(parents=True, exist_ok=True)
    write_report(output_dir / "company_lead_owner_reconcile.xlsx", packages, rows, skipped)

    summary = {
        "mode": "apply" if apply else "dry_run",
        "packages": len(packages),
        "companies": sum(len(package.get("companies", [])) for package in packages),
        "leads": sum(len(company.get("leads", [])) for package in packages for company in package.get("companies", [])),
        "checked": len(rows),
        "planned": sum(row.get("status") == "planned" for row in rows),
        "updated": sum(row.get("status") == "updated" for row in rows),
        "already_correct": sum(row.get("status") == "already_correct" for row in rows),
        "errors": sum(row.get("status") == "error" for row in rows) + len(package_errors),
        "authority_skipped": len(authority_skipped),
        "raw_skipped": len(raw_skipped),
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
    write_summary(output_dir, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror company and lead ASSIGNED_BY_ID from the current director contact owner"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    settings = Settings.from_env()
    client = BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )
    summary = run(client, Path(args.output_dir), args.apply)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
