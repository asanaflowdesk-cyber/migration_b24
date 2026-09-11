from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import xlsxwriter

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings


VISIBLE_COLUMNS = [
    "Руководитель",
    "Ответственный руководителя",
    "Компания",
    "Ответственный компании",
    "Лид",
    "Ответственный лида",
]


def normalized_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def parse_date(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return float("inf")


def lead_sort_key(lead: dict[str, Any]) -> tuple[float, int]:
    lead_id = normalized_id(lead.get("ID")) or 2**63 - 1
    return parse_date(lead.get("DATE_CREATE")), lead_id


def display_user(user: dict[str, Any] | None, user_id: int | None) -> str:
    if user_id is None:
        return ""
    if not user:
        return str(user_id)
    parts = [
        str(user.get("LAST_NAME") or "").strip(),
        str(user.get("NAME") or "").strip(),
        str(user.get("SECOND_NAME") or "").strip(),
    ]
    name = " ".join(part for part in parts if part)
    return name or str(user_id)


def display_director(contact: dict[str, Any]) -> str:
    parts = [
        str(contact.get("LAST_NAME") or "").strip(),
        str(contact.get("NAME") or "").strip(),
        str(contact.get("SECOND_NAME") or "").strip(),
    ]
    return " ".join(part for part in parts if part).strip()


def _normalize_name_part(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text


def director_key(contact: dict[str, Any]) -> str:
    parts = [
        _normalize_name_part(contact.get("LAST_NAME")),
        _normalize_name_part(contact.get("NAME")),
        _normalize_name_part(contact.get("SECOND_NAME")),
    ]
    return "|".join(parts)


def is_director_contact(contact: dict[str, Any]) -> bool:
    post = str(contact.get("POST") or "").casefold()
    comments = str(contact.get("COMMENTS") or "")
    return "руковод" in post or "EQAZYNA_DIRECTOR:" in comments


def load_users(client: BitrixClient) -> dict[int, dict[str, Any]]:
    try:
        rows = client.list_all("user.get", {"order": {"ID": "ASC"}})
    except Exception as exc:  # noqa: BLE001
        print(f"WARN user.get unavailable; reports will contain IDs only: {exc}")
        return {}
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        user_id = normalized_id(row.get("ID"))
        if user_id is not None:
            result[user_id] = row
    return result


def load_companies(client: BitrixClient) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID"],
        },
    )


def load_linked_leads(client: BitrixClient) -> list[dict[str, Any]]:
    rows = client.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": [
                "ID",
                "TITLE",
                "COMPANY_ID",
                "CONTACT_ID",
                "ASSIGNED_BY_ID",
                "DATE_CREATE",
            ],
        },
    )
    return [row for row in rows if normalized_id(row.get("COMPANY_ID")) is not None]


def load_director_contacts(client: BitrixClient) -> list[dict[str, Any]]:
    rows = client.list_all(
        "crm.contact.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": [
                "ID",
                "LAST_NAME",
                "NAME",
                "SECOND_NAME",
                "POST",
                "COMPANY_ID",
                "ASSIGNED_BY_ID",
                "COMMENTS",
            ],
        },
    )
    return [row for row in rows if director_key(row).strip("|") and is_director_contact(row)]


def _contact_sort_key(contact: dict[str, Any]) -> int:
    return normalized_id(contact.get("ID")) or 2**63 - 1


