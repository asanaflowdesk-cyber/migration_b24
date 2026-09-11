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
DEFAULT_PROTECTED_OWNER_IDS = {13, 16, 18, 38, 40, 58}
DEFAULT_PROTECTED_LEAD_IDS = {401}


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


def parse_optional_ids(value: str, default: set[int] | None = None) -> set[int]:
    tokens = [token.strip() for token in re.split(r"[,;\s]+", value or "") if token.strip()]
    if not tokens:
        return set(default or set())
    invalid = [token for token in tokens if not token.isdigit() or int(token) <= 0]
    if invalid:
        raise ReassignmentError("Некорректные ID: " + ", ".join(invalid))
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
            "warning_missing_company_lead_only_package"
            if any("COMPANY_ID" in error or "компания ID=" in error for error in errors)
            else "warning_missing_founder_short_package"
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
    owner_department_names: dict[int, set[str]] | None = None,
    target_owner_ids: set[int] | None = None,
    *,
    skip_any_taldykorgan: bool = True,
) -> tuple[list[OwnerGroup], list[dict[str, Any]]]:
    """Keep only packages that are safe to redistribute.

    Residual runs are intentionally idempotent: entities already moved to one of
    the target ROPs do not block the package.  Instead the unfinished package is
    pinned to that same ROP so the package cannot be split between 72 and 73.

    A package is skipped when it contains a real owner outside both the exclusion
    list and the target ROPs, when both target ROPs are already present in the same
    package, or (for the current one-off cleanup) when it is linked to a
    Taldykorgan owner.
    """
    companies_list = list(companies)
    contacts_list = list(contacts)
    leads_list = list(leads)
    owner_department_names = owner_department_names or {}
    target_owner_ids = set(target_owner_ids or set())
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
        ]

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

        existing_target_ids = sorted(package_owner_ids & target_owner_ids)
        protected_owner_ids = sorted(
            package_owner_ids - excluded_user_ids - target_owner_ids
        )

        taldyk_owner_ids = sorted(
            owner_id
            for owner_id in package_owner_ids
            if _owner_branch_kind(owner_id, owner_department_names) == "taldykorgan"
        )
        other_branch_owner_ids = sorted(
            owner_id
            for owner_id in package_owner_ids
            if _owner_branch_kind(owner_id, owner_department_names) == "other"
        )
        has_taldykorgan = bool(taldyk_owner_ids)
        is_multibranch_taldykorgan = bool(taldyk_owner_ids and other_branch_owner_ids)

        reasons: list[str] = []
        actions: list[str] = []
        if protected_owner_ids:
            actions.append("skipped_existing_owner")
            reasons.append(
                "пакет закреплён за защищённым пользователем вне исключённых и целевых РОПов: "
                + ",".join(map(str, protected_owner_ids))
            )
        if len(existing_target_ids) > 1:
            actions.append("skipped_conflicting_target_rops")
            reasons.append(
                "пакет уже разделён между целевыми РОПами: "
                + ",".join(map(str, existing_target_ids))
            )
        if (skip_any_taldykorgan and has_taldykorgan) or (
            not skip_any_taldykorgan and is_multibranch_taldykorgan
        ):
            actions.append(
                "skipped_taldykorgan"
                if skip_any_taldykorgan
                else "skipped_multibranch_taldykorgan"
            )
            branch_details = []
            for owner_id in sorted(set(taldyk_owner_ids + other_branch_owner_ids)):
                names = sorted(owner_department_names.get(owner_id, set()))
                branch_details.append(
                    f"{owner_id}:" + "/".join(names) if names else str(owner_id)
                )
            reasons.append(
                (
                    "пакет связан с ответственным из Талдыкоргана"
                    if skip_any_taldykorgan
                    else "мультифилиальный пакет по ответственным: Талдыкорган + другой филиал"
                )
                + (f" ({' | '.join(branch_details)})" if branch_details else "")
            )

        if not reasons:
            if len(existing_target_ids) == 1:
                group.target_owner_id = existing_target_ids[0]
                group.assignment_reason = "continue_existing_target_package"
            eligible.append(group)
            continue

        action = "+".join(actions)
        error = "; ".join(reasons)
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
                    "action": action,
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

    # Packages that already contain exactly one target ROP are pinned to that ROP.
    # Only completely untouched packages participate in balancing.
    planned_leads = {user_id: 0 for user_id in rops}
    for group in groups:
        if group.target_owner_id is not None:
            if group.target_owner_id not in rops:
                raise ReassignmentError(
                    f"Пакет {group.key} закреплён за недопустимым целевым ID={group.target_owner_id}"
                )
            planned_leads[group.target_owner_id] += len(group.lead_ids)

    for group in sorted(groups, key=lambda item: (-len(item.lead_ids), item.key)):
        if group.target_owner_id is not None:
            continue
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
                # Leads, companies and contacts already assigned to somebody
                # outside the manually entered exclusion list are protected.
                if old_owner not in excluded_ids:
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
                needs_change = old_owner != group.target_owner_id or (
                    entity_type == "lead" and old_status != NEW_STATUS_ID
                )
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
                        "new_status_id": NEW_STATUS_ID if entity_type == "lead" else "",
                        "new_status_name": (
                            status_names.get(NEW_STATUS_ID, "Новый лид")
                            if entity_type == "lead" else ""
                        ),
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


