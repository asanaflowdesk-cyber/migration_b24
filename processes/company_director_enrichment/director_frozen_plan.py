from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import enrich_missing_directors as base
import enrich_missing_directors_v2 as v2
from eqazyna_bitrix.bitrix_client import BitrixClient

PLAN_SCHEMA_VERSION = 1
PLAN_READY = "READY"
PLAN_BLOCKED = "BLOCKED"
PLAN_NOT_APPLICABLE = "NOT_APPLICABLE"
PLAN_MARKER_PREFIX = "DIRECTOR_PLAN_ID:"

_STABLE_SOURCE_STATUSES = {
    "found",
    "no_director",
    "HTTP_404",
    "bin_mismatch",
    "not_checked",
    "",
}


def _ids(value: Any) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value or "").split(",")
    result: list[int] = []
    for item in items:
        raw = str(item or "").strip()
        if raw.isdigit() and int(raw) > 0:
            result.append(int(raw))
    return sorted(set(result))


def _csv(values: list[int] | set[int]) -> str:
    return ",".join(str(value) for value in sorted(set(values)))


def _contact_ids(items: list[dict[str, Any]]) -> list[int]:
    return sorted(v2._binding_ids(items, "CONTACT_ID"))


def _company_ids(items: list[dict[str, Any]]) -> list[int]:
    return sorted(v2._binding_ids(items, "COMPANY_ID"))


def _source_is_technical(status: Any) -> bool:
    value = str(status or "").strip()
    if value in _STABLE_SOURCE_STATUSES:
        return False
    if value.startswith("HTTP_4") and value != "HTTP_429":
        return False
    return True


