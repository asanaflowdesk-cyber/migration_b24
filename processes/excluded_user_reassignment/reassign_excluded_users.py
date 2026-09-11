from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from eqazyna_bitrix.bitrix_client import BitrixClient, BitrixError
from eqazyna_bitrix.distribution import (
    AssignmentUser,
    DistributionSnapshot,
    DistributionError,
    GoogleSheetDistributionSource,
    branch_key,
    is_branch_head,
    normalise_bin,
)
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
        key = founder_key(contact) if contact else None
        warning = ""
        if key is None and company_id is not None:
            keys = company_founders.get(company_id, set())
            if len(keys) == 1:
                key = next(iter(keys))
            elif len(keys) > 1:
                raise ReassignmentError(
                    f"У компании ID={company_id} найдено несколько руководителей; "
                    "нельзя однозначно определить учредителя"
                )
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
        if group.key.startswith("fio:"):
            group.contact_ids.update(founder_contacts.get(group.key, set()))
            group.company_ids.update(founder_companies.get(group.key, set()))

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
            if group.key.startswith("fio:") and founder_key(contact) != group.key:
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


def _company_bins(
    company: dict[str, Any], requisites_by_company: dict[int, set[str]]
) -> set[str]:
    company_id = _entity_id(company)
    values = set(requisites_by_company.get(company_id or -1, set()))
    origin_id = normalise_bin(company.get("ORIGIN_ID"))
    if len(origin_id) == 12:
        values.add(origin_id)
    return values


def _stable_tie_choice(group_key: str, candidates: list[int]) -> int:
    ordered = sorted(candidates)
    digest = hashlib.sha256(group_key.encode("utf-8")).digest()
    return ordered[int.from_bytes(digest[:8], "big") % len(ordered)]


def assign_targets(
    groups: list[OwnerGroup],
    snapshot: DistributionSnapshot,
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    requisites_by_company: dict[int, set[str]],
    excluded_user_ids: set[int],
    excluded_user_departments: dict[int, set[int]],
    astana_department_id: int = 46,
) -> None:
    approved: dict[int, AssignmentUser] = {user.user_id: user for user in snapshot.users}
    overlap = sorted(set(approved) & excluded_user_ids)
    if overlap:
        raise ReassignmentError(
            "Исключённые пользователи всё ещё присутствуют в user_list: "
            + ", ".join(map(str, overlap))
        )

    companies_by_id = {
        company_id: company
        for company in companies
        if (company_id := _entity_id(company)) is not None
    }
    contacts_by_id = {
        contact_id: contact
        for contact in contacts
        if (contact_id := _entity_id(contact)) is not None
    }
    loads = Counter()
    for lead in leads:
        owner_id = normalized_id(lead.get("ASSIGNED_BY_ID"))
        semantic = str(lead.get("STATUS_SEMANTIC_ID") or "").strip().upper()
        if owner_id in approved and semantic not in {"S", "F"}:
            loads[owner_id] += 1

    users_by_department: dict[int, list[int]] = defaultdict(list)
    for user in snapshot.users:
        users_by_department[user.department_id].append(user.user_id)
    new_founders_by_manager: dict[int, set[str]] = defaultdict(set)

    for group in groups:
        fixed_targets: set[int] = set()
        for company_id in group.company_ids:
            for bin_number in _company_bins(
                companies_by_id.get(company_id, {}), requisites_by_company
            ):
                target = snapshot.company_assignments.get(bin_number)
                if target is not None:
                    fixed_targets.add(target)
        if len(fixed_targets) > 1:
            raise ReassignmentError(
                f"Для учредителя {group.key} Company_fix задаёт разных менеджеров: "
                + ", ".join(map(str, sorted(fixed_targets)))
            )
        if fixed_targets:
            target = next(iter(fixed_targets))
            if target not in approved:
                raise ReassignmentError(
                    f"Company_fix назначает учредителя {group.key} пользователю ID={target}, "
                    "которого нет в user_list"
                )
            group.target_owner_id = target
            group.assignment_reason = "company_fix"
            continue

        active_contact_owners = {
            owner_id
            for contact_id in group.contact_ids
            if (owner_id := normalized_id(
                contacts_by_id.get(contact_id, {}).get("ASSIGNED_BY_ID")
            )) in approved
        }
        if len(active_contact_owners) > 1:
            raise ReassignmentError(
                f"У учредителя {group.key} уже несколько действующих ответственных: "
                + ", ".join(map(str, sorted(active_contact_owners)))
            )
        if active_contact_owners:
            group.target_owner_id = next(iter(active_contact_owners))
            group.assignment_reason = "existing_active_founder_owner"
            continue

        astana = any(
            branch_key(
                " ".join(
                    str(companies_by_id.get(company_id, {}).get(field) or "")
                    for field in (
                        "ADDRESS", "ADDRESS_CITY", "ADDRESS_REGION", "ADDRESS_PROVINCE"
                    )
                )
            ) == "astana"
            for company_id in group.company_ids
        )
        old_departments = {
            department_id
            for old_owner_id in group.seed_old_owner_ids
            for department_id in excluded_user_departments.get(old_owner_id, set())
            if department_id in users_by_department
        }
        if astana:
            department_id = astana_department_id
            reason_prefix = "astana"
        elif len(old_departments) == 1:
            department_id = next(iter(old_departments))
            reason_prefix = "old_owner_department"
        elif len(old_departments) > 1:
            raise ReassignmentError(
                f"У учредителя {group.key} исходные ответственные относятся к разным подразделениям: "
                + ", ".join(map(str, sorted(old_departments)))
            )
        else:
            department_id = None
            reason_prefix = "global"

        scope = (
            sorted(users_by_department.get(department_id, []))
            if department_id else sorted(approved)
        )
        if not scope:
            raise ReassignmentError(
                f"Для учредителя {group.key} не найден пользователь в целевом подразделении"
            )
        if len(scope) == 1:
            target = scope[0]
            reason = f"{reason_prefix}_single_user"
        else:
            regular = [user_id for user_id in scope if not is_branch_head(approved[user_id].role)]
            available = [user_id for user_id in regular if not new_founders_by_manager[user_id]]
            if available:
                minimum = min(loads[user_id] for user_id in available)
                candidates = [user_id for user_id in available if loads[user_id] == minimum]
                target = _stable_tie_choice(group.key, candidates)
                new_founders_by_manager[target].add(group.key)
                reason = f"{reason_prefix}_least_loaded"
            else:
                heads = [user_id for user_id in scope if is_branch_head(approved[user_id].role)]
                if not heads:
                    raise ReassignmentError(
                        f"Для учредителя {group.key} исчерпан лимит менеджеров, но РОП не указан"
                    )
                minimum = min(loads[user_id] for user_id in heads)
                target = _stable_tie_choice(
                    group.key, [user_id for user_id in heads if loads[user_id] == minimum]
                )
                reason = f"{reason_prefix}_overflow_to_rop"
        loads[target] += 1
        group.target_owner_id = target
        group.assignment_reason = reason


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


