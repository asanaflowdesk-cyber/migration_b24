from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from eqazyna_bitrix.bitrix_client import BitrixClient, BitrixError
from eqazyna_bitrix.settings import Settings


class ReassignmentError(RuntimeError):
    pass


DEFAULT_TARGET_ROPS = (72, 73)
DEFAULT_PROTECTED_LEAD_IDS = {401}
DEFAULT_PROTECTED_TALDYK_MANAGER_IDS = {16, 18, 38}


def normalized_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None
    result = int(raw)
    return result if result > 0 else None


def parse_id_set(value: str, *, label: str) -> set[int]:
    tokens = [x for x in re.split(r"[,;\s]+", str(value or "").strip()) if x]
    if not tokens:
        raise ReassignmentError(f"Не указаны {label}")
    bad = [x for x in tokens if not x.isdigit() or int(x) <= 0]
    if bad:
        raise ReassignmentError(f"Некорректные {label}: {', '.join(bad)}")
    return {int(x) for x in tokens}


def parse_optional_id_set(value: str) -> set[int]:
    if not str(value or "").strip():
        return set()
    return parse_id_set(value, label="ID")


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold().replace("ё", "е"))


def founder_key(contact: dict[str, Any]) -> str | None:
    last = _norm_text(contact.get("LAST_NAME"))
    first = _norm_text(contact.get("NAME"))
    second = _norm_text(contact.get("SECOND_NAME"))
    if not last or not first:
        return None
    return f"fio:{last}|{first}|{second}"


def contact_name(contact: dict[str, Any]) -> str:
    return " ".join(
        str(contact.get(k) or "").strip()
        for k in ("LAST_NAME", "NAME", "SECOND_NAME")
        if str(contact.get(k) or "").strip()
    )


def entity_id(row: dict[str, Any]) -> int | None:
    return normalized_id(row.get("ID"))


def is_founder_contact(contact: dict[str, Any], lead_contact_ids: set[int]) -> bool:
    cid = entity_id(contact)
    if cid is None or founder_key(contact) is None:
        return False
    if cid in lead_contact_ids:
        return True
    post = _norm_text(contact.get("POST"))
    comments = str(contact.get("COMMENTS") or "")
    return "руковод" in post or "EQAZYNA_DIRECTOR:" in comments


@dataclass(slots=True)
class Package:
    key: str
    lead_ids: set[int] = field(default_factory=set)  # only leads owned by excluded users
    company_ids: set[int] = field(default_factory=set)
    contact_ids: set[int] = field(default_factory=set)
    source_owner_ids: set[int] = field(default_factory=set)
    warning: str = ""
    target_owner_id: int | None = None
    target_reason: str = ""
    context_owner_ids: set[int] = field(default_factory=set)
    context_lead_ids: set[int] = field(default_factory=set)
    # Every CRM lead linked to this package by company or founder contact.
    # Values are current ASSIGNED_BY_ID (or None when Bitrix returned no owner).
    # This is intentionally separate from entity owners: the protection rule is
    # based on the MAJORITY OF LINKED LEADS, not on company/contact owners.
    context_lead_owner_ids: dict[int, int | None] = field(default_factory=dict)


@dataclass(slots=True)
class PackageDecision:
    package: Package
    skip_reason: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.skip_reason)


