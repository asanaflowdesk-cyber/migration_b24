from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import enrich_missing_directors as base
from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings


EXTRA_ERROR_STATUSES = {
    "director_owner_conflict",
    "company_contact_conflict",
    "contact_create_failed",
    "contact_link_verification_failed",
    "lead_link_verification_failed",
    "company_missing",
    "bin_changed",
    "fresh_director_conflict",
    "company_without_owner",
    "invalid_director_fio",
    "error",
}


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)] if isinstance(value, list) else []


def _binding_ids(items: list[dict[str, Any]], key: str) -> set[int]:
    result: set[int] = set()
    for item in items:
        value = base.normalize_id(item.get(key))
        if value is not None:
            result.add(value)
    return result


def _global_contacts_for_director(client: BitrixClient, director: str) -> list[dict[str, Any]]:
    parts = base.fio_parts(director)
    if parts is None:
        return []
    last_name, _first_name, _second_name = parts
    rows = client.list_all(
        "crm.contact.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"LAST_NAME": last_name},
            "select": [
                "ID",
                "COMPANY_ID",
                "LAST_NAME",
                "NAME",
                "SECOND_NAME",
                "POST",
                "COMMENTS",
                "ASSIGNED_BY_ID",
            ],
        },
    )
    wanted = base.fio_key(director)
    return [row for row in rows if base.valid_fio(base.contact_fio(row)) and base.fio_key(base.contact_fio(row)) == wanted]


def _choose_canonical_contact(contacts: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[int]]:
    if not contacts:
        return None, []
    ordered = sorted(
        contacts,
        key=lambda item: (
            0 if base.is_director_contact(item) else 1,
            base.normalize_id(item.get("ID")) or 10**18,
        ),
    )
    canonical = ordered[0]
    duplicate_ids = [
        value
        for item in ordered[1:]
        if (value := base.normalize_id(item.get("ID"))) is not None
    ]
    return canonical, duplicate_ids


def _contact_company_bindings(client: BitrixClient, contact_id: int) -> list[dict[str, Any]]:
    return _as_list(client.call("crm.contact.company.items.get", {"id": contact_id}))


def _company_contact_bindings(client: BitrixClient, company_id: int) -> list[dict[str, Any]]:
    return _as_list(client.call("crm.company.contact.items.get", {"id": company_id}))


def _lead_contact_bindings(client: BitrixClient, lead_id: int) -> list[dict[str, Any]]:
    return _as_list(client.call("crm.lead.contact.items.get", {"id": lead_id}))


def _linked_contacts_for_company(client: BitrixClient, company_id: int) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for contact_id in sorted(_binding_ids(_company_contact_bindings(client, company_id), "CONTACT_ID")):
        item = client.call("crm.contact.get", {"id": contact_id})
        if isinstance(item, dict):
            contacts.append(item)
    return contacts


def _ensure_contact_company_link(client: BitrixClient, contact_id: int, company_id: int) -> bool:
    bindings = _contact_company_bindings(client, contact_id)
    company_ids = _binding_ids(bindings, "COMPANY_ID")
    if company_id in company_ids:
        return False
    client.call(
        "crm.contact.company.add",
        {
            "id": contact_id,
            "fields": {
                "COMPANY_ID": company_id,
                "IS_PRIMARY": "Y" if not bindings else "N",
            },
        },
    )
    verify_ids = _binding_ids(_contact_company_bindings(client, contact_id), "COMPANY_ID")
    if company_id not in verify_ids:
        raise RuntimeError(f"contact_company_link_verification_failed:{contact_id}:{company_id}")
    return True


def _company_leads(client: BitrixClient, company_id: int) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"COMPANY_ID": company_id},
            "select": ["ID", "COMPANY_ID", "CONTACT_ID", "CONTACT_IDS"],
        },
    )


def _ensure_lead_contact_links(client: BitrixClient, company_id: int, contact_id: int) -> tuple[int, int, list[int]]:
    added = 0
    already = 0
    verified_leads: list[int] = []
    for lead in _company_leads(client, company_id):
        lead_id = base.normalize_id(lead.get("ID"))
        if lead_id is None:
            continue
        bindings = _lead_contact_bindings(client, lead_id)
        linked_ids = _binding_ids(bindings, "CONTACT_ID")
        if contact_id in linked_ids:
            already += 1
            verified_leads.append(lead_id)
            continue
        client.call(
            "crm.lead.contact.add",
            {
                "id": lead_id,
                "fields": {
                    "CONTACT_ID": contact_id,
                    "IS_PRIMARY": "Y" if not bindings else "N",
                },
            },
        )
        verify_ids = _binding_ids(_lead_contact_bindings(client, lead_id), "CONTACT_ID")
        if contact_id not in verify_ids:
            raise RuntimeError(f"lead_contact_link_verification_failed:{lead_id}:{contact_id}")
        added += 1
        verified_leads.append(lead_id)
    return added, already, verified_leads


