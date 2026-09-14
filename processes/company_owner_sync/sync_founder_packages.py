from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import xlsxwriter

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings


def normalized_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _part(value: Any) -> str:
    value = str(value or "").strip().casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я-]+", "", value)


def person_from_parts(last: Any, first: Any, middle: Any = "") -> tuple[str, str, str] | None:
    result = (_part(last), _part(first), _part(middle))
    return result if result[0] and result[1] else None


def person_from_text(value: Any) -> tuple[str, str, str] | None:
    words = [word for word in re.split(r"\s+", str(value or "").strip()) if word]
    if len(words) < 2:
        return None
    return person_from_parts(words[0], words[1], words[2] if len(words) > 2 else "")


def display_person(person: tuple[str, str, str]) -> str:
    return " ".join(part for part in person if part).title()


def modified_key(contact: dict[str, Any]) -> tuple[float, int]:
    raw = str(contact.get("DATE_MODIFY") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = parsed.timestamp()
    except ValueError:
        stamp = 0.0
    return stamp, normalized_id(contact.get("ID")) or 0


def is_founder_contact(contact: dict[str, Any]) -> bool:
    post = str(contact.get("POST") or "").casefold()
    comments = str(contact.get("COMMENTS") or "")
    return (
        "руковод" in post
        or "учред" in post
        or "EQAZYNA_DIRECTOR:" in comments
    )


def _resolved_person_keys(
    contacts: Iterable[dict[str, Any]],
    requisites: Iterable[dict[str, Any]],
) -> tuple[dict[int, tuple[str, str, str]], dict[int, tuple[str, str, str]], set[tuple[str, str]]]:
    contact_people: dict[int, tuple[str, str, str]] = {}
    requisite_people: dict[int, tuple[str, str, str]] = {}
    middles_by_base: dict[tuple[str, str], set[str]] = defaultdict(set)

    for contact in contacts:
        contact_id = normalized_id(contact.get("ID"))
        person = person_from_parts(contact.get("LAST_NAME"), contact.get("NAME"), contact.get("SECOND_NAME"))
        if contact_id and person:
            contact_people[contact_id] = person
            if person[2]:
                middles_by_base[person[:2]].add(person[2])

    for requisite in requisites:
        company_id = normalized_id(requisite.get("ENTITY_ID"))
        person = person_from_text(requisite.get("RQ_DIRECTOR"))
        if company_id and person:
            requisite_people[company_id] = person
            if person[2]:
                middles_by_base[person[:2]].add(person[2])

    ambiguous_bases = {base for base, middles in middles_by_base.items() if len(middles) > 1}

    def resolve(person: tuple[str, str, str]) -> tuple[str, str, str] | None:
        base = person[:2]
        if person[2]:
            return person
        if base in ambiguous_bases:
            return None
        known = middles_by_base.get(base, set())
        return (person[0], person[1], next(iter(known), ""))

    return (
        {item_id: key for item_id, person in contact_people.items() if (key := resolve(person))},
        {item_id: key for item_id, person in requisite_people.items() if (key := resolve(person))},
        ambiguous_bases,
    )


def build_packages(
    companies: Iterable[dict[str, Any]],
    leads: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    requisites: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    companies = list(companies)
    leads = list(leads)
    contacts = [item for item in contacts if is_founder_contact(item)]
    requisites = list(requisites)
    contact_keys, requisite_keys, ambiguous_bases = _resolved_person_keys(contacts, requisites)

    contacts_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for contact in contacts:
        contact_id = normalized_id(contact.get("ID"))
        if contact_id in contact_keys:
            contacts_by_key[contact_keys[contact_id]].append(contact)

    company_candidate_keys: dict[int, set[tuple[str, str, str]]] = defaultdict(set)
    for company_id, key in requisite_keys.items():
        company_candidate_keys[company_id].add(key)
    for contact in contacts:
        contact_id = normalized_id(contact.get("ID"))
        company_id = normalized_id(contact.get("COMPANY_ID"))
        if contact_id in contact_keys and company_id:
            company_candidate_keys[company_id].add(contact_keys[contact_id])
    for lead in leads:
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        company_id = normalized_id(lead.get("COMPANY_ID"))
        if contact_id in contact_keys and company_id:
            company_candidate_keys[company_id].add(contact_keys[contact_id])

    company_key: dict[int, tuple[str, str, str]] = {}
    skipped: list[dict[str, Any]] = []
    company_by_id = {normalized_id(item.get("ID")): item for item in companies}
    for company_id, keys in company_candidate_keys.items():
        if len(keys) == 1:
            company_key[company_id] = next(iter(keys))
        elif len(keys) > 1:
            skipped.append({"type": "company_conflicting_fio", "company_id": company_id})

    leads_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        company_id = normalized_id(lead.get("COMPANY_ID"))
        if company_id:
            leads_by_company[company_id].append(lead)

    companies_by_key: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for company_id, key in company_key.items():
        if company_id in company_by_id:
            companies_by_key[key].append(company_id)

    packages: list[dict[str, Any]] = []
    for key in sorted(set(contacts_by_key) | set(companies_by_key)):
        group_contacts = contacts_by_key.get(key, [])
        if not group_contacts:
            skipped.append({"type": "company_without_founder_contact", "fio": display_person(key)})
            continue
        owned_contacts = [item for item in group_contacts if normalized_id(item.get("ASSIGNED_BY_ID"))]
        if not owned_contacts:
            skipped.append({"type": "founder_without_owner", "fio": display_person(key)})
            continue
        source = max(owned_contacts, key=modified_key)
        owner_id = normalized_id(source.get("ASSIGNED_BY_ID"))
        company_nodes = []
        for company_id in sorted(set(companies_by_key.get(key, []))):
            company = company_by_id[company_id]
            company_nodes.append({
                "id": company_id,
                "title": str(company.get("TITLE") or "").strip(),
                "owner_id": normalized_id(company.get("ASSIGNED_BY_ID")),
                "leads": [
                    {
                        "id": normalized_id(lead.get("ID")),
                        "title": str(lead.get("TITLE") or "").strip(),
                        "owner_id": normalized_id(lead.get("ASSIGNED_BY_ID")),
                    }
                    for lead in leads_by_company.get(company_id, [])
                    if normalized_id(lead.get("ID"))
                ],
            })
        packages.append({
            "fio": display_person(key),
            "owner_id": owner_id,
            "source_contact_id": normalized_id(source.get("ID")),
            "contacts": [
                {"id": normalized_id(item.get("ID")), "owner_id": normalized_id(item.get("ASSIGNED_BY_ID"))}
                for item in group_contacts
            ],
            "companies": company_nodes,
        })

    for base in sorted(ambiguous_bases):
        skipped.append({"type": "missing_patronymic_is_ambiguous", "fio": display_person((*base, ""))})
    return packages, skipped


def build_update_rows(packages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for package in packages:
        target = normalized_id(package.get("owner_id"))
        source_id = normalized_id(package.get("source_contact_id"))
        for contact in package.get("contacts", []):
            item_id = normalized_id(contact.get("id"))
            if item_id and item_id != source_id and normalized_id(contact.get("owner_id")) != target:
                rows.append({"fio": package["fio"], "entity": "contact", "id": item_id, "target": target, "status": "planned"})
        for company in package.get("companies", []):
            if normalized_id(company.get("owner_id")) != target:
                rows.append({"fio": package["fio"], "entity": "company", "id": company["id"], "target": target, "status": "planned"})
            for lead in company.get("leads", []):
                if normalized_id(lead.get("owner_id")) != target:
                    rows.append({"fio": package["fio"], "entity": "lead", "id": lead["id"], "target": target, "status": "planned"})
    return rows


def apply_updates(client: BitrixClient, rows: list[dict[str, Any]]) -> None:
    methods = {"contact": client.update_contact, "company": client.update_company, "lead": client.update_lead}
    for row in rows:
        try:
            methods[row["entity"]](str(row["id"]), {"ASSIGNED_BY_ID": row["target"]})
            row["status"] = "updated"
        except Exception as exc:  # noqa: BLE001
            row["status"] = "error"
            row["error"] = str(exc)


def load(client: BitrixClient, method: str, select: list[str], filter_: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return client.list_all(method, {"order": {"ID": "ASC"}, "filter": filter_ or {}, "select": select})


def write_report(path: Path, packages: list[dict[str, Any]], rows: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    book = xlsxwriter.Workbook(path)
    ws = book.add_worksheet("Пакеты")
    headers = ["ФИО", "Ответственный ID", "Источник контакт ID", "Компаний", "Лидов", "Изменений"]
    head = book.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
    for col, value in enumerate(headers):
        ws.write(0, col, value, head)
    changes_by_fio: dict[str, int] = defaultdict(int)
    for row in rows:
        changes_by_fio[row["fio"]] += 1
    for index, package in enumerate(packages, 1):
        ws.write_row(index, 0, [package["fio"], package["owner_id"], package["source_contact_id"], len(package["companies"]), sum(len(c["leads"]) for c in package["companies"]), changes_by_fio[package["fio"]]])
    ws.set_column(0, 0, 36); ws.set_column(1, 5, 20); ws.freeze_panes(1, 0)
    sk = book.add_worksheet("Пропуски")
    sk.write_row(0, 0, ["Причина", "ФИО", "Компания ID"], head)
    for index, item in enumerate(skipped, 1):
        sk.write_row(index, 0, [item.get("type", ""), item.get("fio", ""), item.get("company_id", "")])
    sk.set_column(0, 0, 38); sk.set_column(1, 1, 36); sk.set_column(2, 2, 18)
    book.close()


def run(client: BitrixClient, output_dir: Path, apply: bool) -> dict[str, int]:
    companies = load(client, "crm.company.list", ["ID", "TITLE", "ASSIGNED_BY_ID"])
    leads = load(client, "crm.lead.list", ["ID", "TITLE", "COMPANY_ID", "CONTACT_ID", "ASSIGNED_BY_ID"])
    contacts = load(client, "crm.contact.list", ["ID", "LAST_NAME", "NAME", "SECOND_NAME", "POST", "COMPANY_ID", "ASSIGNED_BY_ID", "COMMENTS", "DATE_MODIFY"])
    requisites = load(client, "crm.requisite.list", ["ID", "ENTITY_ID", "ENTITY_TYPE_ID", "RQ_DIRECTOR"], {"ENTITY_TYPE_ID": 4})
    packages, skipped = build_packages(companies, leads, contacts, requisites)
    rows = build_update_rows(packages)
    if apply:
        apply_updates(client, rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_report(output_dir / "founder_package_owner_sync.xlsx", packages, rows, skipped)
    summary = {
        "packages": len(packages),
        "companies": sum(len(item["companies"]) for item in packages),
        "leads": sum(len(company["leads"]) for item in packages for company in item["companies"]),
        "planned": len(rows),
        "updated": sum(row["status"] == "updated" for row in rows),
        "errors": sum(row["status"] == "error" for row in rows),
        "skipped": len(skipped),
    }
    (output_dir / "company_owner_sync_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize a founder's contacts, companies and leads to one owner")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()
    settings = Settings.from_env()
    summary = run(
        BitrixClient(
            settings.bitrix_webhook_url or "",
            timeout=settings.bitrix_request_timeout,
            polite_delay_seconds=settings.bitrix_polite_delay_seconds,
            verify_ssl=settings.bitrix_tls_verify,
        ),
        Path(args.output_dir),
        args.apply,
    )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