class DisjointSet:
    def __init__(self, values: Iterable[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        low, high = sorted((a, b))
        self.parent[high] = low


def build_packages(
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    excluded_user_ids: set[int],
) -> list[Package]:
    """Build indivisible packages from excluded-user leads.

    Full package: founder FIO + every company linked to that founder + founder contacts
    + excluded-user leads in that package.

    If founder is absent, company + excluded-user leads is still a package.
    If company is absent too, the lead itself is a minimal package.
    Non-excluded leads are never added to ``lead_ids`` and therefore are never changed.
    """
    companies = list(companies)
    contacts = list(contacts)
    leads = list(leads)
    company_ids = {eid for row in companies if (eid := entity_id(row)) is not None}
    contact_by_id = {eid: row for row in contacts if (eid := entity_id(row)) is not None}
    lead_contact_ids = {
        cid for row in leads if (cid := normalized_id(row.get("CONTACT_ID"))) is not None
    }
    founder_contacts = {
        cid: row
        for cid, row in contact_by_id.items()
        if is_founder_contact(row, lead_contact_ids)
    }

    founder_to_contacts: dict[str, set[int]] = defaultdict(set)
    founder_to_companies: dict[str, set[int]] = defaultdict(set)
    company_to_founders: dict[int, set[str]] = defaultdict(set)

    for cid, contact in founder_contacts.items():
        fkey = founder_key(contact)
        if not fkey:
            continue
        founder_to_contacts[fkey].add(cid)
        company_id = normalized_id(contact.get("COMPANY_ID"))
        if company_id in company_ids:
            founder_to_companies[fkey].add(int(company_id))
            company_to_founders[int(company_id)].add(fkey)

    # Lead links are authoritative too: CONTACT_ID may point at a founder whose
    # primary COMPANY_ID is another company.
    for lead in leads:
        company_id = normalized_id(lead.get("COMPANY_ID"))
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        contact = founder_contacts.get(contact_id or -1)
        fkey = founder_key(contact or {})
        if not fkey:
            continue
        founder_to_contacts[fkey].add(int(contact_id))
        if company_id in company_ids:
            founder_to_companies[fkey].add(int(company_id))
            company_to_founders[int(company_id)].add(fkey)

    dsu = DisjointSet(founder_to_contacts.keys())
    for founders in company_to_founders.values():
        ordered = sorted(founders)
        for other in ordered[1:]:
            dsu.union(ordered[0], other)

    members_by_root: dict[str, set[str]] = defaultdict(set)
    for fkey in founder_to_contacts:
        members_by_root[dsu.find(fkey)].add(fkey)

    canonical_by_founder: dict[str, str] = {}
    canonical_contacts: dict[str, set[int]] = defaultdict(set)
    canonical_companies: dict[str, set[int]] = defaultdict(set)
    for members in members_by_root.values():
        ordered = sorted(members)
        canonical = ordered[0] if len(ordered) == 1 else (
            "founders:" + " & ".join(item.removeprefix("fio:") for item in ordered)
        )
        for member in ordered:
            canonical_by_founder[member] = canonical
            canonical_contacts[canonical].update(founder_to_contacts[member])
            canonical_companies[canonical].update(founder_to_companies[member])

    company_to_canonical: dict[int, set[str]] = defaultdict(set)
    for company_id, founders in company_to_founders.items():
        for fkey in founders:
            canonical = canonical_by_founder.get(fkey)
            if canonical:
                company_to_canonical[company_id].add(canonical)

    seed_leads = [
        lead for lead in leads
        if normalized_id(lead.get("ASSIGNED_BY_ID")) in excluded_user_ids
    ]
    if not seed_leads:
        raise ReassignmentError("У исключённых пользователей не найдено ни одного лида")

    packages: dict[str, Package] = {}
    for lead in seed_leads:
        lead_id = entity_id(lead)
        if lead_id is None:
            continue
        company_id = normalized_id(lead.get("COMPANY_ID"))
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        contact = founder_contacts.get(contact_id or -1)
        raw_fkey = founder_key(contact or {})
        key = canonical_by_founder.get(raw_fkey or "")
        warning = ""

        if key is None and company_id is not None:
            candidates = company_to_canonical.get(company_id, set())
            if len(candidates) == 1:
                key = next(iter(candidates))
        if key is None and company_id is not None:
            key = f"company:{company_id}"
            warning = "Нет учредителя: сокращённый пакет компания + лиды"
        if key is None:
            key = f"lead:{lead_id}"
            warning = "Нет учредителя и компании: минимальный пакет из одного лида"

        package = packages.setdefault(key, Package(key=key, warning=warning))
        package.lead_ids.add(lead_id)
        old_owner = normalized_id(lead.get("ASSIGNED_BY_ID"))
        if old_owner:
            package.source_owner_ids.add(old_owner)
        if company_id in company_ids:
            package.company_ids.add(int(company_id))
        if contact_id in founder_contacts:
            package.contact_ids.add(int(contact_id))

    # Expand founder packages to all linked companies/founder contacts.
    for package in packages.values():
        if package.key in canonical_contacts:
            package.contact_ids.update(canonical_contacts[package.key])
            package.company_ids.update(canonical_companies[package.key])

    # One CRM entity must never belong to two packages in the same run.
    seen: dict[tuple[str, int], str] = {}
    for package in packages.values():
        for kind, ids in (
            ("company", package.company_ids),
            ("contact", package.contact_ids),
            ("lead", package.lead_ids),
        ):
            for eid in ids:
                prior = seen.get((kind, eid))
                if prior and prior != package.key:
                    raise ReassignmentError(
                        f"{kind} ID={eid} попал сразу в два пакета: {prior} и {package.key}"
                    )
                seen[(kind, eid)] = package.key

    return sorted(packages.values(), key=lambda p: p.key)


def add_package_context(
    packages: Iterable[Package],
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
) -> None:
    companies = list(companies)
    contacts = list(contacts)
    leads = list(leads)
    company_by_id = {eid: row for row in companies if (eid := entity_id(row)) is not None}
    contact_by_id = {eid: row for row in contacts if (eid := entity_id(row)) is not None}

    for package in packages:
        owners: set[int] = set()
        for cid in package.company_ids:
            owner = normalized_id((company_by_id.get(cid) or {}).get("ASSIGNED_BY_ID"))
            if owner:
                owners.add(owner)
        for cid in package.contact_ids:
            owner = normalized_id((contact_by_id.get(cid) or {}).get("ASSIGNED_BY_ID"))
            if owner:
                owners.add(owner)

        for lead in leads:
            lid = entity_id(lead)
            if lid is None:
                continue
            company_id = normalized_id(lead.get("COMPANY_ID"))
            contact_id = normalized_id(lead.get("CONTACT_ID"))
            if lid in package.lead_ids or company_id in package.company_ids or contact_id in package.contact_ids:
                package.context_lead_ids.add(lid)
                owner = normalized_id(lead.get("ASSIGNED_BY_ID"))
                package.context_lead_owner_ids[lid] = owner
                if owner:
                    owners.add(owner)
        package.context_owner_ids = owners


def protected_majority_counts(
    package: Package,
    protected_manager_ids: set[int],
) -> tuple[int, int, int]:
    """Return (protected, other, total) for ALL leads linked to the package.

    The package is linked through its company and/or founder contact.  Company
    and contact owners are deliberately ignored here: only lead ownership votes.
    Managers 16/18/38 are one protected group, so their votes are summed.
    A missing lead owner counts as ``other`` because that lead is not assigned to
    16/18/38.
    """
    total = len(package.context_lead_ids)
    protected = sum(
        1
        for lid in package.context_lead_ids
        if package.context_lead_owner_ids.get(lid) in protected_manager_ids
    )
    other = total - protected
    return protected, other, total


def package_has_nonprotected_majority(
    package: Package,
    protected_manager_ids: set[int],
) -> bool:
    """True when leads outside 16/18/38 are a strict majority.

    Such a package goes to ROP 72/73. Otherwise the remaining excluded-user
    entities are consolidated under whichever of 16/18/38 owns the most linked
    leads.
    """
    protected, other, total = protected_majority_counts(package, protected_manager_ids)
    return total > 0 and other > protected


def protected_manager_vote_counts(
    package: Package,
    protected_manager_ids: set[int],
) -> dict[int, int]:
    """Count linked leads separately for each protected manager."""
    return {
        manager_id: sum(
            1
            for owner in package.context_lead_owner_ids.values()
            if owner == manager_id
        )
        for manager_id in sorted(protected_manager_ids)
    }


def select_protected_manager_target(
    package: Package,
    protected_manager_ids: set[int],
) -> tuple[int, dict[int, int], bool]:
    """Pick the protected manager with the most linked leads.

    A package must not remain hanging. If two or more protected managers have
    exactly the same maximum count, use the lowest manager ID as a deterministic
    tie-breaker. The third return value flags that a tie-break was required.
    """
    counts = protected_manager_vote_counts(package, protected_manager_ids)
    if not counts:
        raise ReassignmentError("Не заданы менеджеры 16/18/38 для остаточного распределения")
    max_count = max(counts.values())
    if max_count <= 0:
        raise ReassignmentError(
            f"Пакет {package.key}: нет ни одного связанного лида у менеджеров "
            f"{','.join(map(str, sorted(protected_manager_ids)))}"
        )
    winners = [manager_id for manager_id, count in counts.items() if count == max_count]
    return min(winners), counts, len(winners) > 1


def assign_targets(
    packages: list[Package],
    target_rop_ids: tuple[int, int],
) -> None:
    if len(target_rop_ids) != 2 or target_rop_ids[0] == target_rop_ids[1]:
        raise ReassignmentError("Для автоматического распределения нужны ровно два разных РОП")

    loads = {rop: 0 for rop in target_rop_ids}
    for package in packages:
        if package.target_owner_id in loads:
            loads[int(package.target_owner_id)] += len(package.lead_ids)

    unassigned = [p for p in packages if p.target_owner_id is None]
    for package in sorted(unassigned, key=lambda p: (-len(p.lead_ids), p.key)):
        target = min(target_rop_ids, key=lambda rop: (loads[rop], rop))
        package.target_owner_id = target
        package.target_reason = "balanced_between_rops_72_73"
        loads[target] += len(package.lead_ids)



def plan_packages(
    packages: list[Package],
    protected_manager_ids: set[int],
    protected_lead_ids: set[int],
    target_rop_ids: tuple[int, int],
) -> tuple[list[Package], list[PackageDecision]]:
    """Choose one target for every package except explicitly protected lead 401.

    Rule:
    * if leads outside 16/18/38 are a strict majority -> whole transferable package
      goes to ROP 72/73 and is balanced by transferable lead count;
    * otherwise the package is no longer left hanging: all remaining excluded-user
      leads plus package company/contact are consolidated under whichever of
      16/18/38 currently owns the most linked leads;
    * exact tie between protected managers is resolved deterministically by the
      lowest manager ID so the package cannot remain unresolved;
    * explicitly protected lead 401 still blocks the whole package.

    Non-excluded context leads are votes only and are never reassigned.
    """
    eligible: list[Package] = []
    skipped: list[PackageDecision] = []

    for package in packages:
        protected = package.lead_ids & protected_lead_ids
        if protected:
            skipped.append(
                PackageDecision(
                    package,
                    f"protected_lead:{','.join(map(str, sorted(protected)))}",
                )
            )
            continue

        protected_count, other_count, total_count = protected_majority_counts(
            package, protected_manager_ids
        )

        if package_has_nonprotected_majority(package, protected_manager_ids):
            package.target_owner_id = None
            package.target_reason = (
                "nonprotected_majority_to_rops:"
                f"protected={protected_count};other={other_count};total={total_count}"
            )
        else:
            target, counts, tie_break = select_protected_manager_target(
                package, protected_manager_ids
            )
            votes = ",".join(f"{manager_id}:{counts[manager_id]}" for manager_id in sorted(counts))
            package.target_owner_id = target
            package.target_reason = (
                f"protected_majority_to_{target}:votes={votes};"
                f"other={other_count};total={total_count};"
                f"tie_break={'yes' if tie_break else 'no'}"
            )
        eligible.append(package)

    # assign_targets only touches packages whose target is still None, i.e. the
    # strict non-protected-majority packages that must be balanced across the ROPs.
    assign_targets(eligible, target_rop_ids)
    return eligible, skipped


def load_crm(client: BitrixClient) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    companies = client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "ORIGIN_ID"],
        },
    )
    contacts = client.list_all(
        "crm.contact.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": [
                "ID", "LAST_NAME", "NAME", "SECOND_NAME", "POST", "COMMENTS",
                "COMPANY_ID", "ASSIGNED_BY_ID",
            ],
        },
    )
    leads = client.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": [
                "ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID",
                "STATUS_ID", "DATE_CREATE",
            ],
        },
    )
    return companies, contacts, leads


