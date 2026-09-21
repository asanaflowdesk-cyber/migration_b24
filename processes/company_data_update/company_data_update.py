from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.bitrix import BitrixClient, sanitize_error
from google_sheets import GoogleSheetsClient, column_letter


ENTITY_TYPE_COMPANY = 4
OUTPUT_DIR = Path("output")
RESULT_COLUMNS = ["STATUS", "CHANGES", "MESSAGE", "BITRIX_COMPANY_ID", "UPDATED_AT"]
NULL_TOKENS = {"", "-", "—", "нет", "не найдено", "не найден", "n/a", "na", "none", "null"}
IDENTITY_ALIASES = {
    "company_id": {"company_id", "company id", "id компании", "битрикс id", "bitrix id"},
    "origin_id": {"origin_id", "origin id", "бин", "bin", "бизнес идентификатор"},
    "name": {"name", "company_name", "company name", "название", "наименование", "title"},
    "phone": {"phone", "телефон", "телефоны"},
    "email": {"email", "e-mail", "почта", "электронная почта"},
    "web": {"web", "website", "site", "сайт"},
    "phone_type": {"phone_type", "phone type", "тип телефона"},
    "email_type": {"email_type", "email type", "тип email", "тип почты"},
    "web_type": {"web_type", "web type", "тип сайта"},
}
IGNORED_HEADERS = {header.casefold() for header in RESULT_COLUMNS} | {"address", "адрес"}


@dataclass
class SheetRow:
    row_number: int
    company_id: str
    origin_id: str
    name: str
    phone: str
    email: str
    web: str
    phone_type: str
    email_type: str
    web_type: str
    custom_fields: dict[str, str]


@dataclass
class Action:
    row_number: int
    source_company_id: str
    origin_id: str
    name: str
    resolved_company_id: str
    status: str
    changes: str
    message: str
    updated_at: str


def clean(value: Any) -> str:
    return str(value or "").strip()


def header_key(value: Any) -> str:
    text = clean(value).casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-яәіңғүұқөһ_\-]+", " ", text)
    return " ".join(text.split())


def normalize_spreadsheet_id(value: str) -> str:
    raw = clean(value)
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    return match.group(1) if match else raw


def normalize_bin(value: Any) -> str:
    digits = re.sub(r"\D", "", clean(value))
    return digits if len(digits) == 12 else ""


def split_values(value: Any) -> list[str]:
    raw = clean(value)
    if raw.casefold() in NULL_TOKENS:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[;\n\r]+", raw):
        item = item.strip()
        if not item or item.casefold() in NULL_TOKENS:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def normalize_phone(value: Any) -> str:
    return re.sub(r"\D", "", clean(value))


def normalize_email(value: Any) -> str:
    return clean(value).casefold()


def normalize_web(value: Any) -> str:
    return clean(value).casefold().rstrip("/")


def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))


