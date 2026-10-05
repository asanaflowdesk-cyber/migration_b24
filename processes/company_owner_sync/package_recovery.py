from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from authority_state import load_authority_sources
from eqazyna_bitrix.bitrix_client import BitrixClient
from reconcile_company_lead_owners import (
    _clone_client,
    add_direct_contact_leads,
    contact_write_suppression,
    load_snapshot_fast,
    reconcile_package,
    resolve_authority_packages,
)
from sync_founder_packages import build_packages, normalized_id


def repair_package_residuals(client: Any, output_dir: Path) -> dict[str, Any]:
    """Repair whole founder packages from their live lead-linked contact owner.

    This runs in the same worker as event processing, after draining the queue.
    It discovers residuals even when the event was lost or acknowledged before
    a later CRM write. A conflict is reported instead of inventing an owner.
    """
    snapshot = load_snapshot_fast(client)
    raw, raw_skipped = build_packages(snapshot["companies"], snapshot["leads"], snapshot["contacts"], snapshot["requisites"])
    packages, skipped = resolve_authority_packages(raw, snapshot["contacts"], snapshot["leads"], load_authority_sources(output_dir))
    packages = add_direct_contact_leads(packages, snapshot["leads"])

    def has_residuals(package: dict[str, Any]) -> bool:
        target = package["owner_id"]
        return any(normalized_id(item.get("owner_id")) != target for item in package.get("contacts", [])) or any(
            normalized_id(company.get("owner_id")) != target or any(normalized_id(lead.get("owner_id")) != target for lead in company.get("leads", []))
            for company in package.get("companies", [])
        ) or any(normalized_id(lead.get("owner_id")) != target for lead in package.get("direct_contact_leads", []))

    pending = [package for package in packages if has_residuals(package)]
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    before_contact_writes = contact_write_suppression()

    def work(package: dict[str, Any]):
        local = _clone_client(client) if isinstance(client, BitrixClient) else client
        return reconcile_package(local, package, apply=True, before_contact_writes=before_contact_writes)

    with ThreadPoolExecutor(max_workers=min(8, len(pending) or 1), thread_name_prefix="package-recovery") as pool:
        jobs = {pool.submit(work, package): package for package in pending}
        for future in as_completed(jobs):
            package = jobs[future]
            try:
                package_rows, error = future.result()
                rows.extend(package_rows)
            except Exception as exc:
                error = type(exc).__name__
            if error:
                failures.append({"fio": package["fio"], "error": error})

    # Packages without any leads are outside the GPO repair scope, not errors.
    linked_fios = {package["fio"] for package in raw if any(company.get("leads") for company in package.get("companies", []))}
    linked_contact_ids = {normalized_id(lead.get("CONTACT_ID")) for lead in snapshot["leads"]}
    linked_fios.update(package["fio"] for package in raw if any(normalized_id(contact.get("id")) in linked_contact_ids for contact in package.get("contacts", [])))
    skipped = [item for item in skipped if item.get("fio") in linked_fios]
    summary = {
        "status": "PARTIAL" if failures or skipped or raw_skipped else "DONE",
        "packages": len(packages), "residual_packages": len(pending),
        "updated": sum(row.get("status") == "updated" for row in rows),
        "errors": len(failures), "skipped": len(skipped) + len(raw_skipped),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / "recovery.json.tmp"
    temporary.write_text(json.dumps({"summary": summary, "changes": rows, "failures": failures, "skipped": raw_skipped + skipped}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_dir / "recovery.json")
    print("[RECOVERY] " + json.dumps(summary, ensure_ascii=False), flush=True)
    return summary
