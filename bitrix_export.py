#!/usr/bin/env python3
"""Полная выгрузка основных данных Bitrix24 в один Excel-файл.

Поддерживается запуск локально, в GitHub Codespaces и GitHub Actions.
Авторизация выполняется через входящий webhook Bitrix24.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import urlencode

import requests
import xlsxwriter
from dotenv import load_dotenv


EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_TEXT = 32_767
PAGE_SIZE = 50
BATCH_SIZE = 50
DEFAULT_MAX_PAGES = 20_000
DEFAULT_PROGRESS_EVERY_PAGES = 10


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def scalar(value: Any) -> Any:
    """Преобразует вложенные значения в безопасный для Excel вид."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if len(text) > EXCEL_MAX_TEXT:
        text = text[: EXCEL_MAX_TEXT - 20] + "…[обрезано]"
    return text


def getv(row: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def full_name(user: Mapping[str, Any]) -> str:
    parts = [
        str(getv(user, "LAST_NAME", "lastName", default="")).strip(),
        str(getv(user, "NAME", "name", default="")).strip(),
        str(getv(user, "SECOND_NAME", "secondName", default="")).strip(),
    ]
    name = " ".join(part for part in parts if part)
    return name or str(getv(user, "EMAIL", "email", default="")) or f"ID {getv(user, 'ID', 'id')}"


def flatten_field_catalog(payload: Any) -> list[dict[str, Any]]:
    """Превращает словарь описаний полей Bitrix24 в плоские строки."""
    if isinstance(payload, dict) and "fields" in payload and isinstance(payload["fields"], dict):
        payload = payload["fields"]
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for code, meta in payload.items():
            row: dict[str, Any] = {"FIELD_CODE": code}
            if isinstance(meta, dict):
                row.update(meta)
            else:
                row["VALUE"] = meta
            rows.append(row)
    elif isinstance(payload, list):
        for item in payload:
            rows.append(dict(item) if isinstance(item, dict) else {"VALUE": item})
    return rows


def extract_field_enum_values(payload: Any, field_code: str) -> list[dict[str, Any]]:
    """Извлекает значения перечисления из метаданных поля Bitrix24.

    tasks.task.getFields, например, возвращает STATUS.values как словарь
    {"2": "Ждёт выполнения", ...}. Некоторые методы используют список
    объектов. Сохраняем оба варианта в едином формате.
    """
    source = payload
    if isinstance(source, dict) and isinstance(source.get("fields"), dict):
        source = source["fields"]
    if not isinstance(source, dict):
        return []

    field_meta = source.get(field_code)
    if not isinstance(field_meta, dict):
        return []
    values = field_meta.get("values", field_meta.get("VALUES", {}))

    rows: list[dict[str, Any]] = []
    if isinstance(values, dict):
        for value_id, value_name in values.items():
            if isinstance(value_name, dict):
                row = {"FIELD_CODE": field_code, "VALUE_ID": str(value_id)}
                row.update(value_name)
                row.setdefault("VALUE_NAME", scalar(getv(value_name, "NAME", "name", "VALUE", "value")))
            else:
                row = {
                    "FIELD_CODE": field_code,
                    "VALUE_ID": str(value_id),
                    "VALUE_NAME": scalar(value_name),
                }
            rows.append(row)
    elif isinstance(values, list):
        for position, item in enumerate(values, start=1):
            if isinstance(item, dict):
                value_id = getv(item, "ID", "id", "VALUE_ID", "valueId", default=position)
                value_name = getv(item, "NAME", "name", "VALUE", "value", default="")
                row = {
                    "FIELD_CODE": field_code,
                    "VALUE_ID": str(value_id),
                    "VALUE_NAME": scalar(value_name),
                }
                row.update(item)
            else:
                row = {
                    "FIELD_CODE": field_code,
                    "VALUE_ID": str(position),
                    "VALUE_NAME": scalar(item),
                }
            rows.append(row)
    return rows


def normalize_records(payload: Any, preferred_keys: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Извлекает массив записей из разных форматов ответов REST."""
    current = payload
    for key in preferred_keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
    if isinstance(current, list):
        return [dict(x) if isinstance(x, dict) else {"VALUE": x} for x in current]
    if isinstance(current, dict):
        # Частые контейнеры списочных методов.
        for key in ("items", "tasks", "categories", "groups", "departments", "users"):
            value = current.get(key)
            if isinstance(value, list):
                return [dict(x) if isinstance(x, dict) else {"VALUE": x} for x in value]
        # Одиночный объект не превращаем в набор его значений.
        return [dict(current)]
    if current in (None, ""):
        return []
    return [{"VALUE": current}]


def deep_get(value: Any, path: Sequence[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def query_pairs(prefix: str, value: Any) -> list[tuple[str, str]]:
    """Кодирует вложенные параметры в формат Bitrix24 для batch.cmd."""
    pairs: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{prefix}[{key}]" if prefix else str(key)
            pairs.extend(query_pairs(child, nested))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            child = f"{prefix}[{index}]"
            pairs.extend(query_pairs(child, nested))
    elif value is not None:
        pairs.append((prefix, str(value)))
    return pairs


def field_codes_from_payload(payload: Any) -> list[str]:
    """Возвращает технические коды всех полей из ответа *.fields."""
    current = payload.get("fields", payload) if isinstance(payload, dict) else {}
    if not isinstance(current, dict):
        return []
    return [str(key) for key in current.keys()]


def build_entity_select(payload: Any, extras: Sequence[str] = ()) -> list[str]:
    """Формирует select, включая пользовательские и множественные поля."""
    result: list[str] = []
    seen: set[str] = set()
    for code in [*field_codes_from_payload(payload), *extras]:
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return result or ["*", "UF_*"]


class BitrixAPIError(RuntimeError):
    pass


class RawRecorder:
    """Сохраняет каждый успешный REST-ответ без преобразования."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.counter = 0
        self.index: list[dict[str, Any]] = []

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "call"

    def write(self, method: str, params: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.counter += 1
        filename = f"{self.counter:06d}_{self._safe_name(method)}.json"
        payload = {
            "method": method,
            "params": dict(params),
            "response": dict(response),
        }
        path = self.directory / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
        self.index.append(
            {
                "sequence": self.counter,
                "method": method,
                "file": filename,
                "has_error": "error" in response,
                "total": response.get("total", ""),
                "next": response.get("next", ""),
            }
        )

    def close(self) -> None:
        (self.directory / "index.json").write_text(
            json.dumps(self.index, ensure_ascii=False, separators=(",", ":"), default=str),
            encoding="utf-8",
        )


@dataclass
class ExportLog:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def add(self, dataset: str, method: str, status: str, count: int = 0, error: str = "") -> None:
        self.rows.append(
            {
                "DATASET": dataset,
                "METHOD": method,
                "STATUS": status,
                "ROWS": count,
                "ERROR": error,
            }
        )


class BitrixClient:
    RETRYABLE_ERRORS = {
        "QUERY_LIMIT_EXCEEDED",
        "OPERATION_TIME_LIMIT",
        "TOO_MANY_REQUESTS",
        "INTERNAL_SERVER_ERROR",
    }

    def __init__(
        self,
        webhook_url: str,
        timeout: int = 90,
        delay: float = 0.10,
        max_retries: int = 8,
        recorder: RawRecorder | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        progress_every_pages: int = DEFAULT_PROGRESS_EVERY_PAGES,
    ) -> None:
        webhook_url = webhook_url.strip()
        if not webhook_url:
            raise ValueError("Не задан BITRIX_WEBHOOK_URL")
        if not re.match(r"^https://.+/rest/\d+/[^/]+/?$", webhook_url):
            raise ValueError(
                "BITRIX_WEBHOOK_URL должен быть полным URL входящего webhook вида "
                "https://portal.bitrix24.kz/rest/1/token/"
            )
        self.base_url = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.max_retries = max(1, max_retries)
        self.recorder = recorder
        self.max_pages = max(1, max_pages)
        self.progress_every_pages = max(1, progress_every_pages)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "bitrix24-migration-export/3.0",
            }
        )

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{method}.json"
        payload = dict(params or {})
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 30)
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}; повтор через {wait:.0f} сек.",
                        response=response,
                    )
                response.raise_for_status()
                data = response.json()
                if self.recorder is not None:
                    self.recorder.write(method, payload, data)
                if "error" in data:
                    code = str(data.get("error", "UNKNOWN_ERROR"))
                    description = str(data.get("error_description", ""))
                    last_error = f"{code}: {description}".strip()
                    if code in self.RETRYABLE_ERRORS and attempt < self.max_retries:
                        wait = min(2**attempt, 30)
                        logging.warning(
                            "%s: временная ошибка %s, попытка %s/%s, пауза %s сек.",
                            method, code, attempt, self.max_retries, wait,
                        )
                        time.sleep(wait)
                        continue
                    raise BitrixAPIError(last_error)
                if self.delay:
                    time.sleep(self.delay)
                return data
            except (requests.RequestException, ValueError) as exc:
                last_error = str(exc)
                if attempt >= self.max_retries:
                    break
                wait = min(2**attempt, 30)
                logging.warning(
                    "%s: ошибка запроса, попытка %s/%s, пауза %s сек.: %s",
                    method, attempt, self.max_retries, wait, exc,
                )
                time.sleep(wait)
        raise BitrixAPIError(f"{method}: {last_error}")

    def list_all(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        result_path: Sequence[str] = (),
        start_key: str = "start",
        page_size: int = PAGE_SIZE,
        label: str | None = None,
    ) -> list[dict[str, Any]]:
        """Получает все страницы и защищается от методов, игнорирующих start.

        Некоторые REST-методы возвращают больше 50 строк одним ответом и не
        поддерживают пагинацию. Старая версия повторяла такой ответ бесконечно.
        Здесь повтор страницы определяется до добавления строк.
        """
        base_params = dict(params or {})
        start = 0
        all_rows: list[dict[str, Any]] = []
        seen_starts: set[int] = set()
        seen_pages: set[str] = set()
        dataset_label = label or method

        for page_number in range(1, self.max_pages + 1):
            request_params = dict(base_params)
            request_params[start_key] = start
            data = self.call(method, request_params)
            result = data.get("result")
            if result_path:
                result = deep_get(result, result_path)
            page = normalize_records(result)

            # Не даём методу, который игнорирует start, зациклить экспорт.
            page_signature = hashlib.sha256(
                json.dumps(page, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if page and page_signature in seen_pages:
                logging.warning(
                    "%s: сервер повторил ту же страницу при start=%s; пагинация остановлена без дублей",
                    dataset_label, start,
                )
                break
            seen_pages.add(page_signature)
            all_rows.extend(page)

            total_value = data.get("total")
            if total_value is None and isinstance(data.get("result"), dict):
                total_value = data["result"].get("total")
            try:
                total = int(total_value) if total_value not in (None, "") else None
            except (TypeError, ValueError):
                total = None

            if page_number == 1 or page_number % self.progress_every_pages == 0 or not page:
                total_text = f"/{total}" if total is not None else ""
                logging.info(
                    "%s: страница %s, получено %s, всего %s%s",
                    dataset_label, page_number, len(page), len(all_rows), total_text,
                )

            if total is not None and len(all_rows) >= total:
                break

            next_value = data.get("next")
            if next_value is None and isinstance(data.get("result"), dict):
                next_value = data["result"].get("next")

            if next_value is not None:
                try:
                    next_start = int(next_value)
                except (TypeError, ValueError):
                    next_start = start + page_size
                if next_start in seen_starts or next_start <= start:
                    logging.warning(
                        "%s: некорректный next=%s при start=%s; пагинация остановлена",
                        dataset_label, next_value, start,
                    )
                    break
                seen_starts.add(start)
                start = next_start
                continue

            if not page or len(page) < page_size:
                break
            seen_starts.add(start)
            start += page_size
        else:
            raise BitrixAPIError(
                f"{method}: превышён защитный лимит {self.max_pages} страниц; "
                "проверьте параметры пагинации"
            )

        return all_rows

    def batch(self, calls: Sequence[tuple[str, str, Mapping[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Выполняет до 50 независимых запросов.

        calls: (alias, method, params)
        Возвращает (успешные результаты, ошибки по alias).
        """
        if len(calls) > BATCH_SIZE:
            raise ValueError("В одном batch допускается не более 50 команд")
        cmd: dict[str, str] = {}
        for alias, method, params in calls:
            pairs: list[tuple[str, str]] = []
            for key, value in params.items():
                pairs.extend(query_pairs(str(key), value))
            query = urlencode(pairs, doseq=True)
            cmd[alias] = f"{method}?{query}" if query else method
        data = self.call("batch", {"halt": 0, "cmd": cmd})
        result = data.get("result", {})
        return dict(result.get("result", {})), dict(result.get("result_error", {}))


def chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def export_batch_relations(
    client: BitrixClient,
    source_rows: Sequence[Mapping[str, Any]],
    source_label: str,
    method: str,
    parent_column: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    ids = [str(getv(row, "ID", "id")) for row in source_rows if str(getv(row, "ID", "id"))]
    total_parts = max(1, (len(ids) + BATCH_SIZE - 1) // BATCH_SIZE)
    for part_number, part in enumerate(chunks(ids, BATCH_SIZE), start=1):
        if part_number == 1 or part_number % 10 == 0 or part_number == total_parts:
            logging.info(
                "%s: пакет связей %s/%s, объектов %s/%s",
                source_label, part_number, total_parts, min(part_number * BATCH_SIZE, len(ids)), len(ids),
            )
        calls = [(f"item_{item_id}", method, {"id": item_id}) for item_id in part]
        results, batch_errors = client.batch(calls)
        for alias, payload in results.items():
            item_id = alias.removeprefix("item_")
            records = normalize_records(payload)
            for position, record in enumerate(records, start=1):
                row = {parent_column: item_id, "RELATION_ORDER": position}
                row.update(record)
                output.append(row)
        for alias, error in batch_errors.items():
            errors.append(
                {
                    "DATASET": source_label,
                    "METHOD": method,
                    "ITEM_ID": alias.removeprefix("item_"),
                    "ERROR": scalar(error),
                }
            )
    return output, errors


def collect_communications(entity_name: str, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        entity_id = getv(row, "ID", "id")
        for field_name in ("PHONE", "EMAIL", "WEB", "IM"):
            values = getv(row, field_name, field_name.lower(), default=[])
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except json.JSONDecodeError:
                    values = [{"VALUE": values}]
            if not isinstance(values, list):
                continue
            for position, item in enumerate(values, start=1):
                record = {
                    "ENTITY_TYPE": entity_name,
                    "ENTITY_ID": entity_id,
                    "COMMUNICATION_TYPE": field_name,
                    "POSITION": position,
                }
                if isinstance(item, dict):
                    record.update(item)
                else:
                    record["VALUE"] = item
                result.append(record)
    return result


def enrich_records(
    datasets: MutableMapping[str, list[dict[str, Any]]],
) -> None:
    users = datasets.get("Users", [])
    user_map = {str(getv(u, "ID", "id")): u for u in users}
    departments = datasets.get("Departments", [])
    department_map = {str(getv(d, "ID", "id")): str(getv(d, "NAME", "name")) for d in departments}
    groups = datasets.get("Workgroups", [])
    group_map = {str(getv(g, "ID", "id")): str(getv(g, "NAME", "name")) for g in groups}
    categories = datasets.get("Deal_Categories", [])
    category_map = {str(getv(c, "ID", "id")): str(getv(c, "NAME", "name")) for c in categories}
    statuses = [*datasets.get("CRM_Statuses", []), *datasets.get("CRM_Status_Items", [])]
    task_status_map = {
        str(getv(row, "VALUE_ID", "ID", "id")): str(getv(row, "VALUE_NAME", "NAME", "name"))
        for row in datasets.get("Task_Statuses", [])
    }
    status_map: dict[tuple[str, str], str] = {}
    loose_status_map: dict[str, str] = {}
    for status in statuses:
        entity_id = str(getv(status, "ENTITY_ID", "entityId"))
        status_id = str(getv(status, "STATUS_ID", "statusId", "ID", "id"))
        name = str(getv(status, "NAME", "name"))
        status_map[(entity_id, status_id)] = name
        loose_status_map.setdefault(status_id, name)

    for user in users:
        dep_ids = getv(user, "UF_DEPARTMENT", "ufDepartment", default=[])
        if not isinstance(dep_ids, list):
            dep_ids = [dep_ids] if dep_ids not in (None, "") else []
        user["FULL_NAME"] = full_name(user)
        user["DEPARTMENT_NAMES"] = "; ".join(
            department_map.get(str(dep_id), f"ID {dep_id}") for dep_id in dep_ids
        )

    def add_user(row: dict[str, Any], id_fields: Sequence[str], prefix: str) -> None:
        user_id = str(getv(row, *id_fields))
        if not user_id:
            return
        user = user_map.get(user_id, {})
        row[f"{prefix}_NAME"] = full_name(user) if user else f"ID {user_id}"
        row[f"{prefix}_EMAIL"] = getv(user, "EMAIL", "email") if user else ""
        row[f"{prefix}_DEPARTMENTS"] = user.get("DEPARTMENT_NAMES", "") if user else ""

    for dataset_name in ("Leads", "Deals", "Contacts", "Companies", "CRM_Activities"):
        for row in datasets.get(dataset_name, []):
            add_user(row, ("ASSIGNED_BY_ID", "assignedById", "RESPONSIBLE_ID", "responsibleId"), "ASSIGNED_BY")
            add_user(row, ("CREATED_BY_ID", "createdById", "AUTHOR_ID", "authorId"), "CREATED_BY")
            add_user(row, ("MODIFY_BY_ID", "modifyById", "EDITOR_ID", "editorId"), "MODIFIED_BY")

    for row in datasets.get("Leads", []):
        status_id = str(getv(row, "STATUS_ID", "statusId"))
        row["STATUS_NAME"] = status_map.get(("STATUS", status_id), loose_status_map.get(status_id, ""))

    for row in datasets.get("Deals", []):
        category_id = str(getv(row, "CATEGORY_ID", "categoryId", default="0"))
        stage_id = str(getv(row, "STAGE_ID", "stageId"))
        row["CATEGORY_NAME"] = category_map.get(category_id, "Основная воронка" if category_id in {"", "0"} else "")
        entity_id = "DEAL_STAGE" if category_id in {"", "0"} else f"DEAL_STAGE_{category_id}"
        row["STAGE_NAME"] = status_map.get((entity_id, stage_id), loose_status_map.get(stage_id, ""))

    for row in datasets.get("Tasks", []):
        add_user(row, ("RESPONSIBLE_ID", "responsibleId"), "RESPONSIBLE")
        add_user(row, ("CREATED_BY", "createdBy", "CREATED_BY_ID", "createdById"), "CREATED_BY")
        add_user(row, ("CHANGED_BY", "changedBy", "CHANGED_BY_ID", "changedById"), "CHANGED_BY")
        group_id = str(getv(row, "GROUP_ID", "groupId"))
        row["GROUP_NAME"] = group_map.get(group_id, "")
        task_status_id = str(getv(row, "STATUS", "status"))
        row["STATUS_NAME"] = task_status_map.get(task_status_id, "")

    for row in datasets.get("Workgroups", []):
        add_user(row, ("OWNER_ID", "ownerId"), "OWNER")


def split_workgroups(datasets: MutableMapping[str, list[dict[str, Any]]]) -> None:
    rows = datasets.get("Workgroups", [])
    datasets["Projects"] = []
    datasets["Groups"] = []
    datasets["Scrum"] = []
    datasets["Collabs"] = []
    for row in rows:
        row_type = str(getv(row, "TYPE", "type")).lower()
        project_flag = str(getv(row, "PROJECT", "project")).upper()
        if row_type == "scrum":
            datasets["Scrum"].append(dict(row))
        elif row_type == "collab":
            datasets["Collabs"].append(dict(row))
        elif row_type == "project" or project_flag == "Y":
            datasets["Projects"].append(dict(row))
        else:
            datasets["Groups"].append(dict(row))


def build_clients_index(datasets: MutableMapping[str, list[dict[str, Any]]]) -> None:
    rows: list[dict[str, Any]] = []
    for contact in datasets.get("Contacts", []):
        rows.append(
            {
                "CLIENT_TYPE": "CONTACT",
                "CLIENT_ID": getv(contact, "ID", "id"),
                "CLIENT_NAME": " ".join(
                    str(getv(contact, key, default="")).strip()
                    for key in ("LAST_NAME", "NAME", "SECOND_NAME")
                    if str(getv(contact, key, default="")).strip()
                ),
                "COMPANY_ID": getv(contact, "COMPANY_ID", "companyId"),
                "ASSIGNED_BY_ID": getv(contact, "ASSIGNED_BY_ID", "assignedById"),
                "ASSIGNED_BY_NAME": getv(contact, "ASSIGNED_BY_NAME"),
                "PHONE": getv(contact, "PHONE", "phone"),
                "EMAIL": getv(contact, "EMAIL", "email"),
            }
        )
    for company in datasets.get("Companies", []):
        rows.append(
            {
                "CLIENT_TYPE": "COMPANY",
                "CLIENT_ID": getv(company, "ID", "id"),
                "CLIENT_NAME": getv(company, "TITLE", "title"),
                "COMPANY_ID": getv(company, "ID", "id"),
                "ASSIGNED_BY_ID": getv(company, "ASSIGNED_BY_ID", "assignedById"),
                "ASSIGNED_BY_NAME": getv(company, "ASSIGNED_BY_NAME"),
                "PHONE": getv(company, "PHONE", "phone"),
                "EMAIL": getv(company, "EMAIL", "email"),
            }
        )
    datasets["Clients_Index"] = rows


SHEET_DESCRIPTIONS = {
    "Leads": "Все лиды, пользовательские поля и расшифровка ответственных/статусов",
    "Deals": "Все сделки, пользовательские поля и расшифровка ответственных/воронок/стадий",
    "Contacts": "Все контакты",
    "Companies": "Все компании",
    "Clients_Index": "Сводный индекс клиентов: контакты и компании",
    "Users": "Пользователи и менеджеры с ID и подразделениями",
    "Departments": "Структура подразделений",
    "Workgroups": "Все рабочие группы, проекты, скрамы и коллабы",
    "Projects": "Только проекты",
    "Groups": "Только рабочие группы",
    "Scrum": "Только скрам-команды",
    "Collabs": "Только коллабы",
    "Tasks": "Все доступные задачи и участники с расшифровкой статуса",
    "Task_Statuses": "Полный справочник статусов задач из tasks.task.getFields",
    "Task_Stages": "Стадии канбана задач по проектам и группам",
    "CRM_Status_Types": "Все типы CRM-справочников ENTITY_ID",
    "CRM_Statuses": "Все CRM-справочники статусов, стадий, источников и типов",
    "CRM_UserField_Config": "Полные настройки пользовательских полей CRM для восстановления",
    "Lead_UserFields": "Настройки пользовательских полей лидов",
    "Deal_UserFields": "Настройки пользовательских полей сделок",
    "Contact_UserFields": "Настройки пользовательских полей контактов",
    "Company_UserFields": "Настройки пользовательских полей компаний",
    "Requisite_UserFields": "Настройки пользовательских полей реквизитов",
    "Task_UserFields": "Настройки пользовательских полей задач",
    "User_CustomFields": "Настройки пользовательских полей пользователей",
    "Deal_Categories": "Воронки сделок",
    "Lead_Fields": "Справочник полей лидов",
    "Deal_Fields": "Справочник полей сделок",
    "Contact_Fields": "Справочник полей контактов",
    "Company_Fields": "Справочник полей компаний",
    "Task_Fields": "Справочник полей задач",
    "User_Fields": "Справочник полей пользователей",
    "Department_Fields": "Справочник полей подразделений",
    "Activity_Fields": "Справочник полей CRM-дел",
    "Requisite_Fields": "Справочник полей реквизитов",
    "Address_Fields": "Справочник полей адресов",
    "BankDetail_Fields": "Справочник полей банковских реквизитов",
    "Deal_Contacts": "Все связи сделка–контакт, включая дополнительные контакты",
    "Lead_Contacts": "Все связи лид–контакт",
    "Contact_Companies": "Все связи контакт–компания",
    "Deal_Products": "Товарные позиции сделок",
    "Lead_Products": "Товарные позиции лидов",
    "CRM_Activities": "Дела CRM: звонки, встречи, письма и другие активности",
    "Timeline_Comments": "Комментарии таймлайна лидов, сделок, контактов и компаний",
    "Requisites": "Реквизиты контактов и компаний",
    "Addresses": "Адреса реквизитов и CRM-сущностей",
    "Bank_Details": "Банковские реквизиты",
    "Requisite_Presets": "Шаблоны реквизитов",
    "Requisite_Links": "Связи реквизитов с CRM-объектами",
    "Smart_Process_Types": "Настройки смарт-процессов",
    "CRM_Communications": "Телефоны, email, сайты и мессенджеры в нормализованном виде",
    "Methods": "REST-методы, доступные текущему webhook",
    "Errors": "Ошибки и ограничения прав при выгрузке",
}


class ExcelWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.workbook = xlsxwriter.Workbook(path)
        self.workbook.set_properties(
            {
                "title": "Bitrix24 full export",
                "subject": "Migration inventory",
                "author": "Bitrix24 export script",
                "company": "",
                "comments": "Generated via Bitrix24 REST API",
            }
        )
        self.header = self.workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#1F4E78",
                "border": 1,
                "align": "center",
                "valign": "vcenter",
                "text_wrap": True,
            }
        )
        self.text = self.workbook.add_format({"valign": "top"})
        self.wrap = self.workbook.add_format({"valign": "top", "text_wrap": True})
        self.integer = self.workbook.add_format({"num_format": "0", "valign": "top"})
        self.decimal = self.workbook.add_format({"num_format": "0.00", "valign": "top"})
        self.ok = self.workbook.add_format({"bg_color": "#E2F0D9"})
        self.warn = self.workbook.add_format({"bg_color": "#FFF2CC"})
        self.bad = self.workbook.add_format({"bg_color": "#FCE4D6"})

    @staticmethod
    def safe_sheet_name(name: str, suffix: str = "") -> str:
        clean = re.sub(r"[\[\]:*?/\\]", "_", name)
        return (clean[: 31 - len(suffix)] + suffix)[:31]

    @staticmethod
    def ordered_columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
        priority = [
            "ID",
            "id",
            "TITLE",
            "title",
            "NAME",
            "name",
            "CLIENT_TYPE",
            "CLIENT_ID",
            "ENTITY_TYPE",
            "ENTITY_ID",
            "DEAL_ID",
            "LEAD_ID",
            "CONTACT_ID",
            "COMPANY_ID",
            "GROUP_ID",
            "CATEGORY_ID",
            "CATEGORY_NAME",
            "STAGE_ID",
            "STAGE_NAME",
            "STATUS_ID",
            "STATUS_NAME",
            "ASSIGNED_BY_ID",
            "ASSIGNED_BY_NAME",
            "RESPONSIBLE_ID",
            "RESPONSIBLE_NAME",
        ]
        found: OrderedDict[str, None] = OrderedDict()
        for key in priority:
            if any(key in row for row in rows):
                found[key] = None
        for row in rows:
            for key in row.keys():
                found.setdefault(str(key), None)
        return list(found.keys())

    def write_dataset(self, base_name: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
        if not rows:
            sheet_name = self.safe_sheet_name(base_name)
            ws = self.workbook.add_worksheet(sheet_name)
            ws.write(0, 0, "Нет доступных записей или недостаточно прав", self.warn)
            ws.set_column(0, 0, 55)
            return [sheet_name]

        max_data_rows = EXCEL_MAX_ROWS - 1
        created: list[str] = []
        for part_number, start in enumerate(range(0, len(rows), max_data_rows), start=1):
            part = rows[start : start + max_data_rows]
            suffix = "" if part_number == 1 else f"_{part_number}"
            sheet_name = self.safe_sheet_name(base_name, suffix)
            created.append(sheet_name)
            ws = self.workbook.add_worksheet(sheet_name)
            columns = self.ordered_columns(part)
            for col, key in enumerate(columns):
                ws.write(0, col, key, self.header)
            widths = [len(key) for key in columns]
            for row_index, record in enumerate(part, start=1):
                for col, key in enumerate(columns):
                    value = scalar(record.get(key, ""))
                    if isinstance(value, int) and not isinstance(value, bool):
                        ws.write_number(row_index, col, value, self.integer)
                    elif isinstance(value, float):
                        ws.write_number(row_index, col, value, self.decimal)
                    else:
                        text = str(value)
                        ws.write_string(row_index, col, text, self.wrap if len(text) > 80 else self.text)
                        widths[col] = min(max(widths[col], min(len(text), 60)), 60)
            ws.freeze_panes(1, 0)
            ws.autofilter(0, 0, len(part), max(0, len(columns) - 1))
            ws.set_row(0, 32)
            for col, width in enumerate(widths):
                header = columns[col].upper()
                max_width = 60 if any(token in header for token in ("DESCRIPTION", "COMMENTS", "JSON", "VALUE")) else 38
                ws.set_column(col, col, min(max(width + 2, 9), max_width))
        return created

    def write_summary(
        self,
        datasets: Mapping[str, Sequence[Mapping[str, Any]]],
        export_log: ExportLog,
        generated_at: str,
        portal_hint: str,
    ) -> None:
        ws = self.workbook.add_worksheet("00_Summary")
        title = self.workbook.add_format({"bold": True, "font_size": 16, "font_color": "#1F4E78"})
        label = self.workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
        value = self.workbook.add_format({"border": 1})
        ws.write("A1", "Bitrix24 — полная инвентаризационная выгрузка", title)
        ws.write("A3", "Сформировано", label)
        ws.write("B3", generated_at, value)
        ws.write("A4", "Портал", label)
        ws.write("B4", portal_hint, value)
        ws.write("A6", "Лист", self.header)
        ws.write("B6", "Строк", self.header)
        ws.write("C6", "Назначение", self.header)
        row_index = 6
        for name, rows in datasets.items():
            ws.write(row_index, 0, name)
            ws.write_number(row_index, 1, len(rows), self.integer)
            ws.write(row_index, 2, SHEET_DESCRIPTIONS.get(name, ""), self.wrap)
            row_index += 1
        row_index += 2
        ws.write(row_index, 0, "Журнал выполнения", title)
        row_index += 2
        headers = ["DATASET", "METHOD", "STATUS", "ROWS", "ERROR"]
        for col, key in enumerate(headers):
            ws.write(row_index, col, key, self.header)
        row_index += 1
        for item in export_log.rows:
            for col, key in enumerate(headers):
                cell_format = self.text
                if key == "STATUS":
                    cell_format = self.ok if item[key] == "OK" else self.bad
                ws.write(row_index, col, scalar(item.get(key, "")), cell_format)
            row_index += 1
        ws.freeze_panes(6, 0)
        ws.set_column("A:A", 24)
        ws.set_column("B:B", 14)
        ws.set_column("C:C", 78)
        ws.set_column("D:D", 12)
        ws.set_column("E:E", 65)

    def close(self) -> None:
        self.workbook.close()


def safe_fetch_list(
    client: BitrixClient,
    log: ExportLog,
    dataset: str,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    result_path: Sequence[str] = (),
    start_key: str = "start",
) -> list[dict[str, Any]]:
    logging.info("[%s] начало: %s", dataset, method)
    started = time.monotonic()
    try:
        rows = client.list_all(
            method,
            params,
            result_path=result_path,
            start_key=start_key,
            label=dataset,
        )
        elapsed = time.monotonic() - started
        logging.info("[%s] готово: %s строк за %.1f сек.", dataset, len(rows), elapsed)
        log.add(dataset, method, "OK", len(rows))
        return rows
    except Exception as exc:  # noqa: BLE001 — ошибки должны попасть в Excel, а не оборвать экспорт
        logging.exception("Не удалось выгрузить %s", dataset)
        log.add(dataset, method, "ERROR", 0, str(exc))
        return []


def safe_fetch_single(
    client: BitrixClient,
    log: ExportLog,
    dataset: str,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    result_path: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Один вызов без пагинации — для methods и других несоставных методов."""
    logging.info("[%s] начало: %s (один запрос)", dataset, method)
    started = time.monotonic()
    try:
        data = client.call(method, params or {})
        result = data.get("result")
        if result_path:
            result = deep_get(result, result_path)
        rows = normalize_records(result)
        elapsed = time.monotonic() - started
        logging.info("[%s] готово: %s строк за %.1f сек.", dataset, len(rows), elapsed)
        log.add(dataset, method, "OK", len(rows))
        return rows
    except Exception as exc:  # noqa: BLE001
        logging.exception("Не удалось выгрузить %s", dataset)
        log.add(dataset, method, "ERROR", 0, str(exc))
        return []


def safe_fetch_fields(
    client: BitrixClient,
    log: ExportLog,
    dataset: str,
    method: str,
) -> tuple[list[dict[str, Any]], Any]:
    logging.info("[%s] начало: %s", dataset, method)
    started = time.monotonic()
    try:
        data = client.call(method, {})
        payload = data.get("result", {})
        rows = flatten_field_catalog(payload)
        logging.info("[%s] готово: %s полей за %.1f сек.", dataset, len(rows), time.monotonic() - started)
        log.add(dataset, method, "OK", len(rows))
        return rows, payload
    except Exception as exc:  # noqa: BLE001
        logging.exception("Не удалось выгрузить %s", dataset)
        log.add(dataset, method, "ERROR", 0, str(exc))
        return [], {}


def safe_fetch_crm_entity(
    client: BitrixClient,
    log: ExportLog,
    dataset: str,
    method: str,
    select: Sequence[str],
    fallback_select: Sequence[str],
) -> list[dict[str, Any]]:
    params = {"order": {"ID": "ASC"}, "filter": {}, "select": list(select)}
    logging.info("[%s] начало: %s", dataset, method)
    started = time.monotonic()
    try:
        rows = client.list_all(method, params, label=dataset)
        logging.info("[%s] готово: %s строк за %.1f сек.", dataset, len(rows), time.monotonic() - started)
        log.add(dataset, method, "OK", len(rows))
        return rows
    except Exception as exc:  # noqa: BLE001
        logging.warning("Полный select для %s не сработал: %s. Пробую резервный.", dataset, exc)
        try:
            rows = client.list_all(
                method,
                {"order": {"ID": "ASC"}, "filter": {}, "select": list(fallback_select)},
                label=f"{dataset}_fallback",
            )
            log.add(dataset, method, "PARTIAL", len(rows), f"Полный select не принят: {exc}")
            return rows
        except Exception as fallback_exc:  # noqa: BLE001
            logging.exception("Не удалось выгрузить %s", dataset)
            log.add(dataset, method, "ERROR", 0, f"{exc}; fallback: {fallback_exc}")
            return []


def write_json_bundle(
    dump_dir: Path,
    datasets: Mapping[str, Sequence[Mapping[str, Any]]],
    export_log: ExportLog,
    *,
    generated_at: str,
    portal_hint: str,
    config: Mapping[str, Any],
) -> Path:
    datasets_dir = dump_dir / "json" / "datasets"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for name, rows in datasets.items():
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".json"
        path = datasets_dir / filename
        path.write_text(json.dumps(list(rows), ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"dataset": name, "rows": len(rows), "file": str(path.relative_to(dump_dir)), "sha256": digest})

    log_path = dump_dir / "json" / "export_log.json"
    log_path.write_text(json.dumps(export_log.rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    manifest = {
        "format_version": 2,
        "generated_at": generated_at,
        "portal": portal_hint,
        "configuration": dict(config),
        "datasets": files,
        "notes": [
            "Исходные ID сохранены; при импорте в другой портал ID пользователей, стадий и сущностей потребуется сопоставить.",
            ("json/raw_api содержит необработанные ответы REST API." if config.get("save_raw_api") else "Сырые дубли ответов REST отключены для ускорения; все нормализованные данные находятся в json/datasets."),
            "Excel предназначен для проверки и сопоставления, JSON — основной источник данных для миграции.",
        ],
    }
    manifest_path = dump_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return manifest_path


def make_zip(source_dir: Path) -> Path:
    zip_path = source_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir.parent))
    return zip_path


def export_all(client: BitrixClient, config: Mapping[str, Any]) -> tuple[OrderedDict[str, list[dict[str, Any]]], ExportLog]:
    datasets: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    log = ExportLog()
    errors: list[dict[str, Any]] = []

    # Сначала справочники и поля — они нужны для расшифровки основных сущностей.
    methods = safe_fetch_single(client, log, "Methods", "methods", {})
    datasets["Methods"] = methods

    lead_fields, lead_fields_payload = safe_fetch_fields(client, log, "Lead_Fields", "crm.lead.fields")
    deal_fields, deal_fields_payload = safe_fetch_fields(client, log, "Deal_Fields", "crm.deal.fields")
    contact_fields, contact_fields_payload = safe_fetch_fields(client, log, "Contact_Fields", "crm.contact.fields")
    company_fields, company_fields_payload = safe_fetch_fields(client, log, "Company_Fields", "crm.company.fields")
    task_fields, task_fields_payload = safe_fetch_fields(client, log, "Task_Fields", "tasks.task.getFields")
    user_fields, _ = safe_fetch_fields(client, log, "User_Fields", "user.fields")
    department_fields, _ = safe_fetch_fields(client, log, "Department_Fields", "department.fields")
    activity_fields, activity_fields_payload = safe_fetch_fields(client, log, "Activity_Fields", "crm.activity.fields")
    requisite_fields, requisite_fields_payload = safe_fetch_fields(client, log, "Requisite_Fields", "crm.requisite.fields")
    address_fields, _ = safe_fetch_fields(client, log, "Address_Fields", "crm.address.fields")
    bank_fields, bank_fields_payload = safe_fetch_fields(client, log, "BankDetail_Fields", "crm.requisite.bankdetail.fields")

    datasets["Lead_Fields"] = lead_fields
    datasets["Deal_Fields"] = deal_fields
    datasets["Contact_Fields"] = contact_fields
    datasets["Company_Fields"] = company_fields
    datasets["Task_Fields"] = task_fields
    datasets["Task_Statuses"] = extract_field_enum_values(task_fields_payload, "STATUS")
    log.add(
        "Task_Statuses",
        "tasks.task.getFields:STATUS.values",
        "OK" if datasets["Task_Statuses"] else "PARTIAL",
        len(datasets["Task_Statuses"]),
        "" if datasets["Task_Statuses"] else "Справочник STATUS.values не найден в ответе",
    )
    datasets["User_Fields"] = user_fields
    datasets["Department_Fields"] = department_fields
    datasets["Activity_Fields"] = activity_fields
    datasets["Requisite_Fields"] = requisite_fields
    datasets["Address_Fields"] = address_fields
    datasets["BankDetail_Fields"] = bank_fields

    # Полные настройки пользовательских полей: XML_ID, обязательность,
    # множественность, настройки и значения списков. *.fields нужен для select,
    # а userfieldconfig/list — для точного восстановления конфигурации.
    datasets["CRM_UserField_Config"] = safe_fetch_list(
        client,
        log,
        "CRM_UserField_Config",
        "userfieldconfig.list",
        {
            "moduleId": "crm",
            "select": {"0": "*", "language": "ru"},
            "order": {"sort": "ASC", "id": "ASC"},
            "filter": {},
        },
        result_path=("fields",),
    )

    legacy_userfield_specs = [
        ("Lead_UserFields", "crm.lead.userfield.list"),
        ("Deal_UserFields", "crm.deal.userfield.list"),
        ("Contact_UserFields", "crm.contact.userfield.list"),
        ("Company_UserFields", "crm.company.userfield.list"),
        ("Requisite_UserFields", "crm.requisite.userfield.list"),
    ]
    for dataset_name, method_name in legacy_userfield_specs:
        datasets[dataset_name] = safe_fetch_list(
            client, log, dataset_name, method_name,
            {"order": {"SORT": "ASC", "ID": "ASC"}, "filter": {"LANG": "ru"}},
        )
    datasets["Task_UserFields"] = safe_fetch_list(
        client, log, "Task_UserFields", "task.item.userfield.getlist",
        {"ORDER": {"SORT": "ASC"}},
    )
    datasets["User_CustomFields"] = safe_fetch_list(
        client, log, "User_CustomFields", "user.userfield.list",
        {"order": {"SORT": "ASC", "ID": "ASC"}, "filter": {}},
    )

    datasets["CRM_Status_Types"] = safe_fetch_single(
        client, log, "CRM_Status_Types", "crm.status.entity.types", {}
    )
    datasets["CRM_Statuses"] = safe_fetch_list(
        client,
        log,
        "CRM_Statuses",
        "crm.status.list",
        {"order": {"ENTITY_ID": "ASC", "SORT": "ASC"}, "select": ["*"]},
    )
    datasets["Deal_Categories"] = safe_fetch_list(
        client,
        log,
        "Deal_Categories",
        "crm.category.list",
        {"entityTypeId": 2},
        result_path=("categories",),
    )
    datasets["Smart_Process_Types"] = safe_fetch_list(
        client, log, "Smart_Process_Types", "crm.type.list",
        {"order": {"id": "ASC"}, "filter": {}}, result_path=("types",),
    )
    datasets["Users"] = safe_fetch_list(
        client,
        log,
        "Users",
        "user.get",
        {"sort": "ID", "order": "ASC"},
    )
    datasets["Departments"] = safe_fetch_list(
        client,
        log,
        "Departments",
        "department.get",
        {"sort": "ID", "order": "ASC"},
        start_key="START",
    )
    datasets["Workgroups"] = safe_fetch_list(
        client,
        log,
        "Workgroups",
        "socialnetwork.api.workgroup.list",
        {
            "filter": {},
            "select": [
                "ID",
                "ACTIVE",
                "SUBJECT_ID",
                "NAME",
                "DESCRIPTION",
                "KEYWORDS",
                "CLOSED",
                "VISIBLE",
                "OPENED",
                "PROJECT",
                "LANDING",
                "DATE_CREATE",
                "DATE_UPDATE",
                "DATE_ACTIVITY",
                "OWNER_ID",
                "NUMBER_OF_MEMBERS",
                "NUMBER_OF_MODERATORS",
                "PROJECT_DATE_START",
                "PROJECT_DATE_FINISH",
                "SCRUM_OWNER_ID",
                "SCRUM_MASTER_ID",
                "SCRUM_SPRINT_DURATION",
                "SCRUM_TASK_RESPONSIBLE",
                "TYPE",
            ],
            "order": {"ID": "ASC"},
            "params": {"IS_ADMIN": "Y"},
        },
    )

    # Основные CRM-сущности.
    communication_fields = ["PHONE", "EMAIL", "WEB", "IM"]
    datasets["Leads"] = safe_fetch_crm_entity(
        client, log, "Leads", "crm.lead.list",
        build_entity_select(lead_fields_payload, communication_fields),
        ["*", "UF_*", *communication_fields],
    )
    datasets["Deals"] = safe_fetch_crm_entity(
        client, log, "Deals", "crm.deal.list",
        build_entity_select(deal_fields_payload),
        ["*", "UF_*"],
    )
    datasets["Contacts"] = safe_fetch_crm_entity(
        client, log, "Contacts", "crm.contact.list",
        build_entity_select(contact_fields_payload, communication_fields),
        ["*", "UF_*", *communication_fields],
    )
    datasets["Companies"] = safe_fetch_crm_entity(
        client, log, "Companies", "crm.company.list",
        build_entity_select(company_fields_payload, communication_fields),
        ["*", "UF_*", *communication_fields],
    )

    datasets["Requisites"] = safe_fetch_list(
        client, log, "Requisites", "crm.requisite.list",
        {"order": {"ID": "ASC"}, "filter": {}, "select": build_entity_select(requisite_fields_payload)},
    )
    datasets["Addresses"] = safe_fetch_list(
        client, log, "Addresses", "crm.address.list",
        {"order": {"TYPE_ID": "ASC"}, "filter": {}},
    )
    datasets["Bank_Details"] = safe_fetch_list(
        client, log, "Bank_Details", "crm.requisite.bankdetail.list",
        {"order": {"ID": "ASC"}, "filter": {}, "select": build_entity_select(bank_fields_payload)},
    )
    datasets["Requisite_Presets"] = safe_fetch_list(
        client, log, "Requisite_Presets", "crm.requisite.preset.list",
        {"order": {"ID": "ASC"}, "filter": {}, "select": ["*"]},
    )
    datasets["Requisite_Links"] = safe_fetch_list(
        client, log, "Requisite_Links", "crm.requisite.link.list",
        {"order": {"ENTITY_TYPE_ID": "ASC", "ENTITY_ID": "ASC"}, "filter": {}},
    )

    if config.get("export_smart_processes", True):
        for smart_type in datasets.get("Smart_Process_Types", []):
            entity_type_id = getv(smart_type, "entityTypeId", "ENTITY_TYPE_ID")
            if entity_type_id in (None, ""):
                continue
            prefix = f"SPA_{entity_type_id}"
            try:
                field_response = client.call(
                    "crm.item.fields",
                    {"entityTypeId": int(entity_type_id), "useOriginalUfNames": "Y"},
                )
                field_payload = field_response.get("result", {})
                datasets[f"{prefix}_Fields"] = flatten_field_catalog(field_payload)
                select = build_entity_select(field_payload)
                datasets[f"{prefix}_Items"] = safe_fetch_list(
                    client, log, f"{prefix}_Items", "crm.item.list",
                    {
                        "entityTypeId": int(entity_type_id),
                        "order": {"id": "ASC"},
                        "filter": {},
                        "select": select,
                        "useOriginalUfNames": "Y",
                    },
                    result_path=("items",),
                )
                datasets[f"{prefix}_Categories"] = safe_fetch_list(
                    client, log, f"{prefix}_Categories", "crm.category.list",
                    {"entityTypeId": int(entity_type_id)}, result_path=("categories",),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"DATASET": prefix, "METHOD": "crm.item.*", "ERROR": str(exc)})
                log.add(prefix, "crm.item.*", "ERROR", 0, str(exc))

    # Задачи. Выбираем поля из tasks.task.getFields; при несовместимости — надежный базовый набор.
    field_codes: list[str] = []
    task_fields_source = task_fields_payload.get("fields", task_fields_payload) if isinstance(task_fields_payload, dict) else {}
    if isinstance(task_fields_source, dict):
        field_codes = [str(key) for key in task_fields_source.keys()]
    fallback_task_fields = [
        "ID",
        "PARENT_ID",
        "TITLE",
        "DESCRIPTION",
        "MARK",
        "PRIORITY",
        "STATUS",
        "MULTITASK",
        "REPLICATE",
        "GROUP_ID",
        "STAGE_ID",
        "CREATED_BY",
        "CREATED_DATE",
        "RESPONSIBLE_ID",
        "ACCOMPLICES",
        "AUDITORS",
        "CHANGED_BY",
        "CHANGED_DATE",
        "STATUS_CHANGED_BY",
        "STATUS_CHANGED_DATE",
        "CLOSED_BY",
        "CLOSED_DATE",
        "DATE_START",
        "DEADLINE",
        "START_DATE_PLAN",
        "END_DATE_PLAN",
        "GUID",
        "XML_ID",
        "COMMENTS_COUNT",
        "ALLOW_CHANGE_DEADLINE",
        "ALLOW_TIME_TRACKING",
        "TASK_CONTROL",
        "TIME_ESTIMATE",
        "TIME_SPENT_IN_LOGS",
        "MATCH_WORK_TIME",
        "SITE_ID",
        "UF_CRM_TASK",
        "UF_TASK_WEBDAV_FILES",
        "TAGS",
    ]
    task_select = field_codes or fallback_task_fields
    tasks = safe_fetch_list(
        client,
        log,
        "Tasks",
        "tasks.task.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": task_select,
            "params": {
                "WITH_RESULT_INFO": True,
                "WITH_TIMER_INFO": True,
                "WITH_PARSED_DESCRIPTION": False,
            },
        },
        result_path=("tasks",),
    )
    if not tasks and field_codes:
        tasks = safe_fetch_list(
            client,
            log,
            "Tasks_fallback",
            "tasks.task.list",
            {
                "order": {"ID": "ASC"},
                "filter": {},
                "select": fallback_task_fields,
                "params": {"WITH_RESULT_INFO": True, "WITH_TIMER_INFO": True},
            },
            result_path=("tasks",),
        )
    datasets["Tasks"] = tasks

    task_stages: list[dict[str, Any]] = []
    if config.get("export_task_stages", True):
        group_ids = [str(getv(row, "ID", "id")) for row in datasets.get("Workgroups", []) if str(getv(row, "ID", "id"))]
        total_parts = max(1, (len(group_ids) + BATCH_SIZE - 1) // BATCH_SIZE)
        for part_number, part in enumerate(chunks(group_ids, BATCH_SIZE), start=1):
            logging.info("[Task_Stages] пакет %s/%s", part_number, total_parts)
            calls = [(f"group_{group_id}", "task.stages.get", {"entityId": group_id, "isAdmin": True}) for group_id in part]
            try:
                results, stage_errors = client.batch(calls)
                for alias, payload in results.items():
                    group_id = alias.removeprefix("group_")
                    if isinstance(payload, dict):
                        for stage_key, stage_value in payload.items():
                            row = {"GROUP_ID": group_id, "STAGE_KEY": stage_key}
                            if isinstance(stage_value, dict):
                                row.update(stage_value)
                            else:
                                row["VALUE"] = stage_value
                            task_stages.append(row)
                    else:
                        for stage_value in normalize_records(payload):
                            row = {"GROUP_ID": group_id}
                            row.update(stage_value)
                            task_stages.append(row)
                for alias, error in stage_errors.items():
                    errors.append({"DATASET": "Task_Stages", "METHOD": "task.stages.get", "ITEM_ID": alias, "ERROR": scalar(error)})
            except Exception as exc:  # noqa: BLE001
                errors.append({"DATASET": "Task_Stages", "METHOD": "task.stages.get", "ERROR": str(exc)})
        log.add("Task_Stages", "task.stages.get", "OK", len(task_stages))
    datasets["Task_Stages"] = task_stages

    if config["export_activities"]:
        datasets["CRM_Activities"] = safe_fetch_list(
            client,
            log,
            "CRM_Activities",
            "crm.activity.list",
            {"order": {"ID": "ASC"}, "filter": {}, "select": build_entity_select(activity_fields_payload, ["COMMUNICATIONS", "BINDINGS", "FILES"])},
        )
    else:
        datasets["CRM_Activities"] = []
        log.add("CRM_Activities", "crm.activity.list", "SKIPPED", 0, "Отключено настройкой")

    if config["export_relations"]:
        relation_specs = [
            ("Deal_Contacts", datasets["Deals"], "crm.deal.contact.items.get", "DEAL_ID"),
            ("Lead_Contacts", datasets["Leads"], "crm.lead.contact.items.get", "LEAD_ID"),
            ("Contact_Companies", datasets["Contacts"], "crm.contact.company.items.get", "CONTACT_ID"),
        ]
        for name, source, method, parent_col in relation_specs:
            try:
                rows, relation_errors = export_batch_relations(client, source, name, method, parent_col)
                datasets[name] = rows
                errors.extend(relation_errors)
                log.add(name, method, "OK" if not relation_errors else "PARTIAL", len(rows), f"Ошибок: {len(relation_errors)}" if relation_errors else "")
            except Exception as exc:  # noqa: BLE001
                datasets[name] = []
                errors.append({"DATASET": name, "METHOD": method, "ERROR": str(exc)})
                log.add(name, method, "ERROR", 0, str(exc))
    else:
        for name in ("Deal_Contacts", "Lead_Contacts", "Contact_Companies"):
            datasets[name] = []
            log.add(name, "batch relations", "SKIPPED", 0, "Отключено настройкой")

    if config["export_product_rows"]:
        for name, source, method, parent_col in (
            ("Deal_Products", datasets["Deals"], "crm.deal.productrows.get", "DEAL_ID"),
            ("Lead_Products", datasets["Leads"], "crm.lead.productrows.get", "LEAD_ID"),
        ):
            try:
                rows, relation_errors = export_batch_relations(client, source, name, method, parent_col)
                datasets[name] = rows
                errors.extend(relation_errors)
                log.add(name, method, "OK" if not relation_errors else "PARTIAL", len(rows), f"Ошибок: {len(relation_errors)}" if relation_errors else "")
            except Exception as exc:  # noqa: BLE001
                datasets[name] = []
                errors.append({"DATASET": name, "METHOD": method, "ERROR": str(exc)})
                log.add(name, method, "ERROR", 0, str(exc))
    else:
        datasets["Deal_Products"] = []
        datasets["Lead_Products"] = []

    if config.get("export_timeline_comments", True):
        timeline_rows: list[dict[str, Any]] = []
        timeline_specs = [("Leads", 1), ("Deals", 2), ("Contacts", 3), ("Companies", 4)]
        for source_name, entity_type_id in timeline_specs:
            source_rows = datasets.get(source_name, [])
            ids = [str(getv(row, "ID", "id")) for row in source_rows if str(getv(row, "ID", "id"))]
            total_parts = max(1, (len(ids) + BATCH_SIZE - 1) // BATCH_SIZE)
            for part_number, part in enumerate(chunks(ids, BATCH_SIZE), start=1):
                if part_number == 1 or part_number % 10 == 0 or part_number == total_parts:
                    logging.info("[Timeline_Comments:%s] пакет %s/%s", source_name, part_number, total_parts)
                calls = [
                    (f"item_{entity_type_id}_{item_id}", "crm.timeline.comment.list", {"entityTypeId": entity_type_id, "entityId": item_id})
                    for item_id in part
                ]
                try:
                    results, timeline_errors = client.batch(calls)
                    for alias, payload in results.items():
                        _, _, item_id = alias.split("_", 2)
                        for position, record in enumerate(normalize_records(payload), start=1):
                            row = {"OWNER_TYPE_ID": entity_type_id, "OWNER_ID": item_id, "COMMENT_ORDER": position}
                            row.update(record)
                            timeline_rows.append(row)
                    for alias, error in timeline_errors.items():
                        errors.append({"DATASET": "Timeline_Comments", "METHOD": "crm.timeline.comment.list", "ITEM_ID": alias, "ERROR": scalar(error)})
                except Exception as exc:  # noqa: BLE001
                    errors.append({"DATASET": "Timeline_Comments", "METHOD": "crm.timeline.comment.list", "ERROR": str(exc)})
        datasets["Timeline_Comments"] = timeline_rows
        log.add("Timeline_Comments", "crm.timeline.comment.list", "OK", len(timeline_rows))
    else:
        datasets["Timeline_Comments"] = []
        log.add("Timeline_Comments", "crm.timeline.comment.list", "SKIPPED", 0, "Отключено настройкой")

    communications: list[dict[str, Any]] = []
    for entity_name in ("Leads", "Contacts", "Companies"):
        communications.extend(collect_communications(entity_name.upper(), datasets.get(entity_name, [])))
    datasets["CRM_Communications"] = communications

    enrich_records(datasets)
    split_workgroups(datasets)
    build_clients_index(datasets)
    datasets["Errors"] = errors

    # Упорядочиваем листы по смыслу.
    order = [
        "Leads",
        "Deals",
        "Contacts",
        "Companies",
        "Clients_Index",
        "Users",
        "Departments",
        "Workgroups",
        "Projects",
        "Groups",
        "Scrum",
        "Collabs",
        "Tasks",
        "Task_Statuses",
        "Task_Stages",
        "Deal_Contacts",
        "Lead_Contacts",
        "Contact_Companies",
        "Deal_Products",
        "Lead_Products",
        "CRM_Activities",
        "Timeline_Comments",
        "CRM_Communications",
        "Requisites",
        "Addresses",
        "Bank_Details",
        "Requisite_Presets",
        "Requisite_Links",
        "CRM_Status_Types",
        "CRM_Statuses",
        "Deal_Categories",
        "Smart_Process_Types",
        "CRM_UserField_Config",
        "Lead_UserFields",
        "Deal_UserFields",
        "Contact_UserFields",
        "Company_UserFields",
        "Requisite_UserFields",
        "Task_UserFields",
        "User_CustomFields",
        "Lead_Fields",
        "Deal_Fields",
        "Contact_Fields",
        "Company_Fields",
        "Task_Fields",
        "User_Fields",
        "Department_Fields",
        "Activity_Fields",
        "Requisite_Fields",
        "Address_Fields",
        "BankDetail_Fields",
        "Methods",
        "Errors",
    ]
    ordered = OrderedDict((name, datasets.get(name, [])) for name in order)
    for name, rows in datasets.items():
        if name not in ordered:
            ordered[name] = rows
    return ordered, log


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Выгрузить Bitrix24 в JSON и Excel")
    parser.add_argument("--output", help="Каталог итогового дампа")
    parser.add_argument("--webhook", help="URL webhook; безопаснее использовать BITRIX_WEBHOOK_URL")
    parser.add_argument("--no-activities", action="store_true", help="Не выгружать CRM-активности")
    parser.add_argument("--no-relations", action="store_true", help="Не выгружать связи CRM")
    parser.add_argument("--no-products", action="store_true", help="Не выгружать товарные позиции")
    parser.add_argument("--no-timeline", action="store_true", help="Не выгружать комментарии таймлайна")
    parser.add_argument("--no-smart-processes", action="store_true", help="Не выгружать смарт-процессы")
    parser.add_argument("--no-task-stages", action="store_true", help="Не выгружать стадии задач")
    parser.add_argument("--no-zip", action="store_true", help="Не создавать ZIP-архив")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    webhook = args.webhook or os.getenv("BITRIX_WEBHOOK_URL", "")
    output_dir = Path(os.getenv("OUTPUT_DIR", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_dir = Path(args.output) if args.output else output_dir / f"bitrix24_dump_{timestamp}"
    dump_dir.mkdir(parents=True, exist_ok=True)
    output_path = dump_dir / "bitrix24_export.xlsx"

    config = {
        "export_activities": env_bool("EXPORT_ACTIVITIES", True) and not args.no_activities,
        "export_relations": env_bool("EXPORT_RELATIONS", True) and not args.no_relations,
        "export_product_rows": env_bool("EXPORT_PRODUCT_ROWS", True) and not args.no_products,
        "export_timeline_comments": env_bool("EXPORT_TIMELINE_COMMENTS", True) and not args.no_timeline,
        "export_smart_processes": env_bool("EXPORT_SMART_PROCESSES", True) and not args.no_smart_processes,
        "export_task_stages": env_bool("EXPORT_TASK_STAGES", True) and not args.no_task_stages,
        "save_raw_api": env_bool("SAVE_RAW_API", False),
    }
    recorder = RawRecorder(dump_dir / "json" / "raw_api") if config["save_raw_api"] else None
    client = BitrixClient(
        webhook,
        timeout=int(os.getenv("HTTP_TIMEOUT_SECONDS", "90")),
        delay=float(os.getenv("REQUEST_DELAY_SECONDS", "0.10")),
        max_retries=int(os.getenv("MAX_RETRIES", "8")),
        recorder=recorder,
        max_pages=int(os.getenv("MAX_PAGES", str(DEFAULT_MAX_PAGES))),
        progress_every_pages=int(os.getenv("PROGRESS_EVERY_PAGES", str(DEFAULT_PROGRESS_EVERY_PAGES))),
    )

    logging.info("Начинаю выгрузку Bitrix24")
    logging.info("Параметры: %s", json.dumps(config, ensure_ascii=False, sort_keys=True))
    datasets, export_log = export_all(client, config)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    portal_hint = client.base_url.split("/rest/")[0]
    if recorder is not None:
        recorder.close()
    write_json_bundle(
        dump_dir, datasets, export_log,
        generated_at=generated_at, portal_hint=portal_hint, config=config,
    )

    writer = ExcelWriter(output_path)
    try:
        # Summary должен быть первым листом.
        writer.write_summary(datasets, export_log, generated_at, portal_hint)
        for name, rows in datasets.items():
            writer.write_dataset(name, rows)
    finally:
        writer.close()

    zip_path = None if args.no_zip else make_zip(dump_dir)
    logging.info("Готово: %s", dump_dir.resolve())
    print(dump_dir.resolve())
    if zip_path is not None:
        print(zip_path.resolve())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Остановлено пользователем", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Экспорт завершился с ошибкой")
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)