def load_users(client: BitrixClient, user_ids: set[int]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for user_id in sorted(user_ids):
        rows = client.list_all("user.get", {"FILTER": {"ID": user_id}})
        row = next((r for r in rows if normalized_id(r.get("ID")) == user_id), None)
        if row:
            result[user_id] = row
    return result


def user_name(user: dict[str, Any] | None, user_id: int) -> str:
    if not user:
        return f"ID {user_id}"
    name = " ".join(
        str(user.get(k) or "").strip()
        for k in ("LAST_NAME", "NAME", "SECOND_NAME")
        if str(user.get(k) or "").strip()
    )
    return name or f"ID {user_id}"



def validate_target_users(
    users: dict[int, dict[str, Any]], target_rop_ids: tuple[int, int]
) -> None:
    for user_id in target_rop_ids:
        user = users.get(user_id)
        if not user:
            raise ReassignmentError(f"Целевой РОП ID={user_id} не найден в Bitrix24")
        active = str(user.get("ACTIVE") or "Y").upper()
        if active not in {"Y", "1", "TRUE"}:
            raise ReassignmentError(f"Целевой РОП ID={user_id} не активен")


def build_rows(
    packages: list[Package],
    skipped: list[PackageDecision],
    companies: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    excluded_user_ids: set[int],
    user_names: dict[int, str],
) -> list[dict[str, Any]]:
    company_by_id = {eid: row for row in companies if (eid := entity_id(row)) is not None}
    contact_by_id = {eid: row for row in contacts if (eid := entity_id(row)) is not None}
    lead_by_id = {eid: row for row in leads if (eid := entity_id(row)) is not None}
    rows: list[dict[str, Any]] = []

    def package_meta(package: Package) -> dict[str, Any]:
        founders = sorted(
            {contact_name(contact_by_id[cid]) for cid in package.contact_ids if cid in contact_by_id}
        )
        companies_names = sorted(
            str(company_by_id[cid].get("TITLE") or "") for cid in package.company_ids if cid in company_by_id
        )
        return {
            "package_key": package.key,
            "package_leads": len(package.lead_ids),
            "package_companies": len(package.company_ids),
            "package_contacts": len(package.contact_ids),
            "source_owner_ids": ",".join(map(str, sorted(package.source_owner_ids))),
            "context_owner_ids": ",".join(map(str, sorted(package.context_owner_ids))),
            "context_leads_total": len(package.context_lead_ids),
            "protected_16_18_38_leads": sum(
                1 for owner in package.context_lead_owner_ids.values()
                if owner in DEFAULT_PROTECTED_TALDYK_MANAGER_IDS
            ),
            "other_leads": sum(
                1 for owner in package.context_lead_owner_ids.values()
                if owner not in DEFAULT_PROTECTED_TALDYK_MANAGER_IDS
            ),
            "company_ids": ",".join(map(str, sorted(package.company_ids))),
            "company_titles": " | ".join(companies_names),
            "founder_contact_ids": ",".join(map(str, sorted(package.contact_ids))),
            "founder_names": " | ".join(founders),
            "package_warning": package.warning,
        }

    for package in packages:
        if package.target_owner_id is None:
            raise ReassignmentError(f"Для пакета {package.key} не выбран целевой владелец")
        target = package.target_owner_id
        meta = package_meta(package)
        for kind, ids, source in (
            ("company", package.company_ids, company_by_id),
            ("contact", package.contact_ids, contact_by_id),
            ("lead", package.lead_ids, lead_by_id),
        ):
            for eid in sorted(ids):
                record = source.get(eid)
                if not record:
                    continue
                old_owner = normalized_id(record.get("ASSIGNED_BY_ID"))
                # Leads outside the excluded set are context only and never appear here;
                # company/contact package entities are aligned to the package target.
                if kind == "lead" and old_owner not in excluded_user_ids:
                    continue
                title = str(record.get("TITLE") or contact_name(record) or f"{kind} {eid}")
                rows.append({
                    **meta,
                    "entity_type": kind,
                    "entity_id": eid,
                    "title": title,
                    "old_owner_id": old_owner or "",
                    "old_owner_name": user_names.get(old_owner or -1, ""),
                    "new_owner_id": target,
                    "new_owner_name": user_names.get(target, ""),
                    "lead_status_id": str(record.get("STATUS_ID") or "") if kind == "lead" else "",
                    "target_reason": package.target_reason,
                    "current_owner_id": old_owner or "",
                    "action": "already_matches" if old_owner == target else "pending",
                    "error": "",
                })

    # Keep skipped source leads in the same single report file.
    for decision in skipped:
        package = decision.package
        meta = package_meta(package)
        if decision.skip_reason.startswith("protected_lead:"):
            action = "skipped_protected_lead"
        elif decision.skip_reason.startswith("protected_taldyk_majority:"):
            action = "skipped_protected_taldyk_majority"
        else:
            action = "skipped"
        for lid in sorted(package.lead_ids):
            lead = lead_by_id.get(lid, {})
            old_owner = normalized_id(lead.get("ASSIGNED_BY_ID"))
            rows.append({
                **meta,
                "entity_type": "lead",
                "entity_id": lid,
                "title": str(lead.get("TITLE") or f"lead {lid}"),
                "old_owner_id": old_owner or "",
                "old_owner_name": user_names.get(old_owner or -1, ""),
                "new_owner_id": "",
                "new_owner_name": "",
                "lead_status_id": str(lead.get("STATUS_ID") or ""),
                "target_reason": "",
                "current_owner_id": old_owner or "",
                "action": action,
                "error": decision.skip_reason,
            })

    return rows


def _method_for(kind: str, suffix: str) -> str:
    return f"crm.{kind}.{suffix}"


def _result_true(value: Any) -> bool:
    return value is True or str(value).strip().upper() in {"1", "TRUE", "Y"}


def get_owner(client: BitrixClient, kind: str, eid: int) -> int | None:
    result = client.call(_method_for(kind, "get"), {"id": int(eid)})
    return normalized_id((result or {}).get("ASSIGNED_BY_ID")) if isinstance(result, dict) else None


def update_owner_safely(
    client: BitrixClient,
    kind: str,
    eid: int,
    target_owner: int,
    *,
    attempts: int = 3,
) -> None:
    """Idempotent owner update with read-after-ambiguous-write protection."""
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            result = client.call(
                _method_for(kind, "update"),
                {
                    "id": int(eid),
                    "fields": {"ASSIGNED_BY_ID": int(target_owner)},
                    "params": {"REGISTER_SONET_EVENT": "N"},
                },
            )
            if _result_true(result):
                return
            last_error = f"Bitrix вернул result={result!r}"
        except Exception as exc:  # network timeout may happen after commit
            last_error = str(exc)

        try:
            if get_owner(client, kind, eid) == target_owner:
                return
        except Exception as check_exc:
            last_error = f"{last_error}; контроль: {check_exc}"

        if attempt < attempts:
            time.sleep(min(5.0, float(attempt)))
    raise ReassignmentError(
        f"{kind} ID={eid}: не удалось назначить владельца {target_owner}: {last_error}"
    )


def _progress_every() -> int:
    try:
        return max(1, int(os.getenv("REASSIGN_PROGRESS_EVERY", "25")))
    except ValueError:
        return 25


def print_progress(label: str, done: int, total: int, ok: int, errors: int) -> None:
    if total <= 0:
        return
    every = _progress_every()
    if done not in {1, total} and done % every != 0:
        return
    print(
        f"[{label}] {done}/{total} ({done / total * 100:.1f}%) | успешно: {ok} | ошибок: {errors}",
        flush=True,
    )


def apply_rows(client: BitrixClient, rows: list[dict[str, Any]], *, label: str = "ПЕРЕНОС") -> dict[str, int]:
    pending = [row for row in rows if row["action"] in {"pending", "verify_error"}]
    ok = errors = 0
    for index, row in enumerate(pending, 1):
        try:
            update_owner_safely(
                client,
                str(row["entity_type"]),
                int(row["entity_id"]),
                int(row["new_owner_id"]),
            )
            row["action"] = "updated"
            row["error"] = ""
            ok += 1
        except Exception as exc:
            row["action"] = "update_error"
            row["error"] = str(exc)
            errors += 1
        print_progress(label, index, len(pending), ok, errors)
    return {"planned": len(pending), "ok": ok, "errors": errors}


def verify_rows(
    rows: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    leads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sources = {
        "company": {eid: row for row in companies if (eid := entity_id(row)) is not None},
        "contact": {eid: row for row in contacts if (eid := entity_id(row)) is not None},
        "lead": {eid: row for row in leads if (eid := entity_id(row)) is not None},
    }
    mismatches: list[dict[str, Any]] = []
    for row in rows:
        if row["action"].startswith("skipped_"):
            continue
        current = sources[str(row["entity_type"])].get(int(row["entity_id"]))
        current_owner = normalized_id((current or {}).get("ASSIGNED_BY_ID"))
        row["current_owner_id"] = current_owner or ""
        expected = int(row["new_owner_id"])
        if current is None or current_owner != expected:
            row["action"] = "verify_error"
            row["error"] = f"owner={current_owner}, ожидается {expected}"
            mismatches.append(row)
        elif row["action"] != "already_matches":
            row["action"] = "updated"
            row["error"] = ""
    return mismatches


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "package_key", "package_leads", "package_companies", "package_contacts",
        "source_owner_ids", "context_owner_ids", "context_leads_total",
        "protected_16_18_38_leads", "other_leads", "company_ids", "company_titles",
        "founder_contact_ids", "founder_names", "package_warning",
        "entity_type", "entity_id", "title", "old_owner_id", "old_owner_name",
        "new_owner_id", "new_owner_name", "lead_status_id", "target_reason",
        "current_owner_id", "action", "error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in columns} for row in rows)


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


def run(
    client: BitrixClient,
    excluded_user_ids: set[int],
    output_dir: Path,
    *,
    apply: bool,
    target_rop_ids: tuple[int, int] = DEFAULT_TARGET_ROPS,
    protected_lead_ids: set[int] | None = None,
    protected_taldyk_manager_ids: set[int] | None = None,
) -> dict[str, Any]:
    protected_lead_ids = set(DEFAULT_PROTECTED_LEAD_IDS if protected_lead_ids is None else protected_lead_ids)
    protected_taldyk_manager_ids = set(
        DEFAULT_PROTECTED_TALDYK_MANAGER_IDS
        if protected_taldyk_manager_ids is None
        else protected_taldyk_manager_ids
    )
    if excluded_user_ids & set(target_rop_ids):
        raise ReassignmentError("Целевые РОП не могут входить в список исключённых пользователей")

    companies, contacts, leads = load_crm(client)
    packages = build_packages(companies, contacts, leads, excluded_user_ids)
    add_package_context(packages, companies, contacts, leads)

    owner_ids = set(excluded_user_ids) | set(target_rop_ids) | set(protected_taldyk_manager_ids)
    owner_ids.update(*(package.context_owner_ids for package in packages))
    users = load_users(client, owner_ids)
    validate_target_users(users, target_rop_ids)

    eligible, skipped = plan_packages(
        packages, protected_taldyk_manager_ids, protected_lead_ids, target_rop_ids
    )

    user_names = {uid: user_name(user, uid) for uid, user in users.items()}
    rows = build_rows(
        eligible, skipped, companies, contacts, leads, excluded_user_ids, user_names
    )

    transfer_rows = [r for r in rows if not str(r["action"]).startswith("skipped_")]
    pending_rows = [r for r in transfer_rows if r["action"] == "pending"]
    skipped_protected = [d for d in skipped if d.skip_reason.startswith("protected_lead:")]
    protected_target_packages = [
        p for p in eligible if p.target_owner_id in protected_taldyk_manager_ids
    ]
    rop_target_packages = [p for p in eligible if p.target_owner_id in set(target_rop_ids)]

    def count_type(kind: str, subset: list[dict[str, Any]]) -> int:
        return sum(r["entity_type"] == kind for r in subset)

    protected_distribution = ", ".join(
        f"{manager_id}: {sum(1 for p in protected_target_packages if p.target_owner_id == manager_id)} пакетов/"
        f"{sum(len(p.lead_ids) for p in protected_target_packages if p.target_owner_id == manager_id)} остатков-лидов"
        for manager_id in sorted(protected_taldyk_manager_ids)
    )

    print(
        "[ПЛАН] "
        f"исходных пакетов: {len(packages)}; "
        f"на РОП 72/73: {len(rop_target_packages)} пакетов; "
        f"остатки к 16/18/38: {len(protected_target_packages)} пакетов "
        f"({protected_distribution}); "
        f"защищённые лиды: {sum(len(d.package.lead_ids) for d in skipped_protected)}; "
        f"изменений: {len(pending_rows)} "
        f"(лиды {count_type('lead', pending_rows)}, компании {count_type('company', pending_rows)}, контакты {count_type('contact', pending_rows)}).",
        flush=True,
    )

    write_report(output_dir / "excluded_user_reassignment.csv", rows)

    first_pass = {"planned": len(pending_rows), "ok": 0, "errors": 0}
    retry_pass = {"planned": 0, "ok": 0, "errors": 0}
    mismatches: list[dict[str, Any]] = []

    if apply:
        first_pass = apply_rows(client, rows, label="ПЕРЕНОС")
        wait_seconds = max(0.0, float(os.getenv("REASSIGN_FINAL_WAIT_SECONDS", "2")))
        if wait_seconds:
            print(f"Ожидание {wait_seconds:g} сек. перед контрольной проверкой...", flush=True)
            time.sleep(wait_seconds)

        refreshed = load_crm(client)
        mismatches = verify_rows(rows, *refreshed)

        # One retry inside the same workflow is enough for transient/ambiguous writes.
        # No separate repair workflow or plan file is created.
        retryable = [r for r in mismatches if r.get("new_owner_id")]
        if retryable:
            print(f"[КОНТРОЛЬ] расхождений после первого прохода: {len(retryable)}. Повторяем только их один раз.", flush=True)
            for row in retryable:
                row["action"] = "verify_error"
            retry_pass = apply_rows(client, retryable, label="ПОВТОР")
            if wait_seconds:
                time.sleep(wait_seconds)
            refreshed = load_crm(client)
            mismatches = verify_rows(rows, *refreshed)

        write_report(output_dir / "excluded_user_reassignment.csv", rows)

        confirmed = [r for r in transfer_rows if r["action"] in {"updated", "already_matches"}]
        print(
            "[ИТОГ] "
            f"подтверждено {len(confirmed)}/{len(transfer_rows)}; "
            f"лиды {count_type('lead', confirmed)}/{count_type('lead', transfer_rows)}; "
            f"компании {count_type('company', confirmed)}/{count_type('company', transfer_rows)}; "
            f"контакты {count_type('contact', confirmed)}/{count_type('contact', transfer_rows)}; "
            f"ошибок/расхождений: {len(mismatches)}.",
            flush=True,
        )

    summary: dict[str, Any] = {
        "mode_apply": int(apply),
        "excluded_user_ids": sorted(excluded_user_ids),
        "packages_total": len(packages),
        "packages_transfer": len(eligible),
        "protected_taldyk_manager_ids": sorted(protected_taldyk_manager_ids),
        "packages_to_rops": len(rop_target_packages),
        "packages_to_protected_managers": len(protected_target_packages),
        "protected_lead_ids": sorted(protected_lead_ids),
        "rows_total_transfer": len(transfer_rows),
        "changes_planned": len(pending_rows),
        "leads_to_change": count_type("lead", pending_rows),
        "companies_to_change": count_type("company", pending_rows),
        "contacts_to_change": count_type("contact", pending_rows),
        "first_pass_ok": first_pass["ok"],
        "first_pass_errors": first_pass["errors"],
        "retry_planned": retry_pass["planned"],
        "retry_ok": retry_pass["ok"],
        "retry_errors": retry_pass["errors"],
        "final_verify_errors": len(mismatches),
    }
    for rop in target_rop_ids:
        summary[f"rop_{rop}_packages"] = sum(p.target_owner_id == rop for p in eligible)
        summary[f"rop_{rop}_leads"] = sum(len(p.lead_ids) for p in eligible if p.target_owner_id == rop)
    for manager_id in sorted(protected_taldyk_manager_ids):
        summary[f"manager_{manager_id}_packages"] = sum(
            p.target_owner_id == manager_id for p in protected_target_packages
        )
        summary[f"manager_{manager_id}_remaining_leads"] = sum(
            len(p.lead_ids) for p in protected_target_packages if p.target_owner_id == manager_id
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Переносит только ответственных у пакетов лидов исключённых пользователей. "
            "Стадии лидов не меняет. Если большинство связанных лидов НЕ на 16/18/38, "
            "пакет идёт РОПам 72/73. Иначе остатки пакета переходят тому из 16/18/38, "
            "у кого больше связанных лидов."
        )
    )
    parser.add_argument(
        "--excluded-user-ids",
        default=os.getenv("EXCLUDED_USER_IDS", "15,17,19,22,23,39,44"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    try:
        excluded = parse_id_set(args.excluded_user_ids, label="ID исключённых пользователей")
        target_ids = tuple(sorted(parse_id_set(
            os.getenv("REASSIGN_TARGET_ROP_IDS", "72,73"), label="ID целевых РОП"
        )))
        if len(target_ids) != 2:
            raise ReassignmentError("REASSIGN_TARGET_ROP_IDS должен содержать ровно два ID")
        protected = parse_optional_id_set(os.getenv("REASSIGN_PROTECTED_LEAD_IDS", "401"))
        protected_taldyk_managers = parse_optional_id_set(
            os.getenv("REASSIGN_PROTECTED_TALDYK_MANAGER_IDS", "16,18,38")
        )
        summary = run(
            build_client(), excluded, Path(args.output_dir), apply=args.apply,
            target_rop_ids=(target_ids[0], target_ids[1]),
            protected_lead_ids=protected,
            protected_taldyk_manager_ids=protected_taldyk_managers,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 1 if args.apply and summary["final_verify_errors"] else 0
    except (ReassignmentError, BitrixError, ValueError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
