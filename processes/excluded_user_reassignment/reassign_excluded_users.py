from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from eqazyna_bitrix.bitrix_client import BitrixClient, BitrixError
from eqazyna_bitrix.settings import Settings


NEW_STATUS_ID = "NEW"


class ReassignmentError(RuntimeError):
    pass


def normalized_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def parse_excluded_user_ids(value: str) -> set[int]:
    tokens = [token.strip() for token in re.split(r"[,;\s]+", value or "") if token.strip()]
    if not tokens:
        raise ReassignmentError("Не указаны ID исключённых пользователей")
    invalid = [token for token in tokens if not token.isdigit() or int(token) <= 0]
    if invalid:
        raise ReassignmentError(
            "Некорректные ID исключённых пользователей: " + ", ".join(invalid)
        )
    return {int(token) for token in tokens}


def _name_part(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def founder_key(contact: dict[str, Any]) -> str | None:
    parts = [
        _name_part(contact.get("LAST_NAME")),
        _name_part(contact.get("NAME")),
        _name_part(contact.get("SECOND_NAME")),
    ]
    if not parts[0] or not parts[1]:
        return None
    return "fio:" + "|".join(parts)


def is_director_contact(contact: dict[str, Any]) -> bool:
    return (
        "руковод" in str(contact.get("POST") or "").casefold()
        or "EQAZYNA_DIRECTOR:" in str(contact.get("COMMENTS") or "")
    )


def _entity_id(row: dict[str, Any]) -> int | None:
    return normalized_id(row.get("ID"))


@dataclass(slots=True)
class OwnerGroup:
    key: str
    seed_old_owner_ids: set[int]
    company_ids: set[int]
    contact_ids: set[int]
    lead_ids: set[int]
    warning: str = ""
    target_owner_id: int | None = None
    assignment_reason: str = ""


def build_owner_groups(
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    excluded_user_ids: set[int],
) -> list[OwnerGroup]:
    companies_list = list(companies)
    director_contacts = [contact for contact in contacts if is_director_contact(contact)]
    leads_list = list(leads)
    company_ids = {
        company_id
        for company in companies_list
        if (company_id := _entity_id(company)) is not None
    }
    contact_by_id = {
        contact_id: contact
        for contact in director_contacts
        if (contact_id := _entity_id(contact)) is not None
    }
    founder_contacts: dict[str, set[int]] = defaultdict(set)
    founder_companies: dict[str, set[int]] = defaultdict(set)
    company_founders: dict[int, set[str]] = defaultdict(set)
    for contact_id, contact in contact_by_id.items():
        key = founder_key(contact)
        if not key:
            continue
        founder_contacts[key].add(contact_id)
        company_id = normalized_id(contact.get("COMPANY_ID"))
        if company_id is not None:
            founder_companies[key].add(company_id)
            company_founders[company_id].add(key)

    # A contact can have another primary COMPANY_ID while a lead links that
    # same person to the company being reassigned. These real CRM links must
    # participate in founder merging too; relying only on contact.COMPANY_ID
    # leaves two founders as separate groups and later reports a false conflict.
    for lead in leads_list:
        company_id = normalized_id(lead.get("COMPANY_ID"))
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        contact = contact_by_id.get(contact_id or -1)
        key = founder_key(contact) if contact else None
        if company_id is not None and key:
            founder_contacts[key].add(int(contact_id))
            founder_companies[key].add(company_id)
            company_founders[company_id].add(key)

    # Several directors/owners may be attached to one company. They are one
    # indivisible client package: every connected founder, company and lead
    # must receive the same manager.
    parent = {key: key for key in founder_contacts}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for keys in company_founders.values():
        ordered = sorted(keys)
        for key in ordered[1:]:
            union(ordered[0], key)

    members_by_root: dict[str, set[str]] = defaultdict(set)
    for key in parent:
        members_by_root[find(key)].add(key)
    canonical_by_founder: dict[str, str] = {}
    group_contacts: dict[str, set[int]] = defaultdict(set)
    group_companies: dict[str, set[int]] = defaultdict(set)
    for members in members_by_root.values():
        ordered = sorted(members)
        canonical = (
            ordered[0]
            if len(ordered) == 1
            else "founders:" + " & ".join(key.removeprefix("fio:") for key in ordered)
        )
        for key in ordered:
            canonical_by_founder[key] = canonical
            group_contacts[canonical].update(founder_contacts[key])
            group_companies[canonical].update(founder_companies[key])
    company_groups: dict[int, set[str]] = defaultdict(set)
    for company_id, keys in company_founders.items():
        company_groups[company_id].update(canonical_by_founder[key] for key in keys)

    lead_by_id = {
        lead_id: lead
        for lead in leads_list
        if (lead_id := _entity_id(lead)) is not None
    }
    seed_leads = [
        lead
        for lead in leads_list
        if normalized_id(lead.get("ASSIGNED_BY_ID")) in excluded_user_ids
    ]
    if not seed_leads:
        raise ReassignmentError(
            "У указанных пользователей не найдено ни одного лида; изменения не требуются"
        )

    groups: dict[str, OwnerGroup] = {}
    for lead in seed_leads:
        lead_id = _entity_id(lead)
        if lead_id is None:
            continue
        company_id = normalized_id(lead.get("COMPANY_ID"))
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        contact = contact_by_id.get(contact_id or -1)
        raw_key = founder_key(contact) if contact else None
        key = canonical_by_founder.get(raw_key) if raw_key else None
        warning = ""
        if key is None and company_id is not None:
            keys = company_groups.get(company_id, set())
            if len(keys) == 1:
                key = next(iter(keys))
        if key is None and company_id is not None:
            key = f"company:{company_id}"
            warning = "Учредитель не определён; объединение выполнено только в пределах компании"
        if key is None:
            key = f"lead:{lead_id}"
            warning = "Лид не связан с компанией/руководителем; переназначается отдельно"

        group = groups.setdefault(
            key,
            OwnerGroup(key, set(), set(), set(), set(), warning=warning),
        )
        group.lead_ids.add(lead_id)
        old_owner = normalized_id(lead.get("ASSIGNED_BY_ID"))
        if old_owner is not None:
            group.seed_old_owner_ids.add(old_owner)
        if company_id is not None:
            group.company_ids.add(company_id)
        if contact_id in contact_by_id:
            group.contact_ids.add(int(contact_id))

    # Expand a founder to all same-FIO director cards, their companies and all
    # linked leads. No status filter is used: closed leads are intentionally included.
    for group in groups.values():
        if group.key in group_contacts:
            group.contact_ids.update(group_contacts[group.key])
            group.company_ids.update(group_companies[group.key])

        for lead_id, lead in lead_by_id.items():
            company_id = normalized_id(lead.get("COMPANY_ID"))
            contact_id = normalized_id(lead.get("CONTACT_ID"))
            if company_id in group.company_ids or contact_id in group.contact_ids:
                group.lead_ids.add(lead_id)
                if company_id is not None:
                    group.company_ids.add(company_id)

        for contact_id, contact in contact_by_id.items():
            company_id = normalized_id(contact.get("COMPANY_ID"))
            if company_id not in group.company_ids:
                continue
            raw_contact_key = founder_key(contact)
            if (
                group.key in group_contacts
                and canonical_by_founder.get(raw_contact_key or "") != group.key
            ):
                continue
            group.contact_ids.add(contact_id)
        group.company_ids.intersection_update(company_ids)

    owner_by_entity: dict[tuple[str, int], str] = {}
    for group in groups.values():
        for entity_type, ids in (
            ("company", group.company_ids),
            ("contact", group.contact_ids),
            ("lead", group.lead_ids),
        ):
            for entity_id in ids:
                entity = (entity_type, entity_id)
                previous = owner_by_entity.get(entity)
                if previous is not None and previous != group.key:
                    raise ReassignmentError(
                        f"{entity_type} ID={entity_id} одновременно относится к учредителям "
                        f"{previous} и {group.key}; автоматическое переназначение остановлено"
                    )
                owner_by_entity[entity] = group.key

    return sorted(groups.values(), key=lambda item: item.key)


def assign_targets(
    groups: list[OwnerGroup],
    target_rop_ids: tuple[int, int],
) -> None:
    rops = tuple(sorted(set(target_rop_ids)))
    if len(rops) != 2 or any(user_id <= 0 for user_id in rops):
        raise ReassignmentError("Нужно указать ровно два корректных ID РОПов")

    # Largest packages go first. Each next indivisible founder package is
    # assigned to the ROP with fewer planned leads. This balances lead counts,
    # rather than merely alternating the number of founders.
    planned_leads = {user_id: 0 for user_id in rops}
    for group in sorted(groups, key=lambda item: (-len(item.lead_ids), item.key)):
        target = min(rops, key=lambda user_id: (planned_leads[user_id], user_id))
        group.target_owner_id = target
        group.assignment_reason = "balanced_between_rops_72_73"
        planned_leads[target] += len(group.lead_ids)


def build_change_rows(
    groups: Iterable[OwnerGroup],
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    company_by_id = {_entity_id(row): row for row in companies if _entity_id(row) is not None}
    contact_by_id = {_entity_id(row): row for row in contacts if _entity_id(row) is not None}
    lead_by_id = {_entity_id(row): row for row in leads if _entity_id(row) is not None}
    rows: list[dict[str, Any]] = []
    for group in groups:
        if group.target_owner_id is None:
            raise ReassignmentError(f"Для группы {group.key} не выбран новый ответственный")
        for entity_type, ids, source in (
            ("contact", group.contact_ids, contact_by_id),
            ("company", group.company_ids, company_by_id),
            ("lead", group.lead_ids, lead_by_id),
        ):
            for entity_id in sorted(ids):
                record = source.get(entity_id, {})
                old_owner = normalized_id(record.get("ASSIGNED_BY_ID"))
                old_status = str(record.get("STATUS_ID") or "") if entity_type == "lead" else ""
                needs_change = old_owner != group.target_owner_id or (
                    entity_type == "lead" and old_status != NEW_STATUS_ID
                )
                rows.append(
                    {
                        "founder_key": group.key,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "title": str(
                            record.get("TITLE")
                            or " ".join(
                                str(record.get(field) or "").strip()
                                for field in ("LAST_NAME", "NAME", "SECOND_NAME")
                            ).strip()
                        ),
                        "old_owner_id": old_owner or "",
                        "new_owner_id": group.target_owner_id,
                        "old_status_id": old_status,
                        "new_status_id": NEW_STATUS_ID if entity_type == "lead" else "",
                        "assignment_reason": group.assignment_reason,
                        "warning": group.warning,
                        "action": "pending" if needs_change else "already_matches",
                        "error": "",
                    }
                )
    return rows


def apply_changes(client: BitrixClient, rows: list[dict[str, Any]]) -> None:
    update_methods = {
        "contact": client.update_contact,
        "company": client.update_company,
        "lead": client.update_lead,
    }
    for row in rows:
        if row["action"] != "pending":
            continue
        fields: dict[str, Any] = {"ASSIGNED_BY_ID": int(row["new_owner_id"])}
        if row["entity_type"] == "lead":
            fields["STATUS_ID"] = NEW_STATUS_ID
        try:
            update_methods[row["entity_type"]](str(row["entity_id"]), fields)
            row["action"] = "updated"
        except Exception as exc:  # noqa: BLE001
            row["action"] = "update_error"
            row["error"] = str(exc)


def verify_changes(
    rows: list[dict[str, Any]],
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
) -> None:
    sources = {
        "company": {_entity_id(row): row for row in companies if _entity_id(row) is not None},
        "contact": {_entity_id(row): row for row in contacts if _entity_id(row) is not None},
        "lead": {_entity_id(row): row for row in leads if _entity_id(row) is not None},
    }
    for row in rows:
        if row["action"] == "update_error":
            continue
        current = sources[row["entity_type"]].get(int(row["entity_id"]))
        current_owner = normalized_id((current or {}).get("ASSIGNED_BY_ID"))
        owner_matches = current_owner == int(row["new_owner_id"])
        status_matches = (
            row["entity_type"] != "lead"
            or str((current or {}).get("STATUS_ID") or "") == NEW_STATUS_ID
        )
        if current is None or not owner_matches or not status_matches:
            row["action"] = "verify_error"
            row["error"] = (
                f"Контрольное чтение: owner={current_owner}, "
                f"status={str((current or {}).get('STATUS_ID') or '')!r}"
            )


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "founder_key", "entity_type", "entity_id", "title", "old_owner_id",
        "new_owner_id", "old_status_id", "new_status_id", "assignment_reason",
        "warning", "action", "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def load_crm(
    client: BitrixClient,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    companies = client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"}, "filter": {},
            "select": [
                "ID", "TITLE", "ASSIGNED_BY_ID", "ORIGIN_ID", "ADDRESS",
                "ADDRESS_CITY", "ADDRESS_REGION", "ADDRESS_PROVINCE",
            ],
        },
    )
    contacts = client.list_all(
        "crm.contact.list",
        {
            "order": {"ID": "ASC"}, "filter": {},
            "select": [
                "ID", "LAST_NAME", "NAME", "SECOND_NAME", "POST", "COMPANY_ID",
                "ASSIGNED_BY_ID", "COMMENTS",
            ],
        },
    )
    leads = client.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"}, "filter": {},
            "select": [
                "ID", "TITLE", "COMPANY_ID", "CONTACT_ID", "ASSIGNED_BY_ID",
                "STATUS_ID", "STATUS_SEMANTIC_ID", "DATE_CREATE",
            ],
        },
    )
    return companies, contacts, leads