def prepare_lead_statuses(client: BitrixClient, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Move leads to NEW before ownership reassignment.

    Stage robots may react to STATUS_ID changes and change the responsible user.  Therefore
    the status transition is deliberately completed first; ownership is written only after
    the robot stabilization pause.
    """
    lead_rows = [
        row for row in rows
        if row["action"] == "pending"
        and row["entity_type"] == "lead"
        and str(row.get("old_status_id") or "") != NEW_STATUS_ID
    ]
    total = len(lead_rows)
    ok = 0
    errors = 0
    if total:
        print(
            f"Подготовка лидов: перевести в статус NEW до переназначения владельца — {total} шт.",
            flush=True,
        )
    for index, row in enumerate(lead_rows, start=1):
        try:
            client.update_lead(str(row["entity_id"]), {"STATUS_ID": NEW_STATUS_ID})
            ok += 1
        except Exception as exc:  # noqa: BLE001
            row["action"] = "update_error"
            row["error"] = f"Не удалось перевести лид в NEW: {exc}"
            errors += 1
        _print_progress("СТАТУС NEW", index, total, ok=ok, errors=errors)
    return {"planned": total, "updated": ok, "errors": errors}


def apply_owner_changes(client: BitrixClient, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Write the complete target state in one pass.

    For leads the owner and NEW stage are written in the same crm.lead.update call.
    That prevents the old two-step repair from leaving a lead with the right owner
    but a stage already changed back by a robot between calls.
    """
    update_methods = {
        "contact": client.update_contact,
        "company": client.update_company,
        "lead": client.update_lead,
    }
    eligible = [row for row in rows if row["action"] in {"pending", "verify_error"}]
    total = len(eligible)
    ok = 0
    errors = 0
    if total:
        print(f"Перенос пакетов: всего {total} сущностей.", flush=True)
    for index, row in enumerate(eligible, start=1):
        fields: dict[str, Any] = {"ASSIGNED_BY_ID": int(row["new_owner_id"])}
        if row["entity_type"] == "lead":
            fields["STATUS_ID"] = NEW_STATUS_ID
        try:
            update_methods[row["entity_type"]](str(row["entity_id"]), fields)
            row["action"] = "updated"
            row["error"] = ""
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
        "source_excluded_user_ids", "founder_key", "package_lead_count",
        "package_company_count", "package_contact_count", "package_seed_old_owner_ids",
        "founder_contact_ids", "founder_names", "company_ids", "company_titles",
        "company_bins", "lead_company_id", "lead_company_title", "lead_company_bin",
        "lead_contact_id", "lead_founder_name", "lead_date_create", "entity_type",
        "entity_id", "title",
        "old_owner_id", "old_owner_name", "new_owner_id", "new_owner_name",
        "old_status_id", "old_status_name", "old_status_semantic_id",
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
    protected_owner_ids = parse_optional_ids(
        os.getenv("REASSIGN_PROTECTED_OWNER_IDS", "13,16,18,38,40,58"),
        DEFAULT_PROTECTED_OWNER_IDS,
    )
    protected_lead_ids = parse_optional_ids(
        os.getenv("REASSIGN_PROTECTED_LEAD_IDS", "401"),
        DEFAULT_PROTECTED_LEAD_IDS,
    )
    protected_overlap = sorted(excluded_user_ids & protected_owner_ids)
    if protected_overlap:
        print(
            "Защищённые пользователи исключены из охвата: "
            + ", ".join(map(str, protected_overlap)),
            flush=True,
        )
        excluded_user_ids = excluded_user_ids - protected_owner_ids
    if not excluded_user_ids:
        raise ReassignmentError("После применения защиты не осталось пользователей для переноса")

    protected_seed_issues: list[dict[str, Any]] = []
    for lead in leads:
        lead_id = _entity_id(lead)
        owner_id = normalized_id(lead.get("ASSIGNED_BY_ID"))
        if lead_id not in protected_lead_ids or owner_id not in excluded_user_ids:
            continue
        protected_seed_issues.append({
            "lead_id": lead_id or "",
            "lead_title": str(lead.get("TITLE") or ""),
            "old_owner_id": owner_id or "",
            "old_status_id": str(lead.get("STATUS_ID") or ""),
            "company_id": normalized_id(lead.get("COMPANY_ID")) or "",
            "company_title": "",
            "company_bin": "",
            "contact_id": normalized_id(lead.get("CONTACT_ID")) or "",
            "founder_name": "",
            "action": "skipped_protected_lead",
            "error": "лид явно защищён от переноса",
        })

    seed_lead_ids = {
        int(lead_id)
        for lead in leads
        if normalized_id(lead.get("ASSIGNED_BY_ID")) in excluded_user_ids
        and (lead_id := _entity_id(lead)) is not None
        and lead_id not in protected_lead_ids
    }
    if not seed_lead_ids:
        if protected_seed_issues:
            write_linkage_report(
                output_dir / "excluded_user_reassignment_skipped.csv", protected_seed_issues
            )
        summary = {
            "mode_apply": int(apply),
            "excluded_users": len(excluded_user_ids),
            "excluded_user_ids": sorted(excluded_user_ids),
            "protected_owner_ids": sorted(protected_owner_ids),
            "protected_lead_ids": sorted(protected_lead_ids),
            "linkage_validation_errors": 0,
            "linkage_warnings_total": 0,
            "short_packages_missing_founder_or_company": 0,
            "skipped_protected_lead": len(protected_seed_issues),
            "skipped_existing_owner": 0,
            "skipped_taldykorgan": 0,
            "skipped_conflicting_target_rops": 0,
            "skipped_package_leads_total": 0,
            "skipped_total": len(protected_seed_issues),
            "skipped_lead_ids": sorted({int(issue["lead_id"]) for issue in protected_seed_issues}),
            "founders": 0,
            "leads": 0,
            "companies": 0,
            "contacts": 0,
            "planned_total": 0,
            "lead_status_updates_planned": 0,
            "lead_status_updates_ok": 0,
            "lead_status_update_errors": 0,
            "owner_updates_planned": 0,
            "owner_updates_ok": 0,
            "owner_update_errors": 0,
            "repair_updates_planned": 0,
            "repair_updates_ok": 0,
            "repair_update_errors": 0,
            "pending": 0,
            "updated": 0,
            "verified_total": 0,
            "already_matches": 0,
            "update_errors": 0,
            "verify_errors": 0,
            "rop_72_founders": 0,
            "rop_72_leads": 0,
            "rop_73_founders": 0,
            "rop_73_leads": 0,
        }
        (output_dir / "excluded_user_reassignment_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("Остатков для автоматического переноса нет.", flush=True)
        return summary

    # Missing founder is no longer a stop condition.  The grouping code falls back
    # to company:<ID>, so company + all selected leads remain one shortened package.
    # If even COMPANY_ID is absent, the lead becomes a one-lead package instead of
    # aborting the whole run.  Linkage problems are retained only as warnings.
    linkage_warnings = collect_linkage_issues(companies, contacts, leads, seed_lead_ids)
    if linkage_warnings:
        write_linkage_report(
            output_dir / "excluded_user_reassignment_warnings.csv", linkage_warnings
        )

    groups = build_owner_groups(
        companies,
        contacts,
        leads,
        excluded_user_ids,
        skip_lead_ids=protected_lead_ids,
    )
    package_owner_ids = collect_group_owner_ids(groups, companies, contacts, leads)
    owner_department_names = load_user_department_names(client, package_owner_ids)
    groups, package_skip_issues = split_reassignment_groups(
        groups,
        companies,
        contacts,
        leads,
        excluded_user_ids,
        owner_department_names,
        set(target_rop_ids),
        skip_any_taldykorgan=True,
    )
    relevant_lead_ids = set().union(*(group.lead_ids for group in groups)) if groups else set()
    skipped_issues = protected_seed_issues + package_skip_issues
    if skipped_issues:
        write_linkage_report(
            output_dir / "excluded_user_reassignment_skipped.csv", skipped_issues
        )
    assign_targets(groups, target_rop_ids)
    relevant_owner_ids = set(target_rop_ids) | set(excluded_user_ids)
    entity_ids = {
        "company": set().union(*(group.company_ids for group in groups)) if groups else set(),
        "contact": set().union(*(group.contact_ids for group in groups)) if groups else set(),
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
    status_result = {"planned": 0, "updated": 0, "errors": 0}
    owner_result = {"planned": 0, "updated": 0, "errors": 0}
    repair_result = {"planned": 0, "updated": 0, "errors": 0}
    if apply:
        stabilize_seconds = float(os.getenv("REASSIGN_STABILIZE_SECONDS", "12"))
        final_wait_seconds = float(os.getenv("REASSIGN_FINAL_WAIT_SECONDS", "5"))

        # One atomic target-state write per lead: owner + NEW together.
        owner_result = apply_owner_changes(client, rows)
        if stabilize_seconds > 0 and owner_result["planned"]:
            print(
                f"Ожидание {stabilize_seconds:g} сек. — даём роботам Bitrix24 отработать...",
                flush=True,
            )
            time.sleep(stabilize_seconds)

        refreshed_companies, refreshed_contacts, refreshed_leads = load_crm(client)
        verify_changes(rows, refreshed_companies, refreshed_contacts, refreshed_leads)
        first_verify_errors = sum(row["action"] == "verify_error" for row in rows)
        if first_verify_errors:
            print(
                f"[ПРОВЕРКА 1] роботы изменили {first_verify_errors} сущностей; "
                "повторно фиксируем целевой owner + NEW.",
                flush=True,
            )
            repair_result = apply_owner_changes(client, rows)
            if final_wait_seconds > 0 and repair_result["planned"]:
                print(
                    f"Ожидание {final_wait_seconds:g} сек. после повторной фиксации...",
                    flush=True,
                )
                time.sleep(final_wait_seconds)
            refreshed_companies, refreshed_contacts, refreshed_leads = load_crm(client)
            verify_changes(rows, refreshed_companies, refreshed_contacts, refreshed_leads)

        verify_errors_now = sum(row["action"] == "verify_error" for row in rows)
        verified_now = sum(row["action"] == "updated" for row in rows)
        verify_total = verified_now + verify_errors_now
        if verify_total:
            print(
                f"[ПРОВЕРКА] подтверждено {verified_now}/{verify_total}; "
                f"расхождений после повторной фиксации: {verify_errors_now}",
                flush=True,
            )

    write_report(output_dir / "excluded_user_reassignment.csv", rows)
    update_errors = sum(row["action"] == "update_error" for row in rows)
    verify_errors = sum(row["action"] == "verify_error" for row in rows)
    verified_total = sum(row["action"] == "updated" for row in rows)
    summary = {
        "mode_apply": int(apply),
        "excluded_users": len(excluded_user_ids),
        "excluded_user_ids": sorted(excluded_user_ids),
        "protected_owner_ids": sorted(protected_owner_ids),
        "protected_lead_ids": sorted(protected_lead_ids),
        "linkage_validation_errors": 0,
        "linkage_warnings_total": len(linkage_warnings),
        "short_packages_missing_founder_or_company": len(linkage_warnings),
        "skipped_protected_lead": len(protected_seed_issues),
        "skipped_existing_owner": sum(
            "skipped_existing_owner" in str(issue["action"]) for issue in package_skip_issues
        ),
        "skipped_taldykorgan": sum(
            "skipped_taldykorgan" in str(issue["action"])
            for issue in package_skip_issues
        ),
        "skipped_conflicting_target_rops": sum(
            "skipped_conflicting_target_rops" in str(issue["action"])
            for issue in package_skip_issues
        ),
        "skipped_package_leads_total": len(package_skip_issues),
        "skipped_total": len(skipped_issues),
        "skipped_lead_ids": sorted({int(issue["lead_id"]) for issue in skipped_issues}),
        "founders": len(groups),
        "leads": sum(row["entity_type"] == "lead" for row in rows),
        "companies": sum(row["entity_type"] == "company" for row in rows),
        "contacts": sum(row["entity_type"] == "contact" for row in rows),
        "planned_total": len(rows),
        "lead_status_updates_planned": status_result["planned"],
        "lead_status_updates_ok": status_result["updated"],
        "lead_status_update_errors": status_result["errors"],
        "owner_updates_planned": owner_result["planned"],
        "owner_updates_ok": owner_result["updated"],
        "owner_update_errors": owner_result["errors"],
        "repair_updates_planned": repair_result["planned"],
        "repair_updates_ok": repair_result["updated"],
        "repair_update_errors": repair_result["errors"],
        "pending": sum(row["action"] == "pending" for row in rows),
        "updated": verified_total,
        "verified_total": verified_total,
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
    if apply:
        verification_total = verified_total + verify_errors
        print(
            f"[ИТОГ] API принял переназначение владельца: "
            f"{owner_result['updated']}/{owner_result['planned']}; "
            f"контроль подтверждён: {verified_total}/{verification_total}; "
            f"ошибок записи: {update_errors}; расхождений проверки: {verify_errors}",
            flush=True,
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
