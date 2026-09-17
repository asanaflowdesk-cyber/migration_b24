from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import director_frozen_plan as frozen
import enrich_missing_directors as base
import enrich_missing_directors_v2 as v2
from eqazyna_bitrix.bitrix_client import BitrixClient

# Единственный уже утвержденный план, который был частично выполнен до этого hotfix.
# Разрешаем ему продолжиться после смены SHA только потому, что hotfix НЕ меняет
# набор операций плана: меняется исключительно read-after-write verification.
LEGACY_COMPATIBLE_PLAN = {
    "plan_id": "DIR-07494391A03ABBBD1583E4B4",
    "source_sha": "9a2a2905a2432837884b406bc7c2727c873e67f5",
}

_ORIGINAL_LOAD_FROZEN_PLAN = frozen.load_frozen_plan

READ_BACK_DELAYS = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0)


def load_frozen_plan_compatible(
    path: Path,
    expected_plan_id: str,
    current_sha: str,
) -> dict[str, Any]:
    """Validate a frozen plan and allow exactly one already-approved pre-hotfix plan.

    All hash/schema/PLAN_ID checks are still performed by the original loader.
    The SHA exception is deliberately limited to the one plan that was already
    partially applied before this read-after-write hotfix.
    """
    plan = _ORIGINAL_LOAD_FROZEN_PLAN(
        path,
        expected_plan_id=expected_plan_id,
        current_sha="",
    )
    source_sha = str(plan.get("source_sha") or "")
    if not current_sha or source_sha == current_sha:
        return plan
    if (
        str(plan.get("plan_id") or "") == LEGACY_COMPATIBLE_PLAN["plan_id"]
        and source_sha == LEGACY_COMPATIBLE_PLAN["source_sha"]
    ):
        return plan
    raise RuntimeError(f"plan_code_sha_mismatch:{source_sha}!={current_sha}")


def _eventually(
    check: Callable[[], bool],
    *,
    delays: tuple[float, ...] = READ_BACK_DELAYS,
) -> bool:
    """Retry only READS. Never repeat an unsafe mutation here."""
    for delay in delays:
        if delay:
            time.sleep(delay)
        if check():
            return True
    return False


def _requisite_matches(
    client: BitrixClient,
    req_id: int,
    director: str,
) -> bool:
    # Direct get avoids relying on a potentially stale list page after update.
    item = client.call("crm.requisite.get", {"id": req_id})
    return bool(
        isinstance(item, dict)
        and base.fio_key(item.get("RQ_DIRECTOR")) == base.fio_key(director)
    )


def _company_has_contact(
    client: BitrixClient,
    company_id: int,
    contact_id: int,
) -> bool:
    return contact_id in frozen._contact_ids(
        v2._company_contact_bindings(client, company_id)
    )


def _lead_has_contact(
    client: BitrixClient,
    lead_id: int,
    contact_id: int,
) -> bool:
    return contact_id in frozen._contact_ids(
        v2._lead_contact_bindings(client, lead_id)
    )


def _ensure_company_link_reliable(
    client: BitrixClient,
    contact_id: int,
    company_id: int,
) -> bool:
    current = v2._contact_company_bindings(client, contact_id)
    company_ids = v2._binding_ids(current, "COMPANY_ID")
    if company_id in company_ids:
        return False

    client.call(
        "crm.contact.company.add",
        {
            "id": contact_id,
            "fields": {
                "COMPANY_ID": company_id,
                "IS_PRIMARY": "Y" if not current else "N",
            },
        },
    )
    if not _eventually(lambda: _company_has_contact(client, company_id, contact_id)):
        raise RuntimeError(
            f"contact_company_link_verification_failed:{contact_id}:{company_id}"
        )
    return True


def _ensure_lead_link_reliable(
    client: BitrixClient,
    lead_id: int,
    contact_id: int,
    snapshot_contact_ids: list[int],
) -> bool:
    if _lead_has_contact(client, lead_id, contact_id):
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
    if not _eventually(lambda: _lead_has_contact(client, lead_id, contact_id)):
        raise RuntimeError(f"lead_link_verification_failed:{lead_id}")
    return True