def validate_users(
    client: BitrixClient,
    user_ids: Iterable[int],
    *,
    label: str,
    require_active: bool = False,
) -> None:
    for user_id in sorted(user_ids):
        user = client.get_user(user_id)
        if not user:
            raise ReassignmentError(f"{label} ID={user_id} не найден в Bitrix24")
        active = str(user.get("ACTIVE", "true")).strip().casefold()
        if require_active and active in {"false", "n", "0", "нет"}:
            raise ReassignmentError(f"{label} ID={user_id} неактивен в Bitrix24")


def run(
    client: BitrixClient,
    excluded_user_ids: set[int],
    output_dir: Path,
    apply: bool,
) -> dict[str, int]:
    target_rop_ids = tuple(
        sorted(parse_excluded_user_ids(os.getenv("REASSIGN_TARGET_ROP_IDS", "72,73")))
    )
    if len(target_rop_ids) != 2:
        raise ReassignmentError("REASSIGN_TARGET_ROP_IDS должен содержать ровно два ID")
    overlap = sorted(excluded_user_ids & set(target_rop_ids))
    if overlap:
        raise ReassignmentError(
            "ID одновременно указан как исключённый пользователь и целевой РОП: "
            + ", ".join(map(str, overlap))
        )
    validate_users(client, excluded_user_ids, label="Исключённый пользователь")
    validate_users(client, target_rop_ids, label="Целевой РОП", require_active=True)
    companies, contacts, leads = load_crm(client)
    groups = build_owner_groups(companies, contacts, leads, excluded_user_ids)
    assign_targets(groups, target_rop_ids)
    rows = build_change_rows(groups, companies, contacts, leads)
    if apply:
        apply_changes(client, rows)
        stabilize_seconds = float(os.getenv("REASSIGN_STABILIZE_SECONDS", "12"))
        final_wait_seconds = float(os.getenv("REASSIGN_FINAL_WAIT_SECONDS", "5"))
        if stabilize_seconds > 0:
            print(
                f"Ожидание {stabilize_seconds:g} сек. перед проверкой роботов Bitrix24..."
            )
            time.sleep(stabilize_seconds)
        refreshed_companies, refreshed_contacts, refreshed_leads = load_crm(client)
        verify_changes(rows, refreshed_companies, refreshed_contacts, refreshed_leads)
        if not any(row["action"] == "verify_error" for row in rows):
            if final_wait_seconds > 0:
                print(
                    f"Ожидание {final_wait_seconds:g} сек. перед итоговой проверкой..."
                )
                time.sleep(final_wait_seconds)
            refreshed_companies, refreshed_contacts, refreshed_leads = load_crm(client)
            verify_changes(rows, refreshed_companies, refreshed_contacts, refreshed_leads)
    write_report(output_dir / "excluded_user_reassignment.csv", rows)
    update_errors = sum(row["action"] == "update_error" for row in rows)
    verify_errors = sum(row["action"] == "verify_error" for row in rows)
    summary = {
        "mode_apply": int(apply),
        "excluded_users": len(excluded_user_ids),
        "founders": len(groups),
        "leads": sum(row["entity_type"] == "lead" for row in rows),
        "companies": sum(row["entity_type"] == "company" for row in rows),
        "contacts": sum(row["entity_type"] == "contact" for row in rows),
        "pending": sum(row["action"] == "pending" for row in rows),
        "updated": sum(row["action"] == "updated" for row in rows),
        "already_matches": sum(row["action"] == "already_matches" for row in rows),
        "update_errors": update_errors,
        "verify_errors": verify_errors,
    }
    for rop_id in target_rop_ids:
        summary[f"rop_{rop_id}_founders"] = sum(
            group.target_owner_id == rop_id for group in groups
        )
        summary[f"rop_{rop_id}_leads"] = sum(
            len(group.lead_ids) for group in groups if group.target_owner_id == rop_id
        )
    (output_dir / "excluded_user_reassignment_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Переназначить все лиды исключённых пользователей, связанные компании и "
            "контакты руководителей; пакеты учредителей сбалансировать между РОП 72 и 73."
        )
    )
    parser.add_argument(
        "--excluded-user-ids",
        default=os.getenv("EXCLUDED_USER_IDS", ""),
        help="ID через запятую; по умолчанию читаются из EXCLUDED_USER_IDS",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(
            build_client(), parse_excluded_user_ids(args.excluded_user_ids),
            output_dir, apply=args.apply,
        )
    except (ReassignmentError, BitrixError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["update_errors"] or summary["verify_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
