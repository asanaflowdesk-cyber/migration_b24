from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import xlsxwriter

# The repository workflow adds processes/eqazyna_leads to PYTHONPATH so this
# reuses the already tested Bitrix REST client and TLS settings.
from eqazyna_bitrix.bitrix_client import BitrixClient, BitrixError
from eqazyna_bitrix.settings import Settings


AUDIT_FIELDS = [
    "company_id",
    "company_title",
    "company_owner_id",
    "company_owner_name",
    "first_lead_id",
    "first_lead_date_create",
    "first_lead_title",
    "first_lead_owner_id",
    "first_lead_owner_name",
    "lead_count",
    "unique_owner_count",
    "owner_distribution",
    "owner_ids",
    "owner_names",
    "needs_update",
    "action",
    "error",
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
    lead_id = normalized_id(lead.get("ID")) or sys.maxsize
    return parse_date(lead.get("DATE_CREATE")), lead_id


def earliest_lead(leads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return min(leads, key=lead_sort_key)


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


def load_users(client: BitrixClient) -> dict[int, dict[str, Any]]:
    try:
        rows = client.list_all(
            "user.get",
            {
                "order": {"ID": "ASC"},
            },
        )
    except Exception as exc:  # noqa: BLE001 - names are optional for the audit
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
                "ASSIGNED_BY_ID",
                "DATE_CREATE",
            ],
        },
    )
    return [row for row in rows if normalized_id(row.get("COMPANY_ID")) is not None]


def build_audit_rows(
    companies: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    users: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    users = users or {}
    by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        company_id = normalized_id(lead.get("COMPANY_ID"))
        if company_id is not None:
            by_company[company_id].append(lead)

    rows: list[dict[str, Any]] = []
    for company in companies:
        company_id = normalized_id(company.get("ID"))
        if company_id is None:
            continue
        linked = by_company.get(company_id, [])
        if not linked:
            continue

        first = earliest_lead(linked)
        company_owner = normalized_id(company.get("ASSIGNED_BY_ID"))
        first_owner = normalized_id(first.get("ASSIGNED_BY_ID"))
        owners = [
            owner_id
            for owner_id in (normalized_id(lead.get("ASSIGNED_BY_ID")) for lead in linked)
            if owner_id is not None
        ]
        counts = Counter(owners)
        owner_ids = sorted(counts)
        owner_names = [display_user(users.get(owner_id), owner_id) for owner_id in owner_ids]
        distribution = "; ".join(
            f"{owner_id} {display_user(users.get(owner_id), owner_id)} — {counts[owner_id]}"
            for owner_id in owner_ids
        )
        needs_update = first_owner is not None and company_owner != first_owner

        rows.append(
            {
                "company_id": company_id,
                "company_title": str(company.get("TITLE") or "").strip(),
                "company_owner_id": company_owner or "",
                "company_owner_name": display_user(users.get(company_owner), company_owner),
                "first_lead_id": normalized_id(first.get("ID")) or "",
                "first_lead_date_create": str(first.get("DATE_CREATE") or "").strip(),
                "first_lead_title": str(first.get("TITLE") or "").strip(),
                "first_lead_owner_id": first_owner or "",
                "first_lead_owner_name": display_user(users.get(first_owner), first_owner),
                "lead_count": len(linked),
                "unique_owner_count": len(owner_ids),
                "owner_distribution": distribution,
                "owner_ids": ",".join(str(owner_id) for owner_id in owner_ids),
                "owner_names": "; ".join(owner_names),
                "needs_update": "Y" if needs_update else "N",
                "action": (
                    "pending_update"
                    if needs_update
                    else "skipped_first_lead_no_owner"
                    if first_owner is None
                    else "already_matches"
                ),
                "error": "",
            }
        )
    return rows


def apply_updates(client: BitrixClient, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row["needs_update"] != "Y":
            continue
        company_id = int(row["company_id"])
        target_owner = normalized_id(row["first_lead_owner_id"])
        if target_owner is None:
            row["action"] = "skipped_first_lead_no_owner"
            continue
        try:
            client.update_company(str(company_id), {"ASSIGNED_BY_ID": target_owner})
            row["action"] = "updated"
        except Exception as exc:  # noqa: BLE001 - continue auditing other companies
            row["action"] = "update_error"
            row["error"] = str(exc)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in AUDIT_FIELDS} for row in rows)


def write_xlsx(path: Path, sheets: list[tuple[str, list[dict[str, Any]]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(path)
    header = workbook.add_format({"bold": True, "text_wrap": True, "valign": "top"})
    wrap = workbook.add_format({"text_wrap": True, "valign": "top"})
    for sheet_name, rows in sheets:
        ws = workbook.add_worksheet(sheet_name[:31])
        for col, field in enumerate(AUDIT_FIELDS):
            ws.write(0, col, field, header)
        for r_idx, row in enumerate(rows, start=1):
            for c_idx, field in enumerate(AUDIT_FIELDS):
                ws.write(r_idx, c_idx, row.get(field, ""), wrap)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, max(0, len(rows)), len(AUDIT_FIELDS) - 1)
        widths = {
            "company_id": 12,
            "company_title": 35,
            "company_owner_name": 28,
            "first_lead_id": 12,
            "first_lead_date_create": 23,
            "first_lead_title": 40,
            "first_lead_owner_name": 28,
            "owner_distribution": 55,
            "owner_names": 55,
            "action": 25,
            "error": 55,
        }
        for col, field in enumerate(AUDIT_FIELDS):
            ws.set_column(col, col, widths.get(field, 16))
    workbook.close()


def run(client: BitrixClient, output_dir: Path, apply: bool) -> dict[str, int]:
    print("Loading companies...")
    companies = load_companies(client)
    print(f"Companies loaded: {len(companies)}")

    print("Loading leads...")
    leads = load_linked_leads(client)
    print(f"Linked leads loaded: {len(leads)}")

    users = load_users(client)
    rows = build_audit_rows(companies, leads, users)

    if apply:
        apply_updates(client, rows)

    mismatches = [row for row in rows if row["needs_update"] == "Y"]
    spread = [row for row in rows if int(row["unique_owner_count"]) >= 3]
    errors = [row for row in rows if row["action"] == "update_error"]
    no_first_owner = [row for row in rows if row["action"] == "skipped_first_lead_no_owner"]

    write_csv(output_dir / "company_owner_audit.csv", rows)
    write_csv(output_dir / "company_owner_mismatches.csv", mismatches)
    write_csv(output_dir / "company_lead_owner_spread_3plus.csv", spread)
    write_xlsx(
        output_dir / "company_owner_sync.xlsx",
        [
            ("Несовпадения", mismatches),
            ("3+ ответственных", spread),
            ("Все с лидами", rows),
        ],
    )

    summary = {
        "companies_total": len(companies),
        "companies_with_linked_leads": len(rows),
        "linked_leads_total": len(leads),
        "mismatches": len(mismatches),
        "spread_3plus": len(spread),
        "first_lead_without_owner": len(no_first_owner),
        "updated": sum(1 for row in rows if row["action"] == "updated"),
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
            "Synchronize company ASSIGNED_BY_ID from the earliest lead linked by COMPANY_ID. "
            "The earliest lead is chosen by DATE_CREATE, then by the smallest ID."
        )
    )
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = run(build_client(), output_dir, apply=args.apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["update_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