def parse_rows(rows: list[list[str]], company_fields: dict[str, Any]) -> tuple[list[SheetRow], list[str]]:
    if not rows:
        raise ValueError("Лист пуст")
    headers = rows[0]
    keyed = {header_key(value): index for index, value in enumerate(headers) if clean(value)}

    def find(alias_name: str) -> int | None:
        aliases = IDENTITY_ALIASES[alias_name]
        for alias in aliases:
            if header_key(alias) in keyed:
                return keyed[header_key(alias)]
        return None

    columns = {name: find(name) for name in IDENTITY_ALIASES}
    if columns["company_id"] is None and columns["origin_id"] is None:
        raise ValueError("Нужна колонка COMPANY_ID и/или ORIGIN_ID/БИН")

    custom_columns: dict[int, str] = {}
    errors: list[str] = []
    known_alias_keys = {
        header_key(alias)
        for aliases in IDENTITY_ALIASES.values()
        for alias in aliases
    }
    for index, raw_header in enumerate(headers):
        raw = clean(raw_header)
        key = header_key(raw)
        if not raw or key in known_alias_keys or key in IGNORED_HEADERS:
            continue
        upper = raw.upper()
        if upper.startswith("UF_CRM_"):
            if upper not in company_fields:
                errors.append(f"Неизвестное поле компании {upper!r}")
                continue
            metadata = company_fields.get(upper) or {}
            field_type = clean(metadata.get("type") or metadata.get("USER_TYPE_ID")).casefold()
            if metadata.get("isMultiple") is True or clean(metadata.get("isMultiple")).upper() == "Y":
                errors.append(f"Поле {upper!r} множественное; этот поток его не меняет")
                continue
            if field_type == "file":
                errors.append(f"Поле {upper!r} файловое; этот поток его не меняет")
                continue
            custom_columns[index] = upper

    if errors:
        return [], errors

    def cell(cells: list[str], index: int | None) -> str:
        return clean(cells[index]) if index is not None and index < len(cells) else ""

    parsed: list[SheetRow] = []
    for row_number, cells in enumerate(rows[1:], start=2):
        company_id = cell(cells, columns["company_id"])
        origin_id = cell(cells, columns["origin_id"])
        phone = cell(cells, columns["phone"])
        email = cell(cells, columns["email"])
        web = cell(cells, columns["web"])
        custom = {
            field: cell(cells, index)
            for index, field in custom_columns.items()
            if cell(cells, index).casefold() not in NULL_TOKENS
        }
        if not any((company_id, origin_id, phone, email, web, custom)):
            continue
        parsed.append(
            SheetRow(
                row_number=row_number,
                company_id=company_id,
                origin_id=origin_id,
                name=cell(cells, columns["name"]),
                phone=phone,
                email=email,
                web=web,
                phone_type=(cell(cells, columns["phone_type"]) or "WORK").upper(),
                email_type=(cell(cells, columns["email_type"]) or "WORK").upper(),
                web_type=(cell(cells, columns["web_type"]) or "WORK").upper(),
                custom_fields=custom,
            )
        )
    return parsed, []


def company_bins(client: BitrixClient, company: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    origin = normalize_bin(company.get("ORIGIN_ID"))
    if origin:
        result.add(origin)
    company_id = clean(company.get("ID"))
    if company_id:
        requisites = client.list_all(
            "crm.requisite.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ENTITY_TYPE_ID": ENTITY_TYPE_COMPANY, "ENTITY_ID": int(company_id)},
                "select": ["ID", "ENTITY_ID", "RQ_INN"],
            },
        )
        for item in requisites:
            value = normalize_bin(item.get("RQ_INN"))
            if value:
                result.add(value)
    return result


def find_company_by_bin(
    client: BitrixClient,
    bin_value: str,
) -> tuple[dict[str, Any] | None, str]:
    ids: set[str] = set()
    companies = client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"=ORIGIN_ID": bin_value},
            "select": ["ID", "TITLE", "ORIGIN_ID", "PHONE", "EMAIL", "WEB", "UF_*"],
        },
    )
    for company in companies:
        cid = clean(company.get("ID"))
        if cid:
            ids.add(cid)

    requisites = client.list_all(
        "crm.requisite.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"ENTITY_TYPE_ID": ENTITY_TYPE_COMPANY, "=RQ_INN": bin_value},
            "select": ["ID", "ENTITY_ID", "RQ_INN"],
        },
    )
    for item in requisites:
        cid = clean(item.get("ENTITY_ID"))
        if cid:
            ids.add(cid)

    if not ids:
        return None, "Компания по БИН/ORIGIN_ID не найдена"
    if len(ids) > 1:
        return None, f"По БИН найдено несколько компаний: {', '.join(sorted(ids, key=int))}"
    company_id = next(iter(ids))
    company = client.call("crm.company.get", {"id": int(company_id)})
    if not isinstance(company, dict):
        return None, f"crm.company.get не вернул компанию ID {company_id}"
    return company, ""


def resolve_company(
    client: BitrixClient,
    row: SheetRow,
) -> tuple[dict[str, Any] | None, str]:
    expected_bin = normalize_bin(row.origin_id)
    if row.origin_id and not expected_bin:
        return None, "ORIGIN_ID/БИН должен содержать ровно 12 цифр"

    if row.company_id:
        if not row.company_id.isdigit() or int(row.company_id) <= 0:
            return None, "Некорректный COMPANY_ID"
        company = client.call("crm.company.get", {"id": int(row.company_id)})
        if not isinstance(company, dict):
            return None, f"Компания ID {row.company_id} не найдена"
        if expected_bin:
            bins = company_bins(client, company)
            if expected_bin not in bins:
                actual = ", ".join(sorted(bins)) if bins else "нет БИН в ORIGIN_ID/реквизитах"
                return None, f"БИН не совпадает: в таблице {expected_bin}, в Bitrix {actual}"
        return company, ""

    if expected_bin:
        return find_company_by_bin(client, expected_bin)
    return None, "Не указан COMPANY_ID или корректный 12-значный БИН"