def load_requisites(client: BitrixClient) -> dict[int, set[str]]:
    rows = client.list_all(
        "crm.requisite.list",
        {
            "order": {"ID": "ASC"}, "filter": {"ENTITY_TYPE_ID": 4},
            "select": ["ID", "ENTITY_ID", "RQ_BIN"],
        },
    )
    result: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        company_id = normalized_id(row.get("ENTITY_ID"))
        bin_number = normalise_bin(row.get("RQ_BIN"))
        if company_id is not None and len(bin_number) == 12:
            result[company_id].add(bin_number)
    return result


def load_user_departments(
    client: BitrixClient, user_ids: set[int]
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    for user_id in sorted(user_ids):
        user = client.get_user(user_id)
        if not user:
            raise ReassignmentError(f"Исключённый пользователь ID={user_id} не найден в Bitrix24")
        raw = user.get("UF_DEPARTMENT")
        values = raw if isinstance(raw, list) else [raw]
        result[user_id] = {
            int(str(value))
            for value in values
            if str(value or "").strip().isdigit() and int(str(value)) > 0
        }
    return result


def run(
    client: BitrixClient,
    excluded_user_ids: set[int],
    output_dir: Path,
    apply: bool,
) -> dict[str, int]:
    snapshot = GoogleSheetDistributionSource(
        spreadsheet_id=os.getenv(
            "DISTRIBUTION_SHEET_ID",
            "1WuRHHyQm5lHxDlW81m4P0oZJ6X_SDYj1bN-NOx8aM2k",
        ),
        users_sheet=os.getenv("DISTRIBUTION_USERS_SHEET", "user_list"),
        assignments_sheet=os.getenv("DISTRIBUTION_FIXES_SHEET", "Company_fix"),
    ).load()
    companies, contacts, leads = load_crm(client)
    requisites = load_requisites(client)
    excluded_departments = load_user_departments(client, excluded_user_ids)
    groups = build_owner_groups(companies, contacts, leads, excluded_user_ids)
    assign_targets(
        groups, snapshot, companies, contacts, leads, requisites,
        excluded_user_ids, excluded_departments,
        astana_department_id=int(os.getenv("ASTANA_DEPARTMENT_ID", "46")),
    )
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
            "контакты руководителей; все карточки одного учредителя получает один менеджер."
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
    except (ReassignmentError, DistributionError, BitrixError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["update_errors"] or summary["verify_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