def mark_source_unavailable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Separate transient source failures from a real 'not found' result."""
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("status") == "no_result":
            statuses = [item.get("adata_status"), item.get("kompra_status")]
            if any(_source_is_technical(status) for status in statuses):
                item["status"] = "source_unavailable"
                item["evidence"] = " | ".join(
                    f"{name}={item.get(name + '_status', '')}"
                    for name in ("adata", "kompra")
                )
        result.append(item)
    return result


def _group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("status") == "accepted" and base.valid_fio(row.get("director")):
            groups[base.fio_key(row["director"])].append(dict(row))
    return groups


def _company_leads(client: BitrixClient, company_id: int) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"COMPANY_ID": company_id},
            "select": [
                "ID",
                "COMPANY_ID",
                "CONTACT_ID",
                "CONTACT_IDS",
                "ASSIGNED_BY_ID",
            ],
        },
    )


def _requisite_snapshot(requisites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in requisites:
        req_id = base.normalize_id(row.get("ID"))
        if req_id is None:
            continue
        result.append(
            {
                "id": req_id,
                "bin": base.normalize_bin(row.get("RQ_INN")),
                "director": base.normalize_fio(row.get("RQ_DIRECTOR"))
                if base.valid_fio(row.get("RQ_DIRECTOR"))
                else "",
            }
        )
    return sorted(result, key=lambda item: item["id"])


def _lead_snapshot(client: BitrixClient, company_id: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for lead in _company_leads(client, company_id):
        lead_id = base.normalize_id(lead.get("ID"))
        if lead_id is None:
            continue
        bindings = v2._lead_contact_bindings(client, lead_id)
        result.append(
            {
                "id": lead_id,
                "owner_id": base.normalize_id(lead.get("ASSIGNED_BY_ID")),
                "contact_ids": _contact_ids(bindings),
            }
        )
    return sorted(result, key=lambda item: item["id"])


def _planned_requisites(
    requisites: list[dict[str, Any]],
    bin_number: str,
    director: str,
) -> tuple[list[int], str]:
    update_ids: list[int] = []
    wanted = base.fio_key(director)
    for requisite in requisites:
        req_id = base.normalize_id(requisite.get("ID"))
        if req_id is None:
            continue
        current = requisite.get("RQ_DIRECTOR")
        if base.valid_fio(current):
            if base.fio_key(current) != wanted:
                return [], f"requisite_director_conflict:{req_id}"
            continue
        req_bin = base.normalize_bin(requisite.get("RQ_INN"))
        if req_bin and req_bin != bin_number:
            continue
        update_ids.append(req_id)
    return sorted(set(update_ids)), ""


def _resolve_identity_for_plan(
    client: BitrixClient,
    director: str,
) -> tuple[str, dict[str, Any] | None, list[int], str]:
    """Return CREATE/REUSE/BLOCKED with conservative exact-FIO identity rules."""
    matches = v2._global_contacts_for_director(client, director)
    match_ids = [
        value
        for row in matches
        if (value := base.normalize_id(row.get("ID"))) is not None
    ]
    if not matches:
        return "CREATE", None, [], ""
    if len(matches) > 1:
        return "BLOCKED", None, sorted(match_ids), "ambiguous_multiple_exact_fio_contacts"
    contact = matches[0]
    if not base.is_director_contact(contact):
        return (
            "BLOCKED",
            None,
            sorted(match_ids),
            "ordinary_contact_same_fio_requires_review",
        )
    return "REUSE", contact, [], ""


def _plan_group(client: BitrixClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted((dict(row) for row in rows), key=lambda item: int(item["company_id"]))
    director = base.normalize_fio(ordered[0]["director"])
    group_company_ids = [int(row["company_id"]) for row in ordered]

    action, existing_contact, duplicate_ids, identity_error = _resolve_identity_for_plan(
        client, director
    )
    existing_contact_id = (
        base.normalize_id(existing_contact.get("ID")) if existing_contact else None
    )
    existing_contact_owner_id = (
        base.normalize_id(existing_contact.get("ASSIGNED_BY_ID"))
        if existing_contact
        else None
    )

    fresh: dict[int, dict[str, Any]] = {}
    owners: set[int] = set()
    for row in ordered:
        company_id = int(row["company_id"])
        company, requisites, _contacts, state = v2._fresh_row_state(
            client, row, director
        )
        company_owner_id = (
            base.normalize_id(company.get("ASSIGNED_BY_ID")) if company else None
        )
        company_contact_ids = _contact_ids(
            v2._company_contact_bindings(client, company_id)
        )
        leads = _lead_snapshot(client, company_id)
        fresh[company_id] = {
            "company": company,
            "requisites": requisites,
            "state": state,
            "company_owner_id": company_owner_id,
            "company_contact_ids": company_contact_ids,
            "leads": leads,
        }
        if company_owner_id is not None:
            owners.add(company_owner_id)

    group_error = identity_error
    planned_contact_owner_id = existing_contact_owner_id
    if not group_error and action == "REUSE":
        if existing_contact_id is None:
            group_error = "contact_invalid_id"
        elif planned_contact_owner_id is None:
            if len(owners) != 1:
                group_error = "director_owner_conflict"
            else:
                planned_contact_owner_id = next(iter(owners))
    elif not group_error and action == "CREATE":
        if any(fresh[cid]["company"] is None for cid in group_company_ids):
            group_error = "company_missing"
        elif any(fresh[cid]["company_owner_id"] is None for cid in group_company_ids):
            group_error = "company_without_owner"
        elif len(owners) != 1:
            group_error = "director_owner_conflict"
        else:
            planned_contact_owner_id = next(iter(owners))

    existing_contact_company_ids: list[int] = []
    if existing_contact_id is not None:
        existing_contact_company_ids = _company_ids(
            v2._contact_company_bindings(client, existing_contact_id)
        )

    primary_company_id = min(group_company_ids) if group_company_ids else None
    results: list[dict[str, Any]] = []
    for row in ordered:
        result = dict(row)
        company_id = int(row["company_id"])
        state = fresh[company_id]
        company = state["company"]
        requisites = state["requisites"]
        company_owner_id = state["company_owner_id"]
        company_contact_ids = state["company_contact_ids"]
        leads = state["leads"]

        result.update(
            {
                "plan_status": PLAN_READY,
                "plan_block_reason": "",
                "planned_contact_action": action,
                "existing_contact_id": existing_contact_id or "",
                "existing_contact_owner_id": existing_contact_owner_id or "",
                "planned_contact_id": existing_contact_id or "NEW",
                "planned_contact_owner_id": planned_contact_owner_id or "",
                "company_owner_id": company_owner_id or "",
                "owner_mismatch": "",
                "workflow31_company_would_reassign": "",
                "workflow31_would_reassign": "",
                "workflow31_target_owner_id": planned_contact_owner_id or "",
                "director_group_company_ids": _csv(group_company_ids),
                "existing_contact_company_ids": _csv(existing_contact_company_ids),
                "companies_to_link": "",
                "duplicate_contact_ids": _csv(duplicate_ids),
                "company_link_action": "",
                "company_contact_snapshot": _csv(company_contact_ids),
                "requisites_to_update": "",
                "requisites_to_update_count": 0,
                "lead_ids_all": _csv([item["id"] for item in leads]),
                "leads_to_link": "",
                "leads_to_link_count": 0,
                "existing_lead_links": "",
                "existing_lead_links_count": 0,
                "lead_owner_snapshot": ",".join(
                    f"{item['id']}:{item['owner_id'] or 'NONE'}" for item in leads
                ),
                "lead_contact_snapshot": " | ".join(
                    f"{item['id']}:{_csv(item['contact_ids']) or '-'}" for item in leads
                ),
                "workflow31_leads_to_reassign": "",
                "workflow31_leads_to_reassign_count": 0,
                "_snapshot_company_contact_ids": company_contact_ids,
                "_snapshot_requisites": _requisite_snapshot(requisites),
                "_snapshot_leads": leads,
                "_snapshot_existing_contact": (
                    {
                        "id": existing_contact_id,
                        "owner_id": existing_contact_owner_id,
                        "fio": base.contact_fio(existing_contact),
                        "is_director": bool(base.is_director_contact(existing_contact)),
                        "company_ids": existing_contact_company_ids,
                    }
                    if existing_contact
                    else None
                ),
            }
        )

        if group_error:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = group_error
            result["planned_contact_action"] = "BLOCKED"
            results.append(result)
            continue
        if state["state"] != "ok" or company is None:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = state["state"]
            results.append(result)
            continue
        if company_owner_id is None:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = "company_without_owner"
            results.append(result)
            continue

        mismatch = bool(
            planned_contact_owner_id
            and company_owner_id != planned_contact_owner_id
        )
        result["owner_mismatch"] = "YES" if mismatch else "NO"
        result["workflow31_company_would_reassign"] = "YES" if mismatch else "NO"

        if action == "REUSE":
            result["company_link_action"] = (
                "ALREADY_LINKED"
                if existing_contact_id in company_contact_ids
                else "ADD_LINK"
            )
        else:
            result["company_link_action"] = (
                "CREATE_PRIMARY"
                if company_id == primary_company_id
                else "ADD_SECONDARY_AFTER_CREATE"
            )

        group_to_link = [
            cid
            for cid in group_company_ids
            if action == "CREATE"
            or existing_contact_id
            not in fresh[cid]["company_contact_ids"]
        ]
        result["companies_to_link"] = _csv(group_to_link)

        req_ids, req_error = _planned_requisites(
            requisites, row["bin"], director
        )
        if req_error:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = req_error
            results.append(result)
            continue
        result["requisites_to_update"] = _csv(req_ids)
        result["requisites_to_update_count"] = len(req_ids)

        leads_to_link: list[int] = []
        existing_leads: list[int] = []
        leads_to_reassign: list[int] = []
        for lead in leads:
            if existing_contact_id is not None and existing_contact_id in lead["contact_ids"]:
                existing_leads.append(lead["id"])
            else:
                leads_to_link.append(lead["id"])
            if (
                planned_contact_owner_id
                and lead["owner_id"] != planned_contact_owner_id
            ):
                leads_to_reassign.append(lead["id"])

        result["leads_to_link"] = _csv(leads_to_link)
        result["leads_to_link_count"] = len(leads_to_link)
        result["existing_lead_links"] = _csv(existing_leads)
        result["existing_lead_links_count"] = len(existing_leads)
        result["workflow31_leads_to_reassign"] = _csv(leads_to_reassign)
        result["workflow31_leads_to_reassign_count"] = len(leads_to_reassign)
        result["workflow31_would_reassign"] = (
            "YES" if mismatch or leads_to_reassign else "NO"
        )
        results.append(result)

    return results


def build_exact_dry_run_plan(
    client: BitrixClient,
    rows: list[dict[str, Any]],
    workers: int,
) -> list[dict[str, Any]]:
    untouched: list[dict[str, Any]] = []
    groups = _group_rows(rows)
    accepted_keys = set(groups)
    for row in rows:
        key = (
            base.fio_key(row.get("director"))
            if base.valid_fio(row.get("director"))
            else ""
        )
        if not (row.get("status") == "accepted" and key in accepted_keys):
            item = dict(row)
            item["plan_status"] = PLAN_NOT_APPLICABLE
            item["plan_block_reason"] = ""
            untouched.append(item)

    planned: list[dict[str, Any]] = []
    # Keep plan construction deterministic and easier on Bitrix. External lookup
    # is already parallel; this phase is read-only CRM validation.
    for index, key in enumerate(sorted(groups), 1):
        group_rows = groups[key]
        try:
            planned.extend(_plan_group(client, group_rows))
        except Exception as exc:  # noqa: BLE001
            for row in group_rows:
                item = dict(row)
                item["plan_status"] = PLAN_BLOCKED
                item["plan_block_reason"] = f"plan_error:{type(exc).__name__}"
                item["planned_contact_action"] = "BLOCKED"
                planned.append(item)
        print(f"[DIRECTOR] plan {index}/{len(groups)} director={key}", flush=True)

    return sorted(untouched + planned, key=lambda item: int(item["company_id"]))


def _canonical_payload(
    rows: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    source_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_sha": source_sha,
        "rows": rows,
        "skipped": skipped,
    }


def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "DIR-" + hashlib.sha256(raw).hexdigest()[:24].upper()


def create_frozen_plan(
    rows: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    source_sha: str,
    source_run_id: str,
) -> dict[str, Any]:
    payload = _canonical_payload(rows, skipped, source_sha)
    return {
        **payload,
        "plan_id": _hash_payload(payload),
        "source_run_id": str(source_run_id or ""),
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def save_frozen_plan(output_dir: Path, plan: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "company_director_plan.json"
    path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "PLAN_ID.txt").write_text(
        f"PLAN_ID={plan.get('plan_id', '')}\n"
        f"RUN_ID={plan.get('source_run_id', '')}\n"
        f"SOURCE_SHA={plan.get('source_sha', '')}\n",
        encoding="utf-8",
    )
    return path


def load_frozen_plan(
    path: Path,
    expected_plan_id: str,
    current_sha: str,
) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if int(plan.get("schema_version") or 0) != PLAN_SCHEMA_VERSION:
        raise RuntimeError("plan_schema_version_mismatch")
    payload = _canonical_payload(
        list(plan.get("rows") or []),
        list(plan.get("skipped") or []),
        str(plan.get("source_sha") or ""),
    )
    calculated = _hash_payload(payload)
    if calculated != str(plan.get("plan_id") or ""):
        raise RuntimeError("plan_hash_invalid")
    if expected_plan_id and calculated != expected_plan_id.strip():
        raise RuntimeError("plan_id_mismatch")
    if current_sha and str(plan.get("source_sha") or "") != current_sha:
        raise RuntimeError(
            f"plan_code_sha_mismatch:{plan.get('source_sha')}!={current_sha}"
        )
    return plan


def _plan_created_contact(
    client: BitrixClient,
    director: str,
    plan_id: str,
) -> dict[str, Any] | None:
    marker = f"{PLAN_MARKER_PREFIX} {plan_id}"
    matches = v2._global_contacts_for_director(client, director)
    marked = [
        item
        for item in matches
        if marker in str(item.get("COMMENTS") or "")
    ]
    if len(marked) == 1 and len(matches) == 1:
        return marked[0]
    return None


def _current_contact_for_group(
    client: BitrixClient,
    group_rows: list[dict[str, Any]],
    plan_id: str,
) -> tuple[dict[str, Any] | None, str]:
    first = group_rows[0]
    director = base.normalize_fio(first["director"])
    action = first.get("planned_contact_action")
    matches = v2._global_contacts_for_director(client, director)

    if action == "REUSE":
        expected_id = base.normalize_id(first.get("existing_contact_id"))
        if expected_id is None:
            return None, "preflight_missing_expected_contact_id"
        ids = {
            value
            for item in matches
            if (value := base.normalize_id(item.get("ID"))) is not None
        }
        if ids != {expected_id}:
            return None, "preflight_exact_fio_contact_set_changed"
        contact = matches[0]
        if not base.is_director_contact(contact):
            return None, "preflight_existing_contact_not_director"
        current_owner = base.normalize_id(contact.get("ASSIGNED_BY_ID"))
        old_owner = base.normalize_id(first.get("existing_contact_owner_id"))
        planned_owner = base.normalize_id(first.get("planned_contact_owner_id"))
        allowed_owners = {value for value in (old_owner, planned_owner) if value}
        if current_owner not in allowed_owners and not (
            current_owner is None and old_owner is None
        ):
            return None, "preflight_contact_owner_changed"
        return contact, ""

    if action == "CREATE":
        if not matches:
            return None, ""
        existing = _plan_created_contact(client, director, plan_id)
        if existing is None:
            return None, "preflight_new_exact_fio_contact_appeared"
        planned_owner = base.normalize_id(first.get("planned_contact_owner_id"))
        if base.normalize_id(existing.get("ASSIGNED_BY_ID")) != planned_owner:
            return None, "preflight_plan_created_contact_owner_changed"
        return existing, ""

    return None, "preflight_invalid_contact_action"


def _row_preflight(
    client: BitrixClient,
    row: dict[str, Any],
    contact: dict[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    company_id = int(row["company_id"])
    director = base.normalize_fio(row["director"])
    company, requisites, _contacts, state = v2._fresh_row_state(
        client, row, director
    )
    if state != "ok" or company is None:
        return [f"{company_id}:{state}"]

    current_owner = base.normalize_id(company.get("ASSIGNED_BY_ID"))
    expected_owner = base.normalize_id(row.get("company_owner_id"))
    if current_owner != expected_owner:
        errors.append(
            f"{company_id}:company_owner_changed:{expected_owner}->{current_owner}"
        )
    if base.current_bin(company, requisites) != row["bin"]:
        errors.append(f"{company_id}:bin_changed")

    contact_id = base.normalize_id(contact.get("ID")) if contact else None

    current_company_contacts = _contact_ids(
        v2._company_contact_bindings(client, company_id)
    )
    snapshot_company_contacts = _ids(row.get("_snapshot_company_contact_ids"))
    allowed_company_sets = {tuple(snapshot_company_contacts)}
    if contact_id is not None and row.get("company_link_action") != "ALREADY_LINKED":
        allowed_company_sets.add(tuple(sorted(set(snapshot_company_contacts + [contact_id]))))
    if tuple(current_company_contacts) not in allowed_company_sets:
        errors.append(f"{company_id}:company_contact_set_changed")

    snapshot_reqs = {
        int(item["id"]): item
        for item in row.get("_snapshot_requisites") or []
        if base.normalize_id(item.get("id"))
    }
    current_reqs = {
        req_id: {
            "id": req_id,
            "bin": base.normalize_bin(item.get("RQ_INN")),
            "director": base.normalize_fio(item.get("RQ_DIRECTOR"))
            if base.valid_fio(item.get("RQ_DIRECTOR"))
            else "",
        }
        for item in requisites
        if (req_id := base.normalize_id(item.get("ID"))) is not None
    }
    if set(current_reqs) != set(snapshot_reqs):
        errors.append(f"{company_id}:requisite_set_changed")
    target_req_ids = set(_ids(row.get("requisites_to_update")))
    for req_id, snapshot in snapshot_reqs.items():
        current = current_reqs.get(req_id)
        if current is None:
            continue
        if current["bin"] != snapshot.get("bin", ""):
            errors.append(f"{company_id}:requisite_bin_changed:{req_id}")
        if req_id in target_req_ids:
            if current["director"] not in {"", director}:
                errors.append(f"{company_id}:requisite_director_changed:{req_id}")
        elif current["director"] != snapshot.get("director", ""):
            errors.append(f"{company_id}:non_target_requisite_changed:{req_id}")

    snapshot_leads = {
        int(item["id"]): item
        for item in row.get("_snapshot_leads") or []
        if base.normalize_id(item.get("id"))
    }
    current_leads = {
        int(item["id"]): item
        for item in _lead_snapshot(client, company_id)
    }
    if set(current_leads) != set(snapshot_leads):
        errors.append(f"{company_id}:lead_set_changed")

    to_link = set(_ids(row.get("leads_to_link")))
    for lead_id, snapshot in snapshot_leads.items():
        current = current_leads.get(lead_id)
        if current is None:
            continue
        if base.normalize_id(current.get("owner_id")) != base.normalize_id(
            snapshot.get("owner_id")
        ):
            errors.append(f"{company_id}:lead_owner_changed:{lead_id}")
        snapshot_contacts = _ids(snapshot.get("contact_ids"))
        allowed = {tuple(snapshot_contacts)}
        if contact_id is not None and lead_id in to_link:
            allowed.add(tuple(sorted(set(snapshot_contacts + [contact_id]))))
        if tuple(_ids(current.get("contact_ids"))) not in allowed:
            errors.append(f"{company_id}:lead_contact_set_changed:{lead_id}")

    return errors


def preflight_plan(
    client: BitrixClient,
    plan: dict[str, Any],
) -> tuple[bool, list[str], dict[str, int | None]]:
    ready_rows = [
        dict(row)
        for row in plan.get("rows") or []
        if row.get("plan_status") == PLAN_READY
    ]
    groups = _group_rows(ready_rows)
    errors: list[str] = []
    group_contact_ids: dict[str, int | None] = {}

    for key in sorted(groups):
        group_rows = groups[key]
        contact, error = _current_contact_for_group(
            client, group_rows, str(plan["plan_id"])
        )
        if error:
            errors.append(f"{key}:{error}")
            continue
        contact_id = base.normalize_id(contact.get("ID")) if contact else None
        group_contact_ids[key] = contact_id
        for row in group_rows:
            errors.extend(_row_preflight(client, row, contact))

    return not errors, errors, group_contact_ids


def _comments_with_plan_marker(
    existing: str,
    plan_id: str,
    source: str,
    url: str,
) -> str:
    text = base.provenance_comments(existing, source, url)
    marker = f"{PLAN_MARKER_PREFIX} {plan_id}"
    if marker not in text:
        text = (text.rstrip() + "\n" + marker).strip()
    return text


def _create_or_get_contact(
    client: BitrixClient,
    group_rows: list[dict[str, Any]],
    plan_id: str,
) -> int:
    first = group_rows[0]
    director = base.normalize_fio(first["director"])
    action = first["planned_contact_action"]
    planned_owner = base.normalize_id(first.get("planned_contact_owner_id"))
    if planned_owner is None:
        raise RuntimeError("planned_contact_owner_missing")

    if action == "REUSE":
        contact_id = base.normalize_id(first.get("existing_contact_id"))
        if contact_id is None:
            raise RuntimeError("planned_contact_id_missing")
        contact = client.call("crm.contact.get", {"id": contact_id})
        if not isinstance(contact, dict):
            raise RuntimeError("planned_contact_missing")
        if base.normalize_id(contact.get("ASSIGNED_BY_ID")) is None:
            client.call(
                "crm.contact.update",
                {"id": contact_id, "fields": {"ASSIGNED_BY_ID": planned_owner}},
            )
        return contact_id

    existing = _plan_created_contact(client, director, plan_id)
    if existing is not None:
        contact_id = base.normalize_id(existing.get("ID"))
        if contact_id is None:
            raise RuntimeError("plan_created_contact_invalid_id")
        return contact_id

    parts = base.fio_parts(director)
    if parts is None:
        raise RuntimeError("invalid_director_fio")
    last_name, first_name, second_name = parts
    primary_company_id = min(int(row["company_id"]) for row in group_rows)
    added = client.call(
        "crm.contact.add",
        {
            "fields": {
                "LAST_NAME": last_name,
                "NAME": first_name,
                "SECOND_NAME": second_name,
                "POST": "Руководитель",
                "COMPANY_ID": primary_company_id,
                "ASSIGNED_BY_ID": planned_owner,
                "COMMENTS": _comments_with_plan_marker(
                    "",
                    plan_id,
                    first.get("source", ""),
                    first.get("url", ""),
                ),
            }
        },
    )
    contact_id = base.normalize_id(added)
    if contact_id is None:
        raise RuntimeError("contact_create_failed")
    return contact_id


def _ensure_specific_lead_link(
    client: BitrixClient,
    lead_id: int,
    contact_id: int,
    snapshot_contact_ids: list[int],
) -> bool:
    bindings = v2._lead_contact_bindings(client, lead_id)
    current = _contact_ids(bindings)
    if contact_id in current:
        return False
    client.call(
        "crm.lead.contact.add",
        {
            "id": lead_id,
            "fields": {
                "CONTACT_ID": contact_id,
                "IS_PRIMARY": "Y" if not snapshot_contact_ids else "N",
            },
        },
    )
    verify = _contact_ids(v2._lead_contact_bindings(client, lead_id))
    if contact_id not in verify:
        raise RuntimeError(f"lead_link_verification_failed:{lead_id}")
    return True


def _apply_group(
    client: BitrixClient,
    group_rows: list[dict[str, Any]],
    plan_id: str,
) -> list[dict[str, Any]]:
    contact_id = _create_or_get_contact(client, group_rows, plan_id)
    results: list[dict[str, Any]] = []
    for row in sorted(group_rows, key=lambda item: int(item["company_id"])):
        result = dict(row)
        company_id = int(row["company_id"])
        linked_company = v2._ensure_contact_company_link(
            client, contact_id, company_id
        )

        updated_reqs: list[int] = []
        requisites = client.list_all(
            "crm.requisite.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": company_id},
                "select": ["ID", "ENTITY_ID", "RQ_INN", "RQ_DIRECTOR"],
            },
        )
        req_by_id = {
            req_id: item
            for item in requisites
            if (req_id := base.normalize_id(item.get("ID"))) is not None
        }
        for req_id in _ids(row.get("requisites_to_update")):
            current = req_by_id.get(req_id)
            if current is None:
                raise RuntimeError(f"planned_requisite_missing:{req_id}")
            if base.fio_key(current.get("RQ_DIRECTOR")) == base.fio_key(
                row["director"]
            ):
                continue
            client.call(
                "crm.requisite.update",
                {"id": req_id, "fields": {"RQ_DIRECTOR": row["director"]}},
            )
            updated_reqs.append(req_id)

        snapshot_leads = {
            int(item["id"]): item
            for item in row.get("_snapshot_leads") or []
            if base.normalize_id(item.get("id"))
        }
        added_leads: list[int] = []
        existing_leads: list[int] = []
        for lead_id in _ids(row.get("leads_to_link")):
            snapshot = snapshot_leads.get(lead_id)
            if snapshot is None:
                raise RuntimeError(f"planned_lead_missing_from_snapshot:{lead_id}")
            if _ensure_specific_lead_link(
                client,
                lead_id,
                contact_id,
                _ids(snapshot.get("contact_ids")),
            ):
                added_leads.append(lead_id)
            else:
                existing_leads.append(lead_id)

        # Final verification for this row.
        if contact_id not in _contact_ids(
            v2._company_contact_bindings(client, company_id)
        ):
            raise RuntimeError(f"company_link_verification_failed:{company_id}")
        verify_reqs = client.list_all(
            "crm.requisite.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": company_id},
                "select": ["ID", "RQ_DIRECTOR"],
            },
        )
        verify_by_id = {
            req_id: item
            for item in verify_reqs
            if (req_id := base.normalize_id(item.get("ID"))) is not None
        }
        for req_id in _ids(row.get("requisites_to_update")):
            req = verify_by_id.get(req_id)
            if req is None or base.fio_key(req.get("RQ_DIRECTOR")) != base.fio_key(
                row["director"]
            ):
                raise RuntimeError(f"requisite_verification_failed:{req_id}")

        all_planned_leads = set(_ids(row.get("leads_to_link"))) | set(
            _ids(row.get("existing_lead_links"))
        )
        for lead_id in all_planned_leads:
            if contact_id not in _contact_ids(
                v2._lead_contact_bindings(client, lead_id)
            ):
                raise RuntimeError(f"lead_verification_failed:{lead_id}")

        result.update(
            {
                "apply_status": "VERIFIED",
                "contact_id": contact_id,
                "contact_owner_id": row.get("planned_contact_owner_id", ""),
                "contact_company_link_added": int(linked_company),
                "requisites_updated": _csv(updated_reqs),
                "leads_linked": len(added_leads),
                "lead_links_existing": len(existing_leads),
                "lead_ids_verified": _csv(all_planned_leads),
                "verification_status": "VERIFIED",
            }
        )
        results.append(result)
    return results


def apply_frozen_plan(
    client: BitrixClient,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    ok, errors, _contact_ids_by_group = preflight_plan(client, plan)
    rows = [dict(row) for row in plan.get("rows") or []]
    if not ok:
        for row in rows:
            if row.get("plan_status") == PLAN_READY:
                row["apply_status"] = "NOT_STARTED"
                row["verification_status"] = "PREFLIGHT_FAILED"
        return rows, errors

    groups = _group_rows(
        [row for row in rows if row.get("plan_status") == PLAN_READY]
    )
    applied_by_company: dict[int, dict[str, Any]] = {}
    apply_errors: list[str] = []
    for key in sorted(groups):
        try:
            for result in _apply_group(
                client, groups[key], str(plan["plan_id"])
            ):
                applied_by_company[int(result["company_id"])] = result
        except Exception as exc:  # noqa: BLE001
            apply_errors.append(f"{key}:apply_error:{type(exc).__name__}:{exc}")
            break

    final_rows: list[dict[str, Any]] = []
    for row in rows:
        company_id = int(row["company_id"])
        final_rows.append(applied_by_company.get(company_id, row))
    return sorted(final_rows, key=lambda item: int(item["company_id"])), apply_errors
