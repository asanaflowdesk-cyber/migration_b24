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


def contact_full_name(contact: dict[str, Any]) -> str:
    return " ".join(
        str(contact.get(field) or "").strip()
        for field in ("LAST_NAME", "NAME", "SECOND_NAME")
        if str(contact.get(field) or "").strip()
    )


def user_full_name(user: dict[str, Any], user_id: int) -> str:
    name = " ".join(
        str(user.get(field) or "").strip()
        for field in ("LAST_NAME", "NAME", "SECOND_NAME")
        if str(user.get(field) or "").strip()
    )
    return name or f"ID {user_id}"


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
    skip_lead_ids: set[int] | None = None,
) -> list[OwnerGroup]:
    skip_lead_ids = skip_lead_ids or set()
    companies_list = list(companies)
    contacts_list = list(contacts)
    leads_list = list(leads)
    company_ids = {
        company_id
        for company in companies_list
        if (company_id := _entity_id(company)) is not None
    }
    all_contact_by_id = {
        contact_id: contact
        for contact in contacts_list
        if (contact_id := _entity_id(contact)) is not None
    }
    lead_contact_ids = {
        contact_id
        for lead in leads_list
        if (contact_id := normalized_id(lead.get("CONTACT_ID"))) is not None
    }
    # CONTACT_ID in a lead is the primary evidence of its founder. Do not drop
    # a real linked contact merely because POST/COMMENTS lacks a service marker.
    contact_by_id = {
        contact_id: contact
        for contact_id, contact in all_contact_by_id.items()
        if founder_key(contact)
        and (contact_id in lead_contact_ids or is_director_contact(contact))
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

    seed_leads = [
        lead
        for lead in leads_list
        if normalized_id(lead.get("ASSIGNED_BY_ID")) in excluded_user_ids
        and _entity_id(lead) not in skip_lead_ids
    ]
    if not seed_leads:
        if skip_lead_ids:
            return []
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

    # Expand only the package context (same-FIO founder cards and companies).
    # Lead ownership is a hard boundary: a lead is eligible only when its current
    # ASSIGNED_BY_ID was explicitly entered in excluded_user_ids. In particular,
    # do not pull another manager's leads merely because they share a company or
    # founder with an excluded user's lead.
    for group in groups.values():
        if group.key in group_contacts:
            group.contact_ids.update(group_contacts[group.key])
            group.company_ids.update(group_companies[group.key])

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


def collect_linkage_issues(
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    lead_ids: set[int],
) -> list[dict[str, Any]]:
    company_by_id = {_entity_id(row): row for row in companies if _entity_id(row) is not None}
    contact_by_id = {_entity_id(row): row for row in contacts if _entity_id(row) is not None}
    issues: list[dict[str, Any]] = []
    for lead in leads:
        lead_id = _entity_id(lead)
        if lead_id not in lead_ids:
            continue
        company_id = normalized_id(lead.get("COMPANY_ID"))
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        company = company_by_id.get(company_id)
        contact = contact_by_id.get(contact_id)
        errors: list[str] = []
        if company_id is None:
            errors.append("не указан COMPANY_ID")
        elif company is None:
            errors.append(f"компания ID={company_id} не найдена")
        if contact_id is None:
            errors.append("не указан CONTACT_ID учредителя")
        elif contact is None:
            errors.append(f"контакт учредителя ID={contact_id} не найден")
        elif founder_key(contact) is None:
            errors.append("у контакта учредителя не заполнены фамилия и имя")
        if not errors:
            continue
        action = (
            "skipped_missing_company"
            if any("COMPANY_ID" in error or "компания ID=" in error for error in errors)
            else "skipped_missing_founder"
        )
        issues.append(
            {
                "lead_id": lead_id or "",
                "lead_title": str(lead.get("TITLE") or ""),
                "old_owner_id": normalized_id(lead.get("ASSIGNED_BY_ID")) or "",
                "old_status_id": str(lead.get("STATUS_ID") or ""),
                "company_id": company_id or "",
                "company_title": str((company or {}).get("TITLE") or ""),
                "company_bin": str((company or {}).get("ORIGIN_ID") or ""),
                "contact_id": contact_id or "",
                "founder_name": contact_full_name(contact or {}),
                "action": action,
                "error": "; ".join(errors),
            }
        )
    return issues



TALDYKORGAN_TOKENS = ("талдыкорган", "талдықорған", "taldykorgan")


def _is_taldykorgan_department_name(value: Any) -> bool:
    text = _name_part(value)
    return any(token in text for token in TALDYKORGAN_TOKENS)


def _owner_branch_kind(
    owner_id: int, owner_department_names: dict[int, set[str]],
) -> str:
    """Return ``taldykorgan``, ``other`` or ``unknown`` for a CRM owner.

    Branch means the Bitrix24 department of the responsible employee, not the
    client's legal/postal address. If a user has several department labels and
    at least one of them is Taldykorgan, we classify the owner as Taldykorgan
    so a parent/root department does not create a false multi-branch match.
    """
    names = {str(name).strip() for name in owner_department_names.get(owner_id, set()) if str(name).strip()}
    if any(_is_taldykorgan_department_name(name) for name in names):
        return "taldykorgan"
    if names:
        return "other"
    return "unknown"


def split_reassignment_groups(
    groups: Iterable[OwnerGroup],
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    excluded_user_ids: set[int],
    target_rop_ids: tuple[int, int],
    owner_department_names: dict[int, set[str]] | None = None,
) -> tuple[list[OwnerGroup], list[dict[str, Any]]]:
    """Keep all source packages except packages linked to Taldykorgan.

    Important idempotency rules:
    - 72/73 are migration targets, not evidence that a package belongs to Taldykorgan;
      their departments are ignored for the Taldykorgan gate.
    - if any entity of a partially migrated package is already on exactly one of 72/73,
      the remaining source entities inherit that same target.  We never rebalance an
      already-started package to the other ROP.
    - a non-excluded lead is context only and is never reassigned by this flow.
    - missing founder is not a blocker: company + source lead is a shortened package;
      a source lead without a company is a one-lead package.
    """
    companies_list = list(companies)
    contacts_list = list(contacts)
    leads_list = list(leads)
    owner_department_names = owner_department_names or {}
    target_ids = set(target_rop_ids)
    company_by_id = {_entity_id(row): row for row in companies_list if _entity_id(row) is not None}
    contact_by_id = {_entity_id(row): row for row in contacts_list if _entity_id(row) is not None}
    lead_by_id = {_entity_id(row): row for row in leads_list if _entity_id(row) is not None}

    eligible: list[OwnerGroup] = []
    skipped: list[dict[str, Any]] = []

    for group in groups:
        related_leads = [
            lead
            for lead in leads_list
            if normalized_id(lead.get("COMPANY_ID")) in group.company_ids
            or normalized_id(lead.get("CONTACT_ID")) in group.contact_ids
            or _entity_id(lead) in group.lead_ids
        ]

        # All current owners provide context.  Target ROPs are intentionally excluded
        # from branch detection because they may be present only due to an earlier
        # partial migration attempt.
        package_owner_ids: set[int] = set()
        for company_id in group.company_ids:
            owner_id = normalized_id((company_by_id.get(company_id) or {}).get("ASSIGNED_BY_ID"))
            if owner_id is not None:
                package_owner_ids.add(owner_id)
        for contact_id in group.contact_ids:
            owner_id = normalized_id((contact_by_id.get(contact_id) or {}).get("ASSIGNED_BY_ID"))
            if owner_id is not None:
                package_owner_ids.add(owner_id)
        for lead in related_leads:
            owner_id = normalized_id(lead.get("ASSIGNED_BY_ID"))
            if owner_id is not None:
                package_owner_ids.add(owner_id)

        existing_targets = sorted(package_owner_ids & target_ids)
        if len(existing_targets) > 1:
            raise ReassignmentError(
                f"Пакет {group.key} уже частично распределён одновременно на "
                f"{existing_targets}; автоматическое продолжение остановлено, чтобы не дробить пакет"
            )
        if existing_targets:
            group.target_owner_id = existing_targets[0]
            group.assignment_reason = "continue_existing_package_target"

        branch_owner_ids = package_owner_ids - target_ids
        taldyk_owner_ids = sorted(
            owner_id
            for owner_id in branch_owner_ids
            if _owner_branch_kind(owner_id, owner_department_names) == "taldykorgan"
        )

        if not taldyk_owner_ids:
            eligible.append(group)
            continue

        branch_details = []
        for owner_id in taldyk_owner_ids:
            names = sorted(owner_department_names.get(owner_id, set()))
            branch_details.append(
                f"{owner_id}:" + "/".join(names) if names else str(owner_id)
            )
        error = (
            "пакет связан с Талдыкорганом"
            + (f" ({' | '.join(branch_details)})" if branch_details else "")
        )
        for lead_id in sorted(group.lead_ids):
            lead = lead_by_id.get(lead_id, {})
            company_id = normalized_id(lead.get("COMPANY_ID"))
            contact_id = normalized_id(lead.get("CONTACT_ID"))
            company = company_by_id.get(company_id, {})
            contact = contact_by_id.get(contact_id, {})
            skipped.append(
                {
                    "lead_id": lead_id,
                    "lead_title": str(lead.get("TITLE") or ""),
                    "old_owner_id": normalized_id(lead.get("ASSIGNED_BY_ID")) or "",
                    "old_status_id": str(lead.get("STATUS_ID") or ""),
                    "company_id": company_id or "",
                    "company_title": str(company.get("TITLE") or ""),
                    "company_bin": str(company.get("ORIGIN_ID") or ""),
                    "contact_id": contact_id or "",
                    "founder_name": contact_full_name(contact),
                    "action": "skipped_taldykorgan",
                    "error": error,
                }
            )

    return sorted(eligible, key=lambda item: item.key), skipped

def assign_targets(
    groups: list[OwnerGroup],
    target_rop_ids: tuple[int, int],
) -> None:
    rops = tuple(sorted(set(target_rop_ids)))
    if len(rops) != 2 or any(user_id <= 0 for user_id in rops):
        raise ReassignmentError("Нужно указать ровно два корректных ID РОПов")

    # Preserve a target already present inside a partially migrated package.  Only
    # completely new packages are balanced.  Existing packages count toward the
    # balancing totals so new assignments do not undo earlier distribution.
    planned_leads = {user_id: 0 for user_id in rops}
    for group in groups:
        if group.target_owner_id is not None:
            if group.target_owner_id not in planned_leads:
                raise ReassignmentError(
                    f"Для пакета {group.key} найден неожиданный целевой владелец "
                    f"{group.target_owner_id}"
                )
            planned_leads[group.target_owner_id] += len(group.lead_ids)

    unassigned = [group for group in groups if group.target_owner_id is None]
    for group in sorted(unassigned, key=lambda item: (-len(item.lead_ids), item.key)):
        target = min(rops, key=lambda user_id: (planned_leads[user_id], user_id))
        group.target_owner_id = target
        group.assignment_reason = "balanced_between_rops_72_73"
        planned_leads[target] += len(group.lead_ids)

def build_change_rows(
    groups: Iterable[OwnerGroup],
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    *,
    excluded_user_ids: Iterable[int] = (),
    user_names: dict[int, str] | None = None,
    status_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    user_names = user_names or {}
    status_names = status_names or {}
    excluded_ids = set(excluded_user_ids)
    excluded_ids_text = ",".join(map(str, sorted(excluded_ids)))
    company_by_id = {_entity_id(row): row for row in companies if _entity_id(row) is not None}
    contact_by_id = {_entity_id(row): row for row in contacts if _entity_id(row) is not None}
    lead_by_id = {_entity_id(row): row for row in leads if _entity_id(row) is not None}
    rows: list[dict[str, Any]] = []
    for group in groups:
        if group.target_owner_id is None:
            raise ReassignmentError(f"Для группы {group.key} не выбран новый ответственный")
        founder_names = sorted(
            {contact_full_name(contact_by_id[contact_id]) for contact_id in group.contact_ids}
        )
        package_company_titles = sorted(
            str(company_by_id[company_id].get("TITLE") or "")
            for company_id in group.company_ids
        )
        package_company_bins = sorted(
            {
                str(company_by_id[company_id].get("ORIGIN_ID") or "").strip()
                for company_id in group.company_ids
                if str(company_by_id[company_id].get("ORIGIN_ID") or "").strip()
            }
        )
        package_fields = {
            "source_excluded_user_ids": excluded_ids_text,
            "package_lead_count": len(group.lead_ids),
            "package_company_count": len(group.company_ids),
            "package_contact_count": len(group.contact_ids),
            "package_seed_old_owner_ids": ",".join(map(str, sorted(group.seed_old_owner_ids))),
            "founder_contact_ids": ",".join(map(str, sorted(group.contact_ids))),
            "founder_names": " | ".join(founder_names),
            "company_ids": ",".join(map(str, sorted(group.company_ids))),
            "company_titles": " | ".join(package_company_titles),
            "company_bins": ",".join(package_company_bins),
        }
        for entity_type, ids, source in (
            ("contact", group.contact_ids, contact_by_id),
            ("company", group.company_ids, company_by_id),
            ("lead", group.lead_ids, lead_by_id),
        ):
            for entity_id in sorted(ids):
                record = source.get(entity_id, {})
                old_owner = normalized_id(record.get("ASSIGNED_BY_ID"))
                # A package is indivisible for its company/contact context.
                # Therefore company/contact rows are moved with an eligible package
                # even when their current owner is outside the exclusion list.
                # Non-excluded LEADS remain protected because build_owner_groups()
                # never adds them to group.lead_ids.
                if entity_type == "lead" and old_owner not in excluded_ids:
                    continue
                old_status = str(record.get("STATUS_ID") or "") if entity_type == "lead" else ""
                lead_company_id = (
                    normalized_id(record.get("COMPANY_ID"))
                    if entity_type == "lead" else None
                )
                lead_contact_id = (
                    normalized_id(record.get("CONTACT_ID"))
                    if entity_type == "lead" else None
                )
                lead_company = company_by_id.get(lead_company_id, {})
                lead_contact = contact_by_id.get(lead_contact_id, {})
                needs_change = old_owner != group.target_owner_id
                rows.append(
                    {
                        **package_fields,
                        "founder_key": group.key,
                        "lead_company_id": lead_company_id or "",
                        "lead_company_title": str(lead_company.get("TITLE") or ""),
                        "lead_company_bin": str(lead_company.get("ORIGIN_ID") or ""),
                        "lead_contact_id": lead_contact_id or "",
                        "lead_founder_name": contact_full_name(lead_contact),
                        "lead_date_create": (
                            str(record.get("DATE_CREATE") or "")
                            if entity_type == "lead" else ""
                        ),
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
                        "old_owner_name": user_names.get(old_owner or -1, ""),
                        "new_owner_id": group.target_owner_id,
                        "new_owner_name": user_names.get(group.target_owner_id, ""),
                        "old_status_id": old_status,
                        "old_status_name": status_names.get(old_status, "") if old_status else "",
                        "old_status_semantic_id": (
                            str(record.get("STATUS_SEMANTIC_ID") or "")
                            if entity_type == "lead" else ""
                        ),
                        "new_status_id": "",
                        "new_status_name": "",
                        "assignment_reason": group.assignment_reason,
                        "warning": group.warning,
                        "action": "pending" if needs_change else "already_matches",
                        "error": "",
                    }
                )
    return rows


def _progress_every() -> int:
    raw = str(os.getenv("REASSIGN_PROGRESS_EVERY", "25") or "25").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 25


def _print_progress(
    label: str,
    done: int,
    total: int,
    *,
    ok: int | None = None,
    errors: int | None = None,
    force: bool = False,
) -> None:
    if total <= 0:
        return
    every = _progress_every()
    if not force and done not in {1, total} and done % every != 0:
        return
    pct = (done / total) * 100.0
    suffix = ""
    if ok is not None:
        suffix += f" | успешно: {ok}"
    if errors is not None:
        suffix += f" | ошибок: {errors}"
    print(f"[{label}] {done}/{total} ({pct:.1f}%){suffix}", flush=True)


def _strict_owner_update(
    client: BitrixClient,
    entity_type: str,
    entity_id: int,
    new_owner_id: int,
) -> None:
    method = {
        "lead": "crm.lead.update",
        "company": "crm.company.update",
        "contact": "crm.contact.update",
    }[entity_type]
    result = client.call(
        method,
        {
            "id": int(entity_id),
            "fields": {"ASSIGNED_BY_ID": int(new_owner_id)},
            "params": {"REGISTER_SONET_EVENT": "N"},
        },
    )
    if result is not True and str(result).upper() not in {"1", "TRUE"}:
        raise ReassignmentError(
            f"{method} ID={entity_id}: Bitrix вернул неожиданный result={result!r}"
        )


def apply_owner_changes(client: BitrixClient, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Change only ASSIGNED_BY_ID. Stages/statuses are never touched by this flow."""
    eligible = [row for row in rows if row["action"] == "pending"]
    total = len(eligible)
    ok = 0
    errors = 0
    if total:
        print(f"Переназначение владельцев: {total} сущностей.", flush=True)
    for index, row in enumerate(eligible, start=1):
        try:
            _strict_owner_update(
                client,
                str(row["entity_type"]),
                int(row["entity_id"]),
                int(row["new_owner_id"]),
            )
            row["action"] = "updated"
            ok += 1
        except Exception as exc:  # noqa: BLE001
            row["action"] = "update_error"
            row["error"] = str(exc)
            errors += 1
        _print_progress("ПЕРЕНОС", index, total, ok=ok, errors=errors)
    return {"planned": total, "updated": ok, "errors": errors}

def verify_changes(
    rows: list[dict[str, Any]],
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
) -> dict[str, int]:
    """Verify only ASSIGNED_BY_ID; this flow intentionally does not touch lead stages."""
    sources = {
        "company": {_entity_id(row): row for row in companies if _entity_id(row) is not None},
        "contact": {_entity_id(row): row for row in contacts if _entity_id(row) is not None},
        "lead": {_entity_id(row): row for row in leads if _entity_id(row) is not None},
    }
    owner_ok = owner_errors = 0
    for row in rows:
        if row["action"] == "update_error":
            continue
        current = sources[row["entity_type"]].get(int(row["entity_id"]))
        current_owner = normalized_id((current or {}).get("ASSIGNED_BY_ID"))
        row["current_owner_id"] = current_owner or ""
        if row["entity_type"] == "lead":
            row["current_status_id"] = str((current or {}).get("STATUS_ID") or "")
        if current is None or current_owner != int(row["new_owner_id"]):
            row["action"] = "verify_error"
            row["error"] = (
                f"Контроль владельца: owner={current_owner}, ожидается {row['new_owner_id']}"
            )
            owner_errors += 1
        else:
            if row["action"] != "already_matches":
                row["action"] = "updated"
            owner_ok += 1
    return {"owner_ok": owner_ok, "owner_errors": owner_errors}

def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "source_excluded_user_ids", "founder_key", "package_lead_count",
        "package_company_count", "package_contact_count", "package_seed_old_owner_ids",
        "founder_contact_ids", "founder_names", "company_ids", "company_titles",
        "company_bins", "lead_company_id", "lead_company_title", "lead_company_bin",
        "lead_contact_id", "lead_founder_name", "lead_date_create", "entity_type",
        "entity_id", "title",
        "old_owner_id", "old_owner_name", "new_owner_id", "new_owner_name", "current_owner_id",
        "old_status_id", "old_status_name", "old_status_semantic_id", "current_status_id",
        "new_status_id", "new_status_name",
        "assignment_reason", "warning", "action", "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_linkage_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "lead_id", "lead_title", "old_owner_id", "old_status_id", "company_id",
        "company_title", "company_bin", "contact_id", "founder_name", "action",
        "error",
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
) -> dict[int, str]:
    names: dict[int, str] = {}
    for user_id in sorted(user_ids):
        user = client.get_user(user_id)
        if not user:
            raise ReassignmentError(f"{label} ID={user_id} не найден в Bitrix24")
        active = str(user.get("ACTIVE", "true")).strip().casefold()
        if require_active and active in {"false", "n", "0", "нет"}:
            raise ReassignmentError(f"{label} ID={user_id} неактивен в Bitrix24")
        names[user_id] = user_full_name(user, user_id)
    return names


def collect_group_owner_ids(
    groups: Iterable[OwnerGroup],
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
) -> set[int]:
    """Return owners of every entity connected to the candidate packages."""
    groups_list = list(groups)
    company_ids = set().union(*(group.company_ids for group in groups_list)) if groups_list else set()
    contact_ids = set().union(*(group.contact_ids for group in groups_list)) if groups_list else set()
    owners: set[int] = set()
    for row in companies:
        if _entity_id(row) in company_ids:
            owner_id = normalized_id(row.get("ASSIGNED_BY_ID"))
            if owner_id is not None:
                owners.add(owner_id)
    for row in contacts:
        if _entity_id(row) in contact_ids:
            owner_id = normalized_id(row.get("ASSIGNED_BY_ID"))
            if owner_id is not None:
                owners.add(owner_id)
    for row in leads:
        company_id = normalized_id(row.get("COMPANY_ID"))
        contact_id = normalized_id(row.get("CONTACT_ID"))
        if company_id in company_ids or contact_id in contact_ids:
            owner_id = normalized_id(row.get("ASSIGNED_BY_ID"))
            if owner_id is not None:
                owners.add(owner_id)
    return owners


def load_user_department_names(
    client: BitrixClient, user_ids: Iterable[int],
) -> dict[int, set[str]]:
    """Resolve direct Bitrix24 department names for package owners."""
    department_cache: dict[int, str] = {}
    result: dict[int, set[str]] = {}
    for user_id in sorted(set(user_ids)):
        user = client.get_user(user_id)
        if not user:
            result[user_id] = set()
            continue
        raw = user.get("UF_DEPARTMENT")
        values = raw if isinstance(raw, list) else [raw]
        department_ids = {
            department_id
            for value in values
            if (department_id := normalized_id(value)) is not None
        }
        names: set[str] = set()
        for department_id in sorted(department_ids):
            if department_id not in department_cache:
                department = client.get_department(department_id)
                department_cache[department_id] = str((department or {}).get("NAME") or "").strip()
            name = department_cache[department_id]
            if name:
                names.add(name)
        result[user_id] = names
    return result


def load_user_names(client: BitrixClient, user_ids: Iterable[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    for user_id in sorted(set(user_ids)):
        user = client.get_user(user_id)
        if user:
            names[user_id] = user_full_name(user, user_id)
    return names


def load_lead_status_names(client: BitrixClient) -> dict[str, str]:
    statuses = client.list_all(
        "crm.status.list",
        {
            "order": {"SORT": "ASC"},
            "filter": {"ENTITY_ID": "STATUS"},
            "select": ["STATUS_ID", "NAME"],
        },
    )
    return {
        str(row.get("STATUS_ID") or ""): str(row.get("NAME") or "")
        for row in statuses
        if str(row.get("STATUS_ID") or "")
    }


def run(
    client: BitrixClient,
    excluded_user_ids: set[int],
    output_dir: Path,
    apply: bool,
) -> dict[str, Any]:
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
    excluded_names = validate_users(
        client, excluded_user_ids, label="Исключённый пользователь"
    )
    target_names = validate_users(
        client, target_rop_ids, label="Целевой РОП", require_active=True
    )
    companies, contacts, leads = load_crm(client)
    seed_lead_ids = {
        int(lead_id)
        for lead in leads
        if normalized_id(lead.get("ASSIGNED_BY_ID")) in excluded_user_ids
        and (lead_id := _entity_id(lead)) is not None
    }
    if not seed_lead_ids:
        raise ReassignmentError(
            "У указанных пользователей не найдено ни одного лида; изменения не требуются"
        )
    # Missing founder/company is not a blocker. build_owner_groups() falls back to
    # company:<ID> (company + lead) and then lead:<ID> (single-lead package).
    # The linkage scan is retained only for diagnostics in the main report/log.
    all_linkage_issues = collect_linkage_issues(
        companies, contacts, leads, seed_lead_ids
    )
    groups = build_owner_groups(
        companies,
        contacts,
        leads,
        excluded_user_ids,
        skip_lead_ids=set(),
    )
    package_owner_ids = collect_group_owner_ids(groups, companies, contacts, leads)
    owner_department_names = load_user_department_names(client, package_owner_ids)
    groups, package_skip_issues = split_reassignment_groups(
        groups, companies, contacts, leads, excluded_user_ids, target_rop_ids, owner_department_names
    )
    relevant_lead_ids = set().union(*(group.lead_ids for group in groups))
    skipped_issues = list(package_skip_issues)
    assign_targets(groups, target_rop_ids)
    relevant_owner_ids = set(target_rop_ids) | set(excluded_user_ids)
    entity_ids = {
        "company": set().union(*(group.company_ids for group in groups)),
        "contact": set().union(*(group.contact_ids for group in groups)),
        "lead": relevant_lead_ids,
    }
    for entity_type, records in (
        ("company", companies), ("contact", contacts), ("lead", leads)
    ):
        relevant_owner_ids.update(
            owner_id
            for record in records
            if _entity_id(record) in entity_ids[entity_type]
            and (owner_id := normalized_id(record.get("ASSIGNED_BY_ID"))) is not None
        )
    known_names = dict(excluded_names)
    known_names.update(target_names)
    user_names = load_user_names(client, relevant_owner_ids - set(known_names))
    user_names.update(known_names)
    status_names = load_lead_status_names(client)
    rows = build_change_rows(
        groups, companies, contacts, leads,
        excluded_user_ids=excluded_user_ids,
        user_names=user_names,
        status_names=status_names,
    )
    owner_result = {
        "planned": sum(row["action"] == "pending" for row in rows),
        "updated": 0,
        "errors": 0,
    }
    verify_result = {"owner_ok": 0, "owner_errors": 0}

    print(
        f"[ПЛАН] пакетов к переносу: {len(groups)}; "
        f"лидов: {sum(row['entity_type'] == 'lead' for row in rows)}; "
        f"компаний: {sum(row['entity_type'] == 'company' for row in rows)}; "
        f"контактов: {sum(row['entity_type'] == 'contact' for row in rows)}; "
        f"всего сущностей: {len(rows)}; "
        f"пропущено лидов из пакетов Талдыкоргана: {len(package_skip_issues)}.",
        flush=True,
    )
    if all_linkage_issues:
        print(
            f"[СОКРАЩЁННЫЕ ПАКЕТЫ] лидов без полного учредителя/связки: "
            f"{len(all_linkage_issues)} — они НЕ пропущены, а идут как company+lead/lead-only.",
            flush=True,
        )

    if apply:
        final_wait_seconds = float(os.getenv("REASSIGN_FINAL_WAIT_SECONDS", "2"))
        owner_result = apply_owner_changes(client, rows)
        if final_wait_seconds > 0 and owner_result["planned"]:
            print(
                f"Ожидание {final_wait_seconds:g} сек. перед контрольным чтением владельцев...",
                flush=True,
            )
            time.sleep(final_wait_seconds)

        refreshed_companies, refreshed_contacts, refreshed_leads = load_crm(client)
        verify_result = verify_changes(rows, refreshed_companies, refreshed_contacts, refreshed_leads)

        by_type = {}
        for entity_type in ("lead", "company", "contact"):
            subset = [row for row in rows if row["entity_type"] == entity_type]
            ok = sum(row["action"] in {"updated", "already_matches"} for row in subset)
            errors = sum(row["action"] == "verify_error" for row in subset)
            by_type[entity_type] = (ok, len(subset), errors)
        print(
            f"[ИТОГ] владельцы подтверждены: "
            f"лиды {by_type['lead'][0]}/{by_type['lead'][1]}; "
            f"компании {by_type['company'][0]}/{by_type['company'][1]}; "
            f"контакты {by_type['contact'][0]}/{by_type['contact'][1]}; "
            f"расхождений: {verify_result['owner_errors']}.",
            flush=True,
        )

    # One artifact only: the complete plan/result CSV. Package skips are appended
    # as compact rows so there is no second skipped/report file.
    report_rows = list(rows)
    for issue in package_skip_issues:
        report_rows.append({
            "source_excluded_user_ids": ",".join(map(str, sorted(excluded_user_ids))),
            "founder_key": "",
            "package_lead_count": "", "package_company_count": "", "package_contact_count": "",
            "package_seed_old_owner_ids": "", "founder_contact_ids": "", "founder_names": issue.get("founder_name", ""),
            "company_ids": issue.get("company_id", ""), "company_titles": issue.get("company_title", ""),
            "company_bins": issue.get("company_bin", ""), "lead_company_id": issue.get("company_id", ""),
            "lead_company_title": issue.get("company_title", ""), "lead_company_bin": issue.get("company_bin", ""),
            "lead_contact_id": issue.get("contact_id", ""), "lead_founder_name": issue.get("founder_name", ""),
            "lead_date_create": "", "entity_type": "lead", "entity_id": issue.get("lead_id", ""),
            "title": issue.get("lead_title", ""), "old_owner_id": issue.get("old_owner_id", ""),
            "old_owner_name": "", "new_owner_id": "", "new_owner_name": "", "current_owner_id": issue.get("old_owner_id", ""),
            "old_status_id": issue.get("old_status_id", ""), "old_status_name": "", "old_status_semantic_id": "",
            "current_status_id": issue.get("old_status_id", ""), "new_status_id": "", "new_status_name": "",
            "assignment_reason": "", "warning": "", "action": issue.get("action", "skipped"), "error": issue.get("error", ""),
        })
    write_report(output_dir / "excluded_user_reassignment.csv", report_rows)

    update_errors = sum(row["action"] == "update_error" for row in rows)
    owner_verify_errors = sum(row["action"] == "verify_error" for row in rows)
    summary = {
        "mode_apply": int(apply),
        "excluded_users": len(excluded_user_ids),
        "excluded_user_ids": sorted(excluded_user_ids),
        "short_packages": len(all_linkage_issues),
        "skipped_taldykorgan": sum(
            "skipped_taldykorgan" in str(issue["action"]) for issue in package_skip_issues
        ),
        "skipped_package_leads_total": len(package_skip_issues),
        "packages": len(groups),
        "leads": sum(row["entity_type"] == "lead" for row in rows),
        "companies": sum(row["entity_type"] == "company" for row in rows),
        "contacts": sum(row["entity_type"] == "contact" for row in rows),
        "planned_total": len(rows),
        "owner_updates_planned": owner_result["planned"],
        "owner_updates_ok": owner_result["updated"],
        "owner_update_errors": owner_result["errors"],
        "owner_verified": verify_result["owner_ok"],
        "owner_verify_errors": owner_verify_errors,
    }
    for rop_id in target_rop_ids:
        summary[f"rop_{rop_id}_packages"] = sum(
            group.target_owner_id == rop_id for group in groups
        )
        summary[f"rop_{rop_id}_leads"] = sum(
            len(group.lead_ids) for group in groups if group.target_owner_id == rop_id
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
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
            "Переназначить ответственных у лидов исключённых пользователей и связанного "
            "пакета (компания/контакт) между РОП 72 и 73; статусы лидов не изменяются."
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
    return 1 if summary["owner_update_errors"] or summary["owner_verify_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
