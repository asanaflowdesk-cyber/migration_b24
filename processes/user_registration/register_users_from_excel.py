from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# Reuse the project's Bitrix REST client. It already supports retries and the
# Windows corporate certificate store through truststore.
from common.bitrix import BitrixClient
from google_sheets import GoogleSheetsClient


DEFAULT_SPREADSHEET_ID = "1WuRHHyQm5lHxDlW81m4P0oZJ6X_SDYj1bN-NOx8aM2k"
DEFAULT_INPUT_SHEET = "new_users_add"
DEFAULT_USER_LIST_SHEET = "user_list"
OUTPUT_DIR = Path("output")


def normalize(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    return " ".join(text.split())


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def created_user_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("ID", "id", "user", "result"):
            if key in value:
                found = created_user_id(value[key])
                if found:
                    return found
        return ""
    raw = clean(value)
    return raw if raw.isdigit() and int(raw) > 0 else ""


def normalize_email(value: Any) -> str:
    return clean(value).casefold()


def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))


@dataclass
class InputUser:
    row_number: int
    last_name: str
    name: str
    second_name: str
    email: str
    department: str
    department_id: str
    source_user_id: str = ""
    position: str = ""
    work_phone: str = ""
    mobile_phone: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(x for x in (self.last_name, self.name, self.second_name) if x)


@dataclass
class Action:
    row_number: int
    full_name: str
    email: str
    department: str
    department_id: str
    status: str
    target_user_id: str = ""
    message: str = ""


GOOGLE_HEADERS = {
    "source_user_id": "NEW_id",
    "department_id": "Филиал",
    "full_name": "Ф.И.О",
    "position": "Должность",
    "email": "электронный адрес",
    "work_phone": "внутренний номер",
    "mobile_phone": "мобильный +",
}


def _sheet_header_key(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яәіңғүұқөһ]+", " ", normalize(value)).strip()


def read_users_from_sheet(client: GoogleSheetsClient, sheet_name: str) -> list[InputUser]:
    rows = client.read_rows(sheet_name)
    if not rows:
        raise ValueError(f"Лист {sheet_name!r} пуст")
    headers = {_sheet_header_key(value): index for index, value in enumerate(rows[0])}
    columns: dict[str, int] = {}
    for field, expected in GOOGLE_HEADERS.items():
        key = _sheet_header_key(expected)
        if key not in headers:
            raise ValueError(
                f"В листе {sheet_name!r} отсутствует обязательная колонка {expected!r}"
            )
        columns[field] = headers[key]

    def value(cells: list[str], field: str) -> str:
        index = columns[field]
        return clean(cells[index]) if index < len(cells) else ""

    result: list[InputUser] = []
    for row_number, cells in enumerate(rows[1:], start=2):
        full_name = value(cells, "full_name")
        email = value(cells, "email")
        department_id = value(cells, "department_id")
        source_user_id = value(cells, "source_user_id")
        if not any((full_name, email, department_id, source_user_id)):
            continue
        parts = full_name.split()
        result.append(
            InputUser(
                row_number=row_number,
                last_name=parts[0] if parts else "",
                name=parts[1] if len(parts) > 1 else "",
                second_name=" ".join(parts[2:]) if len(parts) > 2 else "",
                email=email,
                department="",
                department_id=department_id,
                source_user_id=source_user_id,
                position=value(cells, "position"),
                work_phone=value(cells, "work_phone"),
                mobile_phone=value(cells, "mobile_phone"),
            )
        )
    return result


def user_list_name_rows(client: GoogleSheetsClient, sheet_name: str) -> dict[str, int]:
    rows = client.read_rows(sheet_name)
    if not rows:
        raise ValueError(f"Лист {sheet_name!r} пуст")
    headers = {_sheet_header_key(value): index for index, value in enumerate(rows[0])}
    full_name_index = headers.get("full name")
    if full_name_index is None:
        raise ValueError(f"В листе {sheet_name!r} отсутствует колонка 'full_name'")
    result: dict[str, int] = {}
    for row_number, cells in enumerate(rows[1:], start=2):
        if full_name_index >= len(cells):
            continue
        key = normalize(cells[full_name_index])
        if not key:
            continue
        if key in result:
            raise ValueError(f"В {sheet_name!r} ФИО {cells[full_name_index]!r} указано дважды")
        result[key] = row_number
    return result


def write_user_back(
    client: GoogleSheetsClient,
    *,
    input_sheet: str,
    user_list_sheet: str,
    user_list_rows: dict[str, int],
    row: InputUser,
    user_id: str,
    department_name: str,
) -> None:
    user_list_row = user_list_rows.get(normalize(row.full_name))
    updates: list[tuple[str, list[list[Any]]]] = [
        (f"'{input_sheet}'!A{row.row_number}", [[int(user_id)]])
    ]
    if user_list_row is not None:
        updates.extend(
            [
                (f"'{user_list_sheet}'!A{user_list_row}", [[int(user_id)]]),
                (
                    f"'{user_list_sheet}'!C{user_list_row}:D{user_list_row}",
                    [[int(row.department_id), department_name]],
                ),
            ]
        )
        client.batch_update(updates)
        return

    client.batch_update(updates)
    client.append_row(
        user_list_sheet,
        [int(user_id), row.full_name, int(row.department_id), department_name, row.position],
    )
    user_list_rows[normalize(row.full_name)] = max(user_list_rows.values(), default=1) + 1