def _filter_secondary_directors(
    client: BitrixClient,
    candidates: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    snapshot: dict[str, list[dict[str, Any]]],
    workers: int,
) -> list[dict[str, Any]]:
    """Do not re-enrich a company when its director is linked as a non-primary contact."""
    contacts_by_id = {
        contact_id: row
        for row in snapshot.get("contacts", [])
        if (contact_id := base.normalize_id(row.get("ID"))) is not None
    }

    def inspect(row: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
        local = base.clone_client(client)
        company_id = int(row["company_id"])
        linked = _company_contact_bindings(local, company_id)
        for contact_id in _binding_ids(linked, "CONTACT_ID"):
            contact = contacts_by_id.get(contact_id)
            if contact is None:
                fetched = local.call("crm.contact.get", {"id": contact_id})
                contact = fetched if isinstance(fetched, dict) else None
            if contact and base.is_director_contact(contact) and base.valid_fio(base.contact_fio(contact)):
                return row, True, base.contact_fio(contact)
        return row, False, ""

    keep: list[dict[str, Any]] = []
    max_workers = max(1, min(workers, 8))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="director-links-scan") as pool:
        future_map = {pool.submit(inspect, row): row for row in candidates}
        for future in as_completed(future_map):
            row, found, director = future.result()
            if found:
                skipped.append({
                    "company_id": row["company_id"],
                    "title": row.get("title", ""),
                    "status": "director_already_present_secondary_link",
                    "existing_director": director,
                })
            else:
                keep.append(row)
    return sorted(keep, key=lambda item: int(item["company_id"]))


def _annotate_director_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.get("status") == "accepted" and base.valid_fio(row.get("director")):
            counts[base.fio_key(row["director"])] += 1
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        key = base.fio_key(item.get("director")) if base.valid_fio(item.get("director")) else ""
        item["director_group_size"] = counts.get(key, 0)
        result.append(item)
    return result


def _fresh_row_state(client: BitrixClient, row: dict[str, Any], director: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], str]:
    company_id = int(row["company_id"])
    company, requisites, primary_contacts = base.fresh_company_state(client, company_id)
    if not company:
        return None, requisites, primary_contacts, "company_missing"
    if base.current_bin(company, requisites) != row["bin"]:
        return company, requisites, primary_contacts, "bin_changed"

    linked_contacts = _linked_contacts_for_company(client, company_id)
    merged: dict[int, dict[str, Any]] = {}
    for contact in primary_contacts + linked_contacts:
        contact_id = base.normalize_id(contact.get("ID"))
        if contact_id is not None:
            merged[contact_id] = contact

    existing_directors = [
        contact
        for contact in merged.values()
        if base.is_director_contact(contact) and base.valid_fio(base.contact_fio(contact))
    ]
    conflicts = [contact for contact in existing_directors if base.fio_key(base.contact_fio(contact)) != base.fio_key(director)]
    if conflicts:
        return company, requisites, list(merged.values()), "fresh_director_conflict"

    req_directors = [base.normalize_fio(item.get("RQ_DIRECTOR")) for item in requisites if base.valid_fio(item.get("RQ_DIRECTOR"))]
    if any(base.fio_key(value) != base.fio_key(director) for value in req_directors):
        return company, requisites, list(merged.values()), "fresh_director_conflict"
    return company, requisites, list(merged.values()), "ok"