def _apply_group_reliable(
    client: BitrixClient,
    group_rows: list[dict[str, Any]],
    plan_id: str,
) -> list[dict[str, Any]]:
    contact_id = frozen._create_or_get_contact(client, group_rows, plan_id)
    results: list[dict[str, Any]] = []

    for row in sorted(group_rows, key=lambda item: int(item["company_id"])):
        result = dict(row)
        company_id = int(row["company_id"])

        linked_company = _ensure_company_link_reliable(
            client, contact_id, company_id
        )

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
        updated_reqs: list[int] = []
        for req_id in frozen._ids(row.get("requisites_to_update")):
            current = req_by_id.get(req_id)
            if current is None:
                raise RuntimeError(f"planned_requisite_missing:{req_id}")
            if base.fio_key(current.get("RQ_DIRECTOR")) == base.fio_key(
                row["director"]
            ):
                continue

            # One mutation only. Verification below retries READS, not UPDATE.
            client.call(
                "crm.requisite.update",
                {"id": req_id, "fields": {"RQ_DIRECTOR": row["director"]}},
            )
            updated_reqs.append(req_id)
            if not _eventually(
                lambda req_id=req_id: _requisite_matches(
                    client, req_id, row["director"]
                )
            ):
                actual = client.call("crm.requisite.get", {"id": req_id})
                actual_director = (
                    str(actual.get("RQ_DIRECTOR") or "")
                    if isinstance(actual, dict)
                    else "<missing>"
                )
                raise RuntimeError(
                    f"requisite_verification_failed:{req_id}:actual={actual_director!r}"
                )

        snapshot_leads = {
            int(item["id"]): item
            for item in row.get("_snapshot_leads") or []
            if base.normalize_id(item.get("id"))
        }
        added_leads: list[int] = []
        existing_leads: list[int] = []
        for lead_id in frozen._ids(row.get("leads_to_link")):
            snapshot = snapshot_leads.get(lead_id)
            if snapshot is None:
                raise RuntimeError(
                    f"planned_lead_missing_from_snapshot:{lead_id}"
                )
            if _ensure_lead_link_reliable(
                client,
                lead_id,
                contact_id,
                frozen._ids(snapshot.get("contact_ids")),
            ):
                added_leads.append(lead_id)
            else:
                existing_leads.append(lead_id)

        # Final verification is also eventually-consistent aware.
        if not _eventually(
            lambda: _company_has_contact(client, company_id, contact_id)
        ):
            raise RuntimeError(f"company_link_verification_failed:{company_id}")

        for req_id in frozen._ids(row.get("requisites_to_update")):
            if not _eventually(
                lambda req_id=req_id: _requisite_matches(
                    client, req_id, row["director"]
                )
            ):
                actual = client.call("crm.requisite.get", {"id": req_id})
                actual_director = (
                    str(actual.get("RQ_DIRECTOR") or "")
                    if isinstance(actual, dict)
                    else "<missing>"
                )
                raise RuntimeError(
                    f"requisite_verification_failed:{req_id}:actual={actual_director!r}"
                )

        all_planned_leads = set(frozen._ids(row.get("leads_to_link"))) | set(
            frozen._ids(row.get("existing_lead_links"))
        )
        for lead_id in all_planned_leads:
            if not _eventually(
                lambda lead_id=lead_id: _lead_has_contact(
                    client, lead_id, contact_id
                )
            ):
                raise RuntimeError(f"lead_verification_failed:{lead_id}")

        result.update(
            {
                "apply_status": "VERIFIED",
                "contact_id": contact_id,
                "contact_owner_id": row.get("planned_contact_owner_id", ""),
                "contact_company_link_added": int(linked_company),
                "requisites_updated": frozen._csv(updated_reqs),
                "leads_linked": len(added_leads),
                "lead_links_existing": len(existing_leads),
                "lead_ids_verified": frozen._csv(all_planned_leads),
                "verification_status": "VERIFIED",
            }
        )
        results.append(result)

    return results


def apply_frozen_plan_reliable(
    client: BitrixClient,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    ok, errors, _contact_ids_by_group = frozen.preflight_plan(client, plan)
    rows = [dict(row) for row in plan.get("rows") or []]
    if not ok:
        for row in rows:
            if row.get("plan_status") == frozen.PLAN_READY:
                row["apply_status"] = "NOT_STARTED"
                row["verification_status"] = "PREFLIGHT_FAILED"
        return rows, errors

    groups = frozen._group_rows(
        [row for row in rows if row.get("plan_status") == frozen.PLAN_READY]
    )
    applied_by_company: dict[int, dict[str, Any]] = {}
    apply_errors: list[str] = []
    for key in sorted(groups):
        try:
            for result in _apply_group_reliable(
                client, groups[key], str(plan["plan_id"])
            ):
                applied_by_company[int(result["company_id"])] = result
        except Exception as exc:  # noqa: BLE001
            apply_errors.append(
                f"{key}:apply_error:{type(exc).__name__}:{exc}"
            )
            break

    final_rows: list[dict[str, Any]] = []
    for row in rows:
        company_id = int(row["company_id"])
        final_rows.append(applied_by_company.get(company_id, row))
    return sorted(final_rows, key=lambda item: int(item["company_id"])), apply_errors
