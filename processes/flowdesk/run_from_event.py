from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from common.bitrix import BitrixClient


FIELD_NAMES = (
    "flowdesk_request_1",
    "flowdesk_request",
    "flowdesk_department",
    "flowdesk_attachment",
    "resp_id",
    "flowdesk_email",
    "flowdesk_complaint_subproduct",
    "flowdesk_complaint_description",
    "flowdesk_complaint_type",
    "flowdesk_complaint_product",
    "Datetime",
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(clean_text(item) for item in value if clean_text(item)).strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).strip()
    return str(value).strip()


def is_placeholder(field_name: str, value: str) -> bool:
    if not value:
        return False

    compact = re.sub(r"[\s`'\"<>\[\]{}()]+", "", value).casefold()
    field = field_name.casefold()

    # Botmother/Albato may return the variable name itself when the optional
    # field was not filled. Also cover common template wrappers.
    return compact in {
        field,
        f"${field}",
        f"${{{field}}}",
    }


def clean_field(field_name: str, value: Any) -> str:
    text = clean_text(value)
    if is_placeholder(field_name, text):
        return ""
    return text


def normalize_email(value: str) -> str:
    text = value.strip()

    # Defensive support for a markdown-rendered mailto value.
    match = re.fullmatch(r"\[([^\]]+)\]\(mailto:?[^)]*\)", text, flags=re.IGNORECASE)
    if match:
        text = match.group(1)
    elif text.casefold().startswith("mailto:"):
        text = text[7:]

    return text.strip().casefold()


def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))


def parse_payload(raw: str) -> dict[str, str]:
    try:
        source = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FLOWDESK_PAYLOAD is not valid JSON: {exc}") from exc

    if not isinstance(source, dict):
        raise ValueError("FLOWDESK_PAYLOAD must be a JSON object")

    return {name: clean_field(name, source.get(name)) for name in FIELD_NAMES}


def join_pair(first: str, second: str) -> str:
    values = [value for value in (first, second) if value]
    return ". ".join(values)


def build_title(data: dict[str, str]) -> str:
    title = " ".join(
        value
        for value in (
            data["flowdesk_request_1"],
            data["flowdesk_request"],
            data["flowdesk_department"],
        )
        if value
    ).strip()
    if not title:
        raise ValueError("Не удалось сформировать название задачи: все три поля названия пустые")
    return title


def build_description(data: dict[str, str]) -> str:
    top = [
        join_pair(data["flowdesk_request"], data["flowdesk_request_1"]),
        data["flowdesk_department"],
        data["flowdesk_complaint_type"],
        join_pair(data["flowdesk_complaint_product"], data["flowdesk_complaint_subproduct"]),
        data["flowdesk_complaint_description"],
    ]

    bottom = [
        data["Datetime"],
        data["flowdesk_attachment"],
    ]

    top_text = "\n".join(value for value in top if value).strip()
    bottom_text = "\n".join(value for value in bottom if value).strip()

    # One mandatory empty line between the body and Datetime/attachment block.
    if bottom_text:
        return f"{top_text}\n\n{bottom_text}" if top_text else f"\n\n{bottom_text}"
    return f"{top_text}\n\n"


def build_deadline(datetime_text: str, utc_offset_hours: int) -> str:
    tz = timezone(timedelta(hours=utc_offset_hours))
    base: datetime

    if datetime_text:
        formats = (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        )
        for fmt in formats:
            try:
                base = datetime.strptime(datetime_text, fmt).replace(tzinfo=tz)
                break
            except ValueError:
                continue
        else:
            try:
                parsed = datetime.fromisoformat(datetime_text)
                base = parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
                base = base.astimezone(tz)
            except ValueError:
                print(
                    f"WARNING: Datetime {datetime_text!r} не распознан; дедлайн считается от времени запуска",
                    file=sys.stderr,
                )
                base = datetime.now(tz)
    else:
        base = datetime.now(tz)

    return (base + timedelta(days=3)).replace(microsecond=0).isoformat()