def user_full_name(user: dict[str, Any]) -> str:
    return " ".join(
        x for x in (clean(user.get("LAST_NAME")), clean(user.get("NAME")), clean(user.get("SECOND_NAME"))) if x
    )


def write_reports(actions: list[Action], mode: str, source_file: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "mode": mode,
        "source_file": source_file,
        "total_rows": len(actions),
        "counts_by_status": {},
    }
    for action in actions:
        summary["counts_by_status"][action.status] = summary["counts_by_status"].get(action.status, 0) + 1

    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fields = list(Action.__dataclass_fields__.keys())
    def literal_row(action: Action) -> dict[str, Any]:
        row = asdict(action)
        return {
            key: ("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value)
            for key, value in row.items()
        }

    with (OUTPUT_DIR / "actions.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(literal_row(action) for action in actions)

    errors = [action for action in actions if action.status == "ERROR"]
    with (OUTPUT_DIR / "errors.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(literal_row(action) for action in errors)


def main() -> int:
    parser = argparse.ArgumentParser(description="Регистрация пользователей Bitrix24 из Google Sheets")
    parser.add_argument(
        "--spreadsheet-id",
        default=os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID),
    )
    parser.add_argument(
        "--sheet",
        default=os.getenv("GOOGLE_SHEETS_NEW_USERS_TAB", DEFAULT_INPUT_SHEET),
    )
    parser.add_argument(
        "--user-list-sheet",
        default=os.getenv("GOOGLE_SHEETS_USER_LIST_TAB", DEFAULT_USER_LIST_SHEET),
    )
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    args = parser.parse_args()

    actions: list[Action] = []

    try:
        sheet_client = GoogleSheetsClient.from_env(
            args.spreadsheet_id,
            require_write=args.mode == "apply",
        )
        input_users = read_users_from_sheet(sheet_client, args.sheet)
        current_user_list_rows = user_list_name_rows(sheet_client, args.user_list_sheet)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not input_users:
        print("ERROR: В new_users_add нет пользователей для обработки.", file=sys.stderr)
        return 2

    client = BitrixClient.from_env()
    departments = client.list_all("department.get", {})
    target_users = client.list_all("user.get", {})

    departments_by_id = {clean(item.get("ID")): item for item in departments if clean(item.get("ID"))}
    departments_by_name: dict[str, list[dict[str, Any]]] = {}
    for department in departments:
        departments_by_name.setdefault(normalize(department.get("NAME")), []).append(department)

    users_by_email = {
        normalize_email(item.get("EMAIL")): item
        for item in target_users
        if normalize_email(item.get("EMAIL"))
    }
    users_by_id = {
        clean(item.get("ID")): item
        for item in target_users
        if clean(item.get("ID"))
    }
    users_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in target_users:
        full_name = normalize(user_full_name(item))
        if full_name:
            users_by_name.setdefault(full_name, []).append(item)

    seen_emails: set[str] = set()
    seen_names: set[str] = set()

    for row in input_users:
        email = normalize_email(row.email)
        action_base = {
            "row_number": row.row_number,
            "full_name": row.full_name,
            "email": row.email,
            "department": row.department,
            "department_id": row.department_id,
        }

        if not row.last_name or not row.name:
            actions.append(Action(**action_base, status="ERROR", message="Не заполнены фамилия или имя"))
            continue
        if row.source_user_id and (
            not row.source_user_id.isdigit() or int(row.source_user_id) <= 0
        ):
            actions.append(Action(**action_base, status="ERROR", message="Некорректный NEW_id"))
            continue
        if not valid_email(email):
            actions.append(Action(**action_base, status="ERROR", message="Некорректный email"))
            continue
        if email in seen_emails:
            actions.append(Action(**action_base, status="ERROR", message="Повтор email в new_users_add"))
            continue
        seen_emails.add(email)
        normalized_full_name = normalize(row.full_name)
        if normalized_full_name in seen_names:
            actions.append(Action(**action_base, status="ERROR", message="Повтор ФИО в new_users_add с другим email"))
            continue
        seen_names.add(normalized_full_name)

        existing_by_id = users_by_id.get(row.source_user_id) if row.source_user_id else None
        if row.source_user_id and existing_by_id is None:
            actions.append(
                Action(**action_base, status="ERROR", message="NEW_id не найден в Bitrix24")
            )
            continue
        if existing_by_id and normalize_email(existing_by_id.get("EMAIL")) != email:
            actions.append(
                Action(
                    **action_base,
                    status="ERROR",
                    target_user_id=row.source_user_id,
                    message="NEW_id принадлежит пользователю с другим email",
                )
            )
            continue
        existing = existing_by_id or users_by_email.get(email)
        if existing:
            target_id = clean(existing.get("ID"))
            department = departments_by_id.get(row.department_id)
            if department is None:
                actions.append(
                    Action(**action_base, status="ERROR", message="ID подразделения не найден в Bitrix24")
                )
                continue
            resolved_department_name = clean(department.get("NAME"))
            action_base["department"] = resolved_department_name
            if args.mode == "apply":
                try:
                    write_user_back(
                        sheet_client,
                        input_sheet=args.sheet,
                        user_list_sheet=args.user_list_sheet,
                        user_list_rows=current_user_list_rows,
                        row=row,
                        user_id=target_id,
                        department_name=resolved_department_name,
                    )
                except Exception as exc:
                    actions.append(
                        Action(
                            **action_base,
                            status="ERROR",
                            target_user_id=target_id,
                            message=f"Пользователь существует, но ID не записан в Google Sheets: {exc}",
                        )
                    )
                    continue
            actions.append(
                Action(
                    **action_base,
                    status="SKIP",
                    target_user_id=target_id,
                    message=(
                        "Пользователь существует; ID записан в таблицы"
                        if args.mode == "apply"
                        else "Пользователь с таким email уже существует"
                    ),
                )
            )
            continue

        same_name = users_by_name.get(normalize(row.full_name), [])
        if same_name:
            existing_emails = ", ".join(clean(item.get("EMAIL")) for item in same_name)
            actions.append(
                Action(
                    **action_base,
                    status="ERROR",
                    message=f"В Bitrix уже найдено такое ФИО с другим email: {existing_emails}",
                )
            )
            continue

        department: dict[str, Any] | None = None
        if row.department_id:
            department = departments_by_id.get(row.department_id)
            if department is None:
                actions.append(
                    Action(**action_base, status="ERROR", message="ID подразделения не найден в Bitrix24")
                )
                continue
        else:
            matches = departments_by_name.get(normalize(row.department), [])
            if len(matches) == 1:
                department = matches[0]
            elif not matches:
                actions.append(
                    Action(**action_base, status="ERROR", message="Подразделение не найдено по точному названию")
                )
                continue
            else:
                ids = ", ".join(clean(item.get("ID")) for item in matches)
                actions.append(
                    Action(
                        **action_base,
                        status="ERROR",
                        message=f"Найдено несколько подразделений с таким названием. Укажите ID: {ids}",
                    )
                )
                continue

        resolved_department_id = clean(department.get("ID"))
        resolved_department_name = clean(department.get("NAME"))
        action_base["department"] = resolved_department_name
        action_base["department_id"] = resolved_department_id

        fields: dict[str, Any] = {
            "EMAIL": row.email.strip(),
            "NAME": row.name.strip(),
            "LAST_NAME": row.last_name.strip(),
            "UF_DEPARTMENT": [int(resolved_department_id)],
        }
        if row.second_name:
            fields["SECOND_NAME"] = row.second_name.strip()
        if row.position:
            fields["WORK_POSITION"] = row.position.strip()
        if row.work_phone:
            fields["UF_PHONE_INNER"] = row.work_phone.strip()
        if row.mobile_phone:
            fields["PERSONAL_MOBILE"] = row.mobile_phone.strip()

        if args.mode == "dry_run":
            actions.append(
                Action(**action_base, status="DRY_RUN", message="Готов к регистрации и отправке приглашения")
            )
            continue

        try:
            result = client.call("user.add", fields)
            target_id = created_user_id(result)
            if not target_id:
                raise RuntimeError(f"user.add не вернул положительный ID: {result!r}")
            try:
                write_user_back(
                    sheet_client,
                    input_sheet=args.sheet,
                    user_list_sheet=args.user_list_sheet,
                    user_list_rows=current_user_list_rows,
                    row=row,
                    user_id=target_id,
                    department_name=resolved_department_name,
                )
            except Exception as exc:
                actions.append(
                    Action(
                        **action_base,
                        status="ERROR",
                        target_user_id=target_id,
                        message=f"Пользователь создан, но ID не записан в Google Sheets: {exc}",
                    )
                )
                continue
            actions.append(
                Action(
                    **action_base,
                    status="OK",
                    target_user_id=target_id,
                    message="Пользователь создан; ID записан в new_users_add и user_list",
                )
            )
        except Exception as exc:
            actions.append(Action(**action_base, status="ERROR", message=str(exc)))

    write_reports(actions, args.mode, f"Google Sheets {args.spreadsheet_id}/{args.sheet}")

    for action in actions:
        print(
            f"{action.status:7} | row {action.row_number:>3} | {action.email:<40} | "
            f"{action.department} ({action.department_id}) | {action.message}"
        )

    errors = sum(1 for action in actions if action.status == "ERROR")
    print(f"\nProcessed: {len(actions)}; errors: {errors}; mode: {args.mode}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