def _prepare_canonical_contact(
    client: BitrixClient,
    director: str,
    rows: list[dict[str, Any]],
) -> tuple[int | None, int | None, list[int], str]:
    matches = _global_contacts_for_director(client, director)
    canonical, duplicate_ids = _choose_canonical_contact(matches)
    if canonical is not None:
        contact_id = base.normalize_id(canonical.get("ID"))
        owner_id = base.normalize_id(canonical.get("ASSIGNED_BY_ID"))
        if contact_id is None:
            return None, None, duplicate_ids, "contact_create_failed"
        if owner_id is None:
            group_owners = {
                base.normalize_id(row.get("owner_id"))
                for row in rows
                if base.normalize_id(row.get("owner_id")) is not None
            }
            if len(group_owners) != 1:
                return None, None, duplicate_ids, "director_owner_conflict"
            owner_id = next(iter(group_owners))
        source_row = rows[0]
        fields: dict[str, Any] = {
            "COMMENTS": base.provenance_comments(canonical.get("COMMENTS", ""), source_row.get("source", ""), source_row.get("url", "")),
        }
        if not str(canonical.get("POST") or "").strip():
            fields["POST"] = "Руководитель"
        if base.normalize_id(canonical.get("ASSIGNED_BY_ID")) is None:
            fields["ASSIGNED_BY_ID"] = owner_id
        client.call("crm.contact.update", {"id": contact_id, "fields": fields})
        return contact_id, owner_id, duplicate_ids, ""

    owners: set[int] = set()
    fresh_companies: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        company = client.call("crm.company.get", {"id": int(row["company_id"])})
        if not isinstance(company, dict):
            return None, None, [], "company_missing"
        owner_id = base.normalize_id(company.get("ASSIGNED_BY_ID"))
        if owner_id is None:
            return None, None, [], "company_without_owner"
        owners.add(owner_id)
        fresh_companies.append((row, company))
    if len(owners) != 1:
        return None, None, [], "director_owner_conflict"
    owner_id = next(iter(owners))
    parts = base.fio_parts(director)
    if parts is None:
        return None, None, [], "invalid_director_fio"
    last_name, first_name, second_name = parts
    primary_company_id = min(int(row["company_id"]) for row, _company in fresh_companies)
    source_row = sorted(rows, key=lambda item: int(item["company_id"]))[0]
    added = client.call(
        "crm.contact.add",
        {
            "fields": {
                "LAST_NAME": last_name,
                "NAME": first_name,
                "SECOND_NAME": second_name,
                "POST": "Руководитель",
                "COMPANY_ID": primary_company_id,
                "ASSIGNED_BY_ID": owner_id,
                "COMMENTS": base.provenance_comments("", source_row.get("source", ""), source_row.get("url", "")),
            }
        },
    )
    contact_id = base.normalize_id(added)
    if contact_id is None:
        return None, None, [], "contact_create_failed"
    return contact_id, owner_id, [], ""