def find_user_by_email(client: BitrixClient, email: str) -> dict[str, Any] | None:
    rows = client.list_all(
        "user.get",
        {
            "FILTER": {"EMAIL": email},
            "ADMIN_MODE": True,
            "select": ["ID", "EMAIL", "ACTIVE", "NAME", "LAST_NAME"],
        },
    )

    for user in rows:
        if normalize_email(clean_text(user.get("EMAIL"))) == email:
            return user
    return None


def resolve_creator(client: BitrixClient, email_raw: str, invite_department_id: int) -> int:
    email = normalize_email(email_raw)
    if not valid_email(email):
        raise ValueError(f"Некорректный flowdesk_email: {email_raw!r}")

    existing = find_user_by_email(client, email)
    if existing is not None:
        if clean_text(existing.get("ACTIVE")).upper() == "N":
            raise ValueError(
                f"Пользователь {email} уже существует в Bitrix24, но неактивен; повторно приглашать его нельзя"
            )
        return int(clean_text(existing.get("ID")))

    result = client.call(
        "user.add",
        {
            "EMAIL": email,
            "UF_DEPARTMENT": [invite_department_id],
        },
    )

    creator_id = int(clean_text(result))
    if creator_id <= 0:
        raise RuntimeError(f"user.add вернул некорректный ID: {result!r}")

    return creator_id


def resolve_responsible_id(value: str) -> int:
    if not value:
        raise ValueError("resp_id пустой")
    try:
        responsible_id = int(value)
    except ValueError as exc:
        raise ValueError(f"resp_id должен быть числовым ID Bitrix24, получено: {value!r}") from exc
    if responsible_id <= 0:
        raise ValueError(f"resp_id должен быть больше 0, получено: {responsible_id}")
    return responsible_id


def create_task(
    client: BitrixClient,
    *,
    title: str,
    description: str,
    creator_id: int,
    responsible_id: int,
    project_id: int,
    deadline: str,
) -> str:
    result = client.call(
        "tasks.task.add",
        {
            "fields": {
                "TITLE": title,
                "DESCRIPTION": description,
                "CREATED_BY": creator_id,
                "RESPONSIBLE_ID": responsible_id,
                "GROUP_ID": project_id,
                "DEADLINE": deadline,
            }
        },
    )

    if not isinstance(result, dict):
        raise RuntimeError(f"tasks.task.add вернул неожиданный ответ: {result!r}")

    task = result.get("task")
    if not isinstance(task, dict) or not clean_text(task.get("id")):
        raise RuntimeError(f"tasks.task.add не вернул ID задачи: {result!r}")

    return clean_text(task.get("id"))


def main() -> int:
    raw_payload = os.environ.get("FLOWDESK_PAYLOAD", "").strip()
    if not raw_payload:
        print("ERROR: FLOWDESK_PAYLOAD is empty", file=sys.stderr)
        return 2

    try:
        project_id = int(os.environ.get("FLOWDESK_PROJECT_ID", "2"))
        invite_department_id = int(os.environ.get("FLOWDESK_INVITE_DEPARTMENT_ID", "1"))
        utc_offset_hours = int(os.environ.get("FLOWDESK_UTC_OFFSET_HOURS", "5"))

        data = parse_payload(raw_payload)
        title = build_title(data)
        description = build_description(data)
        responsible_id = resolve_responsible_id(data["resp_id"])
        deadline = build_deadline(data["Datetime"], utc_offset_hours)

        client = BitrixClient.from_env()
        creator_id = resolve_creator(client, data["flowdesk_email"], invite_department_id)

        task_id = create_task(
            client,
            title=title,
            description=description,
            creator_id=creator_id,
            responsible_id=responsible_id,
            project_id=project_id,
            deadline=deadline,
        )

        print(f"OK: FlowDesk task created: ID={task_id}")
        print(f"Creator ID: {creator_id}")
        print(f"Responsible ID: {responsible_id}")
        print(f"Project ID: {project_id}")
        print(f"Deadline: {deadline}")
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