def _canonical_contact(contacts: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(contacts, key=_contact_sort_key)
    for contact in ordered:
        if normalized_id(contact.get("ASSIGNED_BY_ID")) is not None:
            return contact
    return ordered[0]


def _select_company_director_contacts(
    companies: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    director_contacts: Iterable[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Pick one director contact per company, deterministically.

    Priority: a director contact whose primary COMPANY_ID is this company;
    fallback: a director contact actually linked by one of the company's leads.
    Within a priority tier the oldest contact (smallest ID) wins.
    """
    company_ids = {
        company_id
        for company_id in (normalized_id(company.get("ID")) for company in companies)
        if company_id is not None
    }
    contacts_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    contacts_by_id: dict[int, dict[str, Any]] = {}
    for contact in director_contacts:
        contact_id = normalized_id(contact.get("ID"))
        if contact_id is not None:
            contacts_by_id[contact_id] = contact
        company_id = normalized_id(contact.get("COMPANY_ID"))
        if company_id in company_ids:
            contacts_by_company[company_id].append(contact)

    lead_contacts_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        company_id = normalized_id(lead.get("COMPANY_ID"))
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        if company_id is None or contact_id is None:
            continue
        contact = contacts_by_id.get(contact_id)
        if contact is not None:
            lead_contacts_by_company[company_id].append(contact)

    selected: dict[int, dict[str, Any]] = {}
    for company_id in sorted(company_ids):
        candidates = contacts_by_company.get(company_id) or lead_contacts_by_company.get(company_id) or []
        if candidates:
            selected[company_id] = min(candidates, key=_contact_sort_key)
    return selected


def build_desync_tree(
    companies: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    director_contacts: Iterable[dict[str, Any]],
    users: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-company director -> company -> leads desync groups.

    The selected director contact owner is the source of truth for that company.
    Equal normalized names in different companies never establish identity.
    """
    users = users or {}
    companies_list = list(companies)
    leads_list = list(leads)
    contacts_list = list(director_contacts)

    company_by_id: dict[int, dict[str, Any]] = {}
    for company in companies_list:
        company_id = normalized_id(company.get("ID"))
        if company_id is not None:
            company_by_id[company_id] = company

    leads_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads_list:
        company_id = normalized_id(lead.get("COMPANY_ID"))
        if company_id is not None:
            leads_by_company[company_id].append(lead)
    for company_leads in leads_by_company.values():
        company_leads.sort(key=lead_sort_key)

    selected_contact_by_company = _select_company_director_contacts(
        companies_list,
        leads_list,
        contacts_list,
    )

    # A matching FIO is not a global person identifier. Keep every company as
    # an independent ownership group so namesakes cannot reassign each other's
    # companies.
    company_ids_by_group: dict[tuple[int, str], list[int]] = defaultdict(list)
    for company_id, contact in selected_contact_by_company.items():
        key = director_key(contact)
        if key.strip("|"):
            company_ids_by_group[(company_id, key)].append(company_id)

    result: list[dict[str, Any]] = []
    for (group_company_id, key), company_ids in company_ids_by_group.items():
        selected = selected_contact_by_company[group_company_id]
        contacts = [
            contact
            for contact in contacts_list
            if director_key(contact) == key
            and normalized_id(contact.get("COMPANY_ID")) == group_company_id
        ] or [selected]
        if not contacts:
            continue
        canonical = _canonical_contact(contacts)
        root_owner_id = normalized_id(canonical.get("ASSIGNED_BY_ID"))
        contact_owner_ids = {
            owner_id
            for owner_id in (normalized_id(contact.get("ASSIGNED_BY_ID")) for contact in contacts)
            if owner_id is not None
        }

        company_nodes: list[dict[str, Any]] = []
        # The visible report compares the related entities to the canonical
        # director owner. Duplicate contact cards alone do not create a row
        # that would look synchronized without explaining why it was included.
        has_desync = root_owner_id is None

        for company_id in sorted(
            set(company_ids),
            key=lambda cid: str(company_by_id.get(cid, {}).get("TITLE") or "").casefold(),
        ):
            company = company_by_id.get(company_id)
            if company is None:
                continue
            company_owner_id = normalized_id(company.get("ASSIGNED_BY_ID"))
            linked_leads = leads_by_company.get(company_id, [])
            lead_owner_ids = {
                owner_id
                for owner_id in (normalized_id(lead.get("ASSIGNED_BY_ID")) for lead in linked_leads)
                if owner_id is not None
            }

            company_mismatch = root_owner_id is None or company_owner_id != root_owner_id
            lead_mismatch = any(
                normalized_id(lead.get("ASSIGNED_BY_ID")) != root_owner_id
                for lead in linked_leads
            ) if root_owner_id is not None else bool(linked_leads)
            has_desync = has_desync or company_mismatch or lead_mismatch

            company_nodes.append(
                {
                    "company_id": company_id,
                    "company_title": str(company.get("TITLE") or "").strip(),
                    "company_owner_id": company_owner_id,
                    "company_owner_name": display_user(users.get(company_owner_id), company_owner_id),
                    "company_mismatch": company_mismatch,
                    "unique_lead_owner_count": len(lead_owner_ids),
                    "leads": [
                        {
                            "lead_id": normalized_id(lead.get("ID")),
                            "lead_title": str(lead.get("TITLE") or "").strip(),
                            "lead_owner_id": normalized_id(lead.get("ASSIGNED_BY_ID")),
                            "lead_owner_name": display_user(
                                users.get(normalized_id(lead.get("ASSIGNED_BY_ID"))),
                                normalized_id(lead.get("ASSIGNED_BY_ID")),
                            ),
                            "lead_mismatch": (
                                root_owner_id is None
                                or normalized_id(lead.get("ASSIGNED_BY_ID")) != root_owner_id
                            ),
                        }
                        for lead in linked_leads
                    ],
                }
            )

        if not has_desync:
            continue

        result.append(
            {
                "director_key": key,
                "director_name": display_director(canonical),
                "director_owner_id": root_owner_id,
                "director_owner_name": display_user(users.get(root_owner_id), root_owner_id),
                "director_contact_owner_count": len(contact_owner_ids),
                "companies": company_nodes,
            }
        )

    result.sort(key=lambda group: group["director_name"].casefold())
    return result


def build_company_update_rows(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare safe company-only updates from the director owner.

    Existing leads and contacts are never changed here. A company whose leads
    are split between 3+ different managers remains manual-review only.
    """
    rows: list[dict[str, Any]] = []
    for group in tree:
        target_owner = normalized_id(group.get("director_owner_id"))
        for company in group.get("companies", []):
            current_owner = normalized_id(company.get("company_owner_id"))
            needs_update = target_owner is not None and current_owner != target_owner
            rows.append(
                {
                    "company_id": company["company_id"],
                    "target_owner_id": target_owner,
                    "needs_update": "Y" if needs_update else "N",
                    "unique_owner_count": int(company.get("unique_lead_owner_count") or 0),
                    "action": (
                        "manual_review_3plus"
                        if needs_update and int(company.get("unique_lead_owner_count") or 0) >= 3
                        else "pending_update"
                        if needs_update
                        else "skipped_director_no_owner"
                        if target_owner is None
                        else "already_matches"
                    ),
                    "error": "",
                }
            )
    return rows


def apply_updates(client: BitrixClient, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row.get("needs_update") != "Y":
            continue
        if int(row.get("unique_owner_count") or 0) >= 3:
            row["action"] = "manual_review_3plus"
            continue
        target_owner = normalized_id(row.get("target_owner_id"))
        if target_owner is None:
            row["action"] = "skipped_director_no_owner"
            continue
        try:
            client.update_company(str(int(row["company_id"])), {"ASSIGNED_BY_ID": target_owner})
            row["action"] = "updated"
        except Exception as exc:  # noqa: BLE001
            row["action"] = "update_error"
            row["error"] = str(exc)


def write_tree_xlsx(path: Path, tree: list[dict[str, Any]]) -> None:
    """Write a sparse tree exactly as: director -> company -> leads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(path)
    ws = workbook.add_worksheet("Рассинхрон")

    def write_literal(row: int, column: int, value: Any, cell_format: Any) -> None:
        ws.write_string(row, column, str(value or ""), cell_format)

    header = workbook.add_format(
        {
            "bold": True,
            "bg_color": "#D9EAF7",
            "border": 1,
            "valign": "vcenter",
            "align": "left",
        }
    )
    root_fmt = workbook.add_format(
        {"bold": True, "bg_color": "#EAF2F8", "top": 1, "bottom": 1, "valign": "vcenter"}
    )
    root_owner_fmt = workbook.add_format(
        {"bold": True, "bg_color": "#EAF2F8", "top": 1, "bottom": 1, "valign": "vcenter"}
    )
    root_missing_fmt = workbook.add_format(
        {"bold": True, "bg_color": "#FFF2CC", "top": 1, "bottom": 1, "valign": "vcenter"}
    )
    company_fmt = workbook.add_format(
        {"bold": True, "bg_color": "#F7F7F7", "valign": "vcenter", "left": 1}
    )
    company_owner_fmt = workbook.add_format(
        {"bold": True, "bg_color": "#F7F7F7", "valign": "vcenter"}
    )
    mismatch_fmt = workbook.add_format(
        {"bg_color": "#FCE8E6", "font_color": "#B3261E", "valign": "vcenter"}
    )
    lead_fmt = workbook.add_format({"valign": "vcenter"})
    lead_owner_fmt = workbook.add_format({"valign": "vcenter"})

    for col, title in enumerate(VISIBLE_COLUMNS):
        ws.write(0, col, title, header)

    row_idx = 1
    for group in tree:
        first_root_row = True
        director_owner_id = normalized_id(group.get("director_owner_id"))
        companies = group.get("companies", []) or [{}]
        for company in companies:
            leads = company.get("leads") or [{}]
            first_company_row = True
            for lead in leads:
                if first_root_row:
                    write_literal(row_idx, 0, group.get("director_name", ""), root_fmt)
                    write_literal(
                        row_idx,
                        1,
                        group.get("director_owner_name", ""),
                        root_owner_fmt if director_owner_id is not None else root_missing_fmt,
                    )
                    first_root_row = False
                else:
                    ws.write_blank(row_idx, 0, None, lead_fmt)
                    ws.write_blank(row_idx, 1, None, lead_fmt)

                if first_company_row:
                    write_literal(row_idx, 2, company.get("company_title", ""), company_fmt)
                    company_owner_format = mismatch_fmt if company.get("company_mismatch") else company_owner_fmt
                    write_literal(row_idx, 3, company.get("company_owner_name", ""), company_owner_format)
                    first_company_row = False
                else:
                    ws.write_blank(row_idx, 2, None, lead_fmt)
                    ws.write_blank(row_idx, 3, None, lead_fmt)

                write_literal(row_idx, 4, lead.get("lead_title", ""), lead_fmt)
                lead_owner_format = mismatch_fmt if lead.get("lead_mismatch") else lead_owner_fmt
                write_literal(row_idx, 5, lead.get("lead_owner_name", ""), lead_owner_format)
                row_idx += 1

    ws.freeze_panes(1, 0)
    ws.set_row(0, 24)
    ws.set_column(0, 0, 32)
    ws.set_column(1, 1, 28)
    ws.set_column(2, 2, 44)
    ws.set_column(3, 3, 28)
    ws.set_column(4, 4, 46)
    ws.set_column(5, 5, 28)
    ws.hide_gridlines(2)
    workbook.close()


def run(client: BitrixClient, output_dir: Path, apply: bool) -> dict[str, int]:
    print("Loading companies...")
    companies = load_companies(client)
    print(f"Companies loaded: {len(companies)}")

    print("Loading leads...")
    leads = load_linked_leads(client)
    print(f"Linked leads loaded: {len(leads)}")

    print("Loading director contacts...")
    director_contacts = load_director_contacts(client)
    print(f"Director contacts loaded: {len(director_contacts)}")

    users = load_users(client)
    tree = build_desync_tree(companies, leads, director_contacts, users)
    update_rows = build_company_update_rows(tree)

    if apply:
        apply_updates(client, update_rows)
        # Rebuild from current in-memory company owners for the report only when
        # updates succeeded; this keeps the artifact useful after apply without
        # re-fetching all CRM entities.
        updated_by_company = {
            int(row["company_id"]): normalized_id(row.get("target_owner_id"))
            for row in update_rows
            if row.get("action") == "updated"
        }
        if updated_by_company:
            for company in companies:
                company_id = normalized_id(company.get("ID"))
                if company_id in updated_by_company:
                    company["ASSIGNED_BY_ID"] = updated_by_company[company_id]
            tree = build_desync_tree(companies, leads, director_contacts, users)

    write_tree_xlsx(output_dir / "company_owner_desync.xlsx", tree)

    errors = [row for row in update_rows if row.get("action") == "update_error"]
    manual = [row for row in update_rows if row.get("action") == "manual_review_3plus"]
    summary = {
        "companies_total": len(companies),
        "linked_leads_total": len(leads),
        "director_contacts_total": len(director_contacts),
        "desync_directors": len(tree),
        "desync_companies": sum(len(group.get("companies", [])) for group in tree),
        "manual_review_3plus": len(manual),
        "updated": sum(1 for row in update_rows if row.get("action") == "updated"),
        "update_errors": len(errors),
        "mode_apply": int(apply),
    }
    (output_dir / "company_owner_sync_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def build_client() -> BitrixClient:
    settings = Settings.from_env()
    if not settings.bitrix_webhook_url:
        raise SystemExit("TARGET_BITRIX_WEBHOOK_URL is not set")
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
            "Scan ownership desync as director -> company -> leads. "
            "The responsible manager of the director contact is the source of truth."
        )
    )
    parser.add_argument("--apply", action="store_true", help="update company owners only; default is dry-run")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = run(build_client(), output_dir, apply=args.apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["update_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