def _apply_director_group(client: BitrixClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted((dict(row) for row in rows), key=lambda item: int(item["company_id"]))
    director = base.normalize_fio(ordered[0]["director"])
    contact_id, contact_owner_id, duplicate_ids, group_error = _prepare_canonical_contact(client, director, ordered)
    if group_error or contact_id is None:
        for row in ordered:
            row["status"] = group_error or "contact_create_failed"
            row["duplicate_contact_ids"] = ",".join(str(value) for value in duplicate_ids)
        return ordered

    results: list[dict[str, Any]] = []
    for row in ordered:
        result = dict(row)
        company_id = int(row["company_id"])
        try:
            company, requisites, _contacts, state = _fresh_row_state(client, row, director)
            if state != "ok" or not company:
                result["status"] = state
                results.append(result)
                continue

            company_owner = base.normalize_id(company.get("ASSIGNED_BY_ID"))
            if company_owner is None:
                result["status"] = "company_without_owner"
                results.append(result)
                continue

            linked_company = _ensure_contact_company_link(client, contact_id, company_id)

            updated_requisites: list[int] = []
            for requisite in requisites:
                req_id = base.normalize_id(requisite.get("ID"))
                if not req_id:
                    continue
                existing = requisite.get("RQ_DIRECTOR")
                if base.valid_fio(existing):
                    if base.fio_key(existing) != base.fio_key(director):
                        raise RuntimeError(f"requisite_director_conflict:{req_id}")
                    continue
                req_bin = base.normalize_bin(requisite.get("RQ_INN"))
                if req_bin and req_bin != row["bin"]:
                    continue
                client.call("crm.requisite.update", {"id": req_id, "fields": {"RQ_DIRECTOR": director}})
                updated_requisites.append(req_id)

            leads_added, leads_existing, verified_leads = _ensure_lead_contact_links(client, company_id, contact_id)

            verify_company_ids = _binding_ids(_contact_company_bindings(client, contact_id), "COMPANY_ID")
            if company_id not in verify_company_ids:
                result["status"] = "contact_link_verification_failed"
                results.append(result)
                continue

            if updated_requisites:
                _company, verify_requisites, _primary_contacts = base.fresh_company_state(client, company_id)
                if not all(
                    any(
                        base.normalize_id(req.get("ID")) == req_id
                        and base.fio_key(req.get("RQ_DIRECTOR")) == base.fio_key(director)
                        for req in verify_requisites
                    )
                    for req_id in updated_requisites
                ):
                    result["status"] = "requisite_verification_failed"
                    results.append(result)
                    continue

            result["status"] = "updated" if updated_requisites else "updated_contact_only_no_requisite"
            result["contact_id"] = contact_id
            result["contact_owner_id"] = contact_owner_id or ""
            result["contact_company_link_added"] = int(linked_company)
            result["requisites_updated"] = ",".join(str(value) for value in updated_requisites)
            result["leads_linked"] = leads_added
            result["lead_links_existing"] = leads_existing
            result["lead_ids_verified"] = ",".join(str(value) for value in verified_leads)
            result["duplicate_contact_ids"] = ",".join(str(value) for value in duplicate_ids)
            results.append(result)
        except RuntimeError as exc:
            failed = dict(result)
            message = str(exc)
            if message.startswith("contact_company_link_verification_failed"):
                failed["status"] = "contact_link_verification_failed"
            elif message.startswith("lead_contact_link_verification_failed"):
                failed["status"] = "lead_link_verification_failed"
            elif message.startswith("requisite_director_conflict"):
                failed["status"] = "fresh_director_conflict"
            else:
                failed["status"] = "error"
            failed["error"] = message
            failed["contact_id"] = contact_id
            results.append(failed)
        except Exception as exc:  # noqa: BLE001
            failed = dict(result)
            failed["status"] = "error"
            failed["error"] = type(exc).__name__
            failed["contact_id"] = contact_id
            results.append(failed)
    return results


def apply_rows_v2(client: BitrixClient, rows: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    accepted = [dict(row) for row in rows if row.get("status") == "accepted"]
    untouched = [dict(row) for row in rows if row.get("status") != "accepted"]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted:
        groups[base.fio_key(row["director"])].append(row)

    results: list[dict[str, Any]] = []
    max_workers = max(1, min(workers, 8, len(groups) or 1))

    def work(group_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        local = base.clone_client(client)
        return _apply_director_group(local, group_rows)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="director-group-apply") as pool:
        future_map = {pool.submit(work, group_rows): key for key, group_rows in groups.items()}
        completed = 0
        for future in as_completed(future_map):
            completed += 1
            key = future_map[future]
            try:
                group_results = future.result()
            except Exception as exc:  # noqa: BLE001
                group_results = []
                for row in groups[key]:
                    failed = dict(row)
                    failed["status"] = "error"
                    failed["error"] = type(exc).__name__
                    group_results.append(failed)
            results.extend(group_results)
            print(
                f"[DIRECTOR] group {completed}/{len(groups)} fio={key} companies={len(groups[key])}",
                flush=True,
            )
    return sorted(untouched + results, key=lambda item: int(item["company_id"]))


def _write_report_v2(output_dir: Path, rows: list[dict[str, Any]], skipped: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    summary = base.write_report(output_dir, rows, skipped, apply)
    if apply:
        summary["errors"] = sum(
            row.get("status") in EXTRA_ERROR_STATUSES
            or str(row.get("status") or "").endswith("_failed")
            or str(row.get("status") or "").endswith("_conflict")
            for row in rows
        )
    summary["director_groups"] = len({base.fio_key(row["director"]) for row in rows if base.valid_fio(row.get("director"))})
    summary["contacts_used"] = len({base.normalize_id(row.get("contact_id")) for row in rows if base.normalize_id(row.get("contact_id")) is not None})
    summary["lead_links_added"] = sum(int(row.get("leads_linked") or 0) for row in rows)
    summary["lead_links_existing"] = sum(int(row.get("lead_links_existing") or 0) for row in rows)
    (output_dir / "company_director_enrichment_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enrich missing company directors with one canonical contact per person and link it to all companies/leads"
    )
    parser.add_argument("--apply", action="store_true", help="Write director/requisite/company/lead links; default is dry-run")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-companies", type=int, default=0, help="0 = all candidates")
    parser.add_argument("--workers", type=int, default=int(os.getenv("DIRECTOR_ENRICH_WORKERS", "6") or 6))
    parser.add_argument("--http-timeout", type=int, default=int(os.getenv("DIRECTOR_HTTP_TIMEOUT", "12") or 12))
    args = parser.parse_args()
    if args.max_companies < 0 or args.workers <= 0 or args.http_timeout <= 0:
        parser.error("max-companies must be >= 0; workers/http-timeout must be > 0")

    settings = Settings.from_env()
    client = BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )
    print("[DIRECTOR] loading Bitrix companies/requisites/contacts", flush=True)
    snapshot = base.load_snapshot(client)
    candidates, skipped = base.build_candidates(snapshot)
    candidates = _filter_secondary_directors(client, candidates, skipped, snapshot, args.workers)
    candidates.sort(key=lambda item: int(item["company_id"]))
    if args.max_companies:
        candidates = candidates[: args.max_companies]
    print(f"[DIRECTOR] candidates={len(candidates)} skipped={len(skipped)}", flush=True)
    rows = base.enrich_candidates(candidates, min(args.workers, 12), args.http_timeout)
    rows = _annotate_director_groups(rows)
    if args.apply:
        rows = apply_rows_v2(client, rows, args.workers)
    summary = _write_report_v2(Path(args.output_dir), rows, skipped, args.apply)
    return 1 if args.apply and summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
