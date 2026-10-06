from __future__ import annotations

import argparse
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from collections.abc import Callable

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings
from fast_package_reconciler import batch_execute, bulk_owners
from authority_state import load_authority_sources
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
    """Compatibility predicate: authority is established by CRM linkage, not card text."""
    return normalized_id(contact.get("ID")) is not None


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
    leads: list[dict[str, Any]] | None = None,
    authority_sources: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve from CONTACT_ID of package leads, never from DATE_MODIFY.

    The source of an accepted owner-change event stays authoritative for its
    package. Without that event history, competing lead-linked contacts must
    agree. Unlinked duplicate contacts are targets, not competing authorities.
    ``leads=None`` is retained for callers holding a preselected package.
    """
    contacts_by_id = {
        contact_id: contact
        for contact in contacts
        if (contact_id := normalized_id(contact.get("ID"))) is not None
    }
    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    linked_ids = {
        value for lead in leads or []
        if (value := normalized_id(lead.get("CONTACT_ID"))) is not None
    }

    for package in packages:
        authority: list[dict[str, Any]] = []
        for item in package.get("contacts", []):
            contact_id = normalized_id(item.get("id"))
            contact = contacts_by_id.get(contact_id or 0)
            if not contact:
                continue
            # CONTACT_ID is the primary authority signal. Card fields such as
            # POST/COMMENTS are legacy hints only and are not required.
            if leads is None:
                if is_director_authority_contact(contact):
                    authority.append(contact)
            elif contact_id in linked_ids:
                authority.append(contact)
        remembered = (authority_sources or {}).get(str(package.get("fio") or ""))
        if remembered:
            selected = [contact for contact in authority if normalized_id(contact.get("ID")) == remembered]
            if not selected:
                skipped.append({"type": "remembered_source_not_lead_linked", "fio": package.get("fio", ""), "company_title": f"contact_id={remembered}"})
                continue
            authority = selected

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
        item["sync_package_contacts"] = leads is not None
        resolved.append(item)

    return resolved, skipped


def company_lead_items(package: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    if package.get("sync_package_contacts"):
        source_ids = set(package.get("authority_contact_ids") or [])
        for contact in package.get("contacts", []):
            contact_id = normalized_id(contact.get("id"))
            if contact_id and contact_id not in source_ids:
                items.append({"entity": "contact", "id": contact_id, "title": str(contact.get("title") or "")})

    def add_lead(lead: dict[str, Any]) -> None:
        lead_id = normalized_id(lead.get("id"))
        key = ("lead", lead_id or 0)
        if not lead_id or key in seen:
            return
        seen.add(key)
        items.append({
            "entity": "lead",
            "id": lead_id,
            "title": str(lead.get("title") or ""),
        })

    for company in package.get("companies", []):
        company_id = normalized_id(company.get("id"))
        if company_id:
            items.append({
                "entity": "company",
                "id": company_id,
                "title": str(company.get("title") or ""),
            })
        for lead in company.get("leads", []):
            add_lead(lead)
    # A lead can be related only to the director contact, without COMPANY_ID.
    # It is still part of the manual reconciliation scope of workflow 31.
    for lead in package.get("direct_contact_leads", []):
        add_lead(lead)
    return items


def add_direct_contact_leads(
    packages: list[dict[str, Any]],
    leads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach leads linked directly to authority contacts, even without a company.

    ``build_packages`` groups companies and their leads. Workflow 31 also has to
    reconcile leads whose only reliable relation is ``CONTACT_ID`` of the
    director contact. Keep this enrichment local to workflow 31: 31A owns a
    separate full-package contract.
    """
    result: list[dict[str, Any]] = []
    for package in packages:
        # Authority decides the target manager, but every contact card in that
        # resolved person package can own a direct lead. Limiting this to the
        # authority card left leads attached to another card of the same
        # director/founder out of workflow 31.
        contact_ids = {
            int(contact_id)
            for contact in package.get("contacts", [])
            if (contact_id := normalized_id(contact.get("id"))) is not None
        }
        company_lead_ids = {
            normalized_id(lead.get("id"))
            for company in package.get("companies", [])
            for lead in company.get("leads", [])
            if normalized_id(lead.get("id"))
        }
        direct_leads = [
            {
                "id": lead_id,
                "title": str(lead.get("TITLE") or "").strip(),
                "owner_id": normalized_id(lead.get("ASSIGNED_BY_ID")),
            }
            for lead in leads
            if (lead_id := normalized_id(lead.get("ID")))
            and normalized_id(lead.get("CONTACT_ID")) in contact_ids
            and lead_id not in company_lead_ids
        ]
        item = copy.deepcopy(package)
        item["direct_contact_leads"] = direct_leads
        result.append(item)
    return result


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


def contact_write_suppression() -> Callable[[list[dict[str, int]]], None] | None:
    queue_url = os.getenv("GOOGLE_QUEUE_URL", "").strip()
    queue_key = os.getenv("GOOGLE_QUEUE_KEY", "").strip()
    if not queue_url or not queue_key:
        return None

    def remember(owners: list[dict[str, int]]) -> None:
        from queue_founder_packages import queue_call

        # Register before the write so CRM-generated duplicate-contact events
        # cannot become a new user-selected authority for the package.
        queue_call(queue_url, queue_key, "remember_owners", owners=owners)

    return remember


def reconcile_package(
    client: Any,
    package: dict[str, Any],
    apply: bool,
    before_contact_writes: Callable[[list[dict[str, int]]], None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
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
            contacts = [{"contact_id": int(row["id"]), "owner_id": target} for row in pending if row["entity"] == "contact"]
            if contacts and before_contact_writes:
                before_contact_writes(contacts)
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
    state_dir = Path(os.getenv("OWNER_SYNC_OUTPUT_DIR", str(Path.home() / ".company-owner-sync" / "output")))
    packages, authority_skipped = resolve_authority_packages(raw_packages, snapshot["contacts"], snapshot["leads"], load_authority_sources(state_dir))
    packages = add_direct_contact_leads(packages, snapshot["leads"])
    skipped = raw_skipped + authority_skipped

    print(
        f"[RECONCILE] packages: resolved={len(packages)}; authority_skipped={len(authority_skipped)}; mode={'apply' if apply else 'dry_run'}",
        flush=True,
    )

    rows: list[dict[str, Any]] = []
    package_errors: list[dict[str, Any]] = []
    workers = max(1, min(int(os.getenv("OWNER_SYNC_WORKERS", str(MAX_WORKERS)) or MAX_WORKERS), 16, len(packages) or 1))
    before_contact_writes = contact_write_suppression()

    def work(package: dict[str, Any]):
        local = _clone_client(client) if isinstance(client, BitrixClient) else client
        return package, *reconcile_package(local, package, apply, before_contact_writes)

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
        "leads": sum(
            len(company.get("leads", []))
            for package in packages
            for company in package.get("companies", [])
        ) + sum(len(package.get("direct_contact_leads", [])) for package in packages),
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