def existing_multifield(company: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = company.get(field)
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def plan_multifields(
    company: dict[str, Any],
    row: SheetRow,
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    additions: list[dict[str, str]] = []
    changes: list[str] = []
    errors: list[str] = []

    specs = [
        ("PHONE", split_values(row.phone), row.phone_type, normalize_phone),
        ("EMAIL", split_values(row.email), row.email_type, normalize_email),
        ("WEB", split_values(row.web), row.web_type, normalize_web),
    ]
    for field, values, value_type, normalizer in specs:
        current = existing_multifield(company, field)
        current_keys = {
            normalizer(item.get("VALUE"))
            for item in current
            if normalizer(item.get("VALUE"))
        }
        for value in values:
            normalized = normalizer(value)
            if field == "PHONE" and len(normalized) < 6:
                errors.append(f"Некорректный телефон: {value}")
                continue
            if field == "EMAIL" and not valid_email(value):
                errors.append(f"Некорректный email: {value}")
                continue
            if not normalized or normalized in current_keys:
                continue
            additions.append(
                {"typeId": field, "valueType": value_type or "WORK", "value": value}
            )
            current_keys.add(normalized)
            changes.append(f"{field} + {value}")
    return additions, changes, errors


def values_equal(current: Any, desired: str) -> bool:
    if isinstance(current, list):
        return [clean(x) for x in current] == [desired]
    if isinstance(current, bool):
        return ("Y" if current else "N") == desired.upper()
    return clean(current) == desired


def plan_custom_fields(
    company: dict[str, Any],
    row: SheetRow,
) -> tuple[dict[str, str], list[str]]:
    updates: dict[str, str] = {}
    changes: list[str] = []
    for field, desired in row.custom_fields.items():
        if values_equal(company.get(field), desired):
            continue
        updates[field] = desired
        changes.append(f"{field}: {clean(company.get(field)) or '∅'} → {desired}")
    return updates, changes


def verify_company(
    company: dict[str, Any],
    fm: list[dict[str, str]],
    custom_fields: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    for item in fm:
        field = item["typeId"]
        target = item["value"]
        normalizer = {
            "PHONE": normalize_phone,
            "EMAIL": normalize_email,
            "WEB": normalize_web,
        }[field]
        current = {
            normalizer(x.get("VALUE"))
            for x in existing_multifield(company, field)
        }
        if normalizer(target) not in current:
            errors.append(f"{field} не найден после обновления: {target}")
    for field, desired in custom_fields.items():
        if not values_equal(company.get(field), desired):
            errors.append(f"{field} не подтвердился после обновления")
    return errors


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_reports(
    actions: list[Action],
    mode: str,
    spreadsheet_id: str,
    sheet_name: str,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.status] = counts.get(action.status, 0) + 1
    summary = {
        "mode": mode,
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": sheet_name,
        "total_rows": len(actions),
        "counts_by_status": counts,
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    fields = list(Action.__dataclass_fields__.keys())
    with (OUTPUT_DIR / "actions.csv").open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(action) for action in actions)


def write_sheet_results(
    sheet_client: GoogleSheetsClient,
    sheet_name: str,
    headers: list[str],
    actions: list[Action],
) -> None:
    columns = sheet_client.ensure_columns(sheet_name, headers, RESULT_COLUMNS)
    updates: list[tuple[str, list[list[Any]]]] = []
    for action in actions:
        values = {
            "STATUS": action.status,
            "CHANGES": action.changes,
            "MESSAGE": action.message,
            "BITRIX_COMPANY_ID": action.resolved_company_id,
            "UPDATED_AT": action.updated_at,
        }
        for header, value in values.items():
            col = column_letter(columns[header])
            updates.append((f"'{sheet_name}'!{col}{action.row_number}", [[value]]))
    sheet_client.batch_update(updates)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Безопасное обновление компаний Bitrix24 из Google Sheets"
    )
    parser.add_argument("--spreadsheet", required=True, help="ID или ссылка Google Sheets")
    parser.add_argument("--sheet", default="company_update")
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    args = parser.parse_args()

    spreadsheet_id = normalize_spreadsheet_id(args.spreadsheet)
    actions: list[Action] = []

    try:
        sheet_client = GoogleSheetsClient.from_env(spreadsheet_id)
        rows = sheet_client.read_rows(args.sheet)
        if not rows:
            raise ValueError(f"Лист {args.sheet!r} пуст")
        client = BitrixClient.from_env()
        company_fields = client.call("crm.company.fields") or {}
        if not isinstance(company_fields, dict):
            raise RuntimeError("crm.company.fields вернул неожиданный ответ")
        input_rows, schema_errors = parse_rows(rows, company_fields)
        if schema_errors:
            raise ValueError("; ".join(schema_errors))
    except Exception as exc:
        print(f"ERROR: {sanitize_error(exc)}", file=sys.stderr)
        return 2

    if not input_rows:
        print("ERROR: Нет строк для обработки", file=sys.stderr)
        return 2

    seen_company_ids: set[str] = set()
    for row in input_rows:
        resolved_id = ""
        status = "ERROR"
        changes_text = ""
        message = ""
        timestamp = now_iso()
        try:
            company, error = resolve_company(client, row)
            if error or company is None:
                raise RuntimeError(error or "Компания не найдена")
            resolved_id = clean(company.get("ID"))
            if resolved_id in seen_company_ids:
                raise RuntimeError(
                    f"Компания ID {resolved_id} уже встречалась выше в этом листе"
                )
            seen_company_ids.add(resolved_id)

            fm, fm_changes, fm_errors = plan_multifields(company, row)
            if fm_errors:
                raise RuntimeError("; ".join(fm_errors))
            custom_updates, custom_changes = plan_custom_fields(company, row)
            change_list = fm_changes + custom_changes
            changes_text = "; ".join(change_list)

            warnings: list[str] = []
            if (
                row.name
                and clean(company.get("TITLE"))
                and row.name.casefold() != clean(company.get("TITLE")).casefold()
            ):
                warnings.append(
                    f"Название в таблице отличается от Bitrix: {company.get('TITLE')}"
                )
            if any(header_key(value) in {"address", "адрес"} for value in rows[0]):
                warnings.append(
                    "ADDRESS/Адрес не обновляется: адрес хранится через реквизиты Bitrix24"
                )

            if not change_list:
                status = "SKIP"
                message = "Изменений нет" + (
                    "; " + "; ".join(warnings) if warnings else ""
                )
            elif args.mode == "dry_run":
                status = "DRY_RUN"
                message = "Будет обновлено: " + changes_text + (
                    "; " + "; ".join(warnings) if warnings else ""
                )
            else:
                fields: dict[str, Any] = dict(custom_updates)
                if fm:
                    fields["fm"] = fm
                client.call(
                    "crm.item.update",
                    {
                        "entityTypeId": ENTITY_TYPE_COMPANY,
                        "id": int(resolved_id),
                        "fields": fields,
                        "useOriginalUfNames": "Y",
                    },
                )
                fresh = client.call("crm.company.get", {"id": int(resolved_id)})
                if not isinstance(fresh, dict):
                    raise RuntimeError("Не удалось перечитать компанию после обновления")
                verify_errors = verify_company(fresh, fm, custom_updates)
                if verify_errors:
                    raise RuntimeError(
                        "Проверка после записи не пройдена: "
                        + "; ".join(verify_errors)
                    )
                status = "UPDATED"
                message = "Обновлено и проверено" + (
                    "; " + "; ".join(warnings) if warnings else ""
                )
        except Exception as exc:
            message = sanitize_error(exc)

        actions.append(
            Action(
                row_number=row.row_number,
                source_company_id=row.company_id,
                origin_id=row.origin_id,
                name=row.name,
                resolved_company_id=resolved_id,
                status=status,
                changes=changes_text,
                message=message,
                updated_at=timestamp,
            )
        )

    try:
        write_reports(actions, args.mode, spreadsheet_id, args.sheet)
        write_sheet_results(sheet_client, args.sheet, rows[0], actions)
    except Exception as exc:
        print(
            f"ERROR: не удалось записать отчет: {sanitize_error(exc)}",
            file=sys.stderr,
        )
        return 2

    counts: dict[str, int] = {}
    for action in actions:
        counts[action.status] = counts.get(action.status, 0) + 1
    print(json.dumps({"mode": args.mode, "counts": counts}, ensure_ascii=False))
    return 1 if counts.get("ERROR") else 0


if __name__ == "__main__":
    raise SystemExit(main())
