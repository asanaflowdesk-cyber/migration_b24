from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import requests


DEFAULT_DISTRIBUTION_SPREADSHEET_ID = "1WuRHHyQm5lHxDlW81m4P0oZJ6X_SDYj1bN-NOx8aM2k"


class DistributionError(RuntimeError):
    """The assignment source is missing, inconsistent, or cannot be applied safely."""


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalise(value: Any) -> str:
    value = _text(value).casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-яәіңғүұқөһ]+", " ", value).strip()


def normalise_bin(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


@dataclass(frozen=True, slots=True)
class AssignmentUser:
    user_id: int
    full_name: str
    department_id: int
    department_name: str
    role: str = "менеджер по страхованию"


@dataclass(frozen=True, slots=True)
class DistributionSnapshot:
    users: tuple[AssignmentUser, ...]
    company_assignments: dict[str, int]


class GoogleSheetDistributionSource:
    """Read the two assignment tables through Google Sheets' CSV export.

    The workbook must be readable by the runner. A failed or empty read is a
    blocking error: silently returning to a stale hard-coded manager list could
    assign client data to a person who is absent.
    """

    def __init__(
        self,
        spreadsheet_id: str = DEFAULT_DISTRIBUTION_SPREADSHEET_ID,
        *,
        users_sheet: str = "user_list",
        assignments_sheet: str = "Company_fix",
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.spreadsheet_id = _text(spreadsheet_id)
        self.users_sheet = _text(users_sheet)
        self.assignments_sheet = _text(assignments_sheet)
        self.timeout = timeout
        self.authenticated = False
        if session is not None:
            self.session = session
        else:
            raw_credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
            if raw_credentials:
                try:
                    info = json.loads(raw_credentials)
                    from google.auth.transport.requests import AuthorizedSession
                    from google.oauth2.service_account import Credentials

                    credentials = Credentials.from_service_account_info(
                        info,
                        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
                    )
                    self.session = AuthorizedSession(credentials)
                    self.authenticated = True
                except Exception as exc:  # noqa: BLE001 - configuration boundary
                    raise DistributionError(
                        f"Некорректный GOOGLE_SERVICE_ACCOUNT_JSON: {exc}"
                    ) from None
            else:
                self.session = requests.Session()
        if not self.spreadsheet_id:
            raise DistributionError("Не задан ID Google-таблицы распределения")

    def load(self) -> DistributionSnapshot:
        users_rows = self._read_csv(
            self.users_sheet,
            ("user_id", "full_name", "DepartmentID", "name", "role"),
        )
        assignment_rows = self._read_csv(
            self.assignments_sheet,
            ("responsible_id", "full_name", "ORIGIN_ID", "TITLE"),
        )
        users = self._parse_users(users_rows)
        assignments = self._parse_assignments(assignment_rows)
        if not users:
            raise DistributionError(
                f"Лист {self.users_sheet!r} не содержит участников распределения"
            )
        return DistributionSnapshot(tuple(users), assignments)

    def _read_csv(
        self,
        sheet_name: str,
        expected_headers: Iterable[str],
    ) -> list[dict[str, str]]:
        if self.authenticated:
            encoded_range = quote(f"'{sheet_name}'!A:Z", safe="")
            url = (
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{self.spreadsheet_id}/values/{encoded_range}"
            )
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                values = payload.get("values", [])
            except (requests.RequestException, ValueError, OSError) as exc:
                raise DistributionError(
                    f"Не удалось прочитать лист Google Sheets {sheet_name!r}: {exc}"
                ) from None
            if not values:
                raise DistributionError(f"В листе {sheet_name!r} отсутствует строка заголовков")
            headers = [_text(value) for value in values[0]]
            missing = [name for name in expected_headers if name not in set(headers)]
            if missing:
                raise DistributionError(
                    f"В листе {sheet_name!r} отсутствуют обязательные столбцы: {', '.join(missing)}"
                )
            result: list[dict[str, str]] = []
            for cells in values[1:]:
                row = {
                    header: _text(cells[index]) if index < len(cells) else ""
                    for index, header in enumerate(headers)
                }
                if any(row.values()):
                    result.append(row)
            return result

        url = (
            "https://docs.google.com/spreadsheets/d/"
            f"{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"
        )
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except (requests.RequestException, OSError) as exc:
            raise DistributionError(
                f"Не удалось прочитать лист Google Sheets {sheet_name!r}: {exc}"
            ) from None
        body = response.content.decode("utf-8-sig", errors="strict")
        if "<html" in body[:500].casefold():
            raise DistributionError(
                f"Google Sheets вернул страницу входа вместо листа {sheet_name!r}; "
                "дайте runner доступ на чтение по ссылке"
            )
        reader = csv.DictReader(io.StringIO(body))
        if not reader.fieldnames:
            raise DistributionError(f"В листе {sheet_name!r} отсутствует строка заголовков")
        actual_headers = {_text(value) for value in reader.fieldnames}
        missing = [name for name in expected_headers if name not in actual_headers]
        if missing:
            raise DistributionError(
                f"В листе {sheet_name!r} отсутствуют обязательные столбцы: {', '.join(missing)}"
            )
        return [
            {_text(key): _text(value) for key, value in row.items() if key is not None}
            for row in reader
            if any(_text(value) for value in row.values())
        ]

    def _parse_users(self, rows: list[dict[str, str]]) -> list[AssignmentUser]:
        result: dict[int, AssignmentUser] = {}
        for line_number, row in enumerate(rows, start=2):
            raw_id = _text(row.get("user_id"))
            raw_department = _text(row.get("DepartmentID"))
            if not raw_id and not raw_department:
                continue
            if not raw_id.isdigit() or int(raw_id) <= 0:
                raise DistributionError(
                    f"{self.users_sheet}!A{line_number}: некорректный user_id {raw_id!r}"
                )
            if not raw_department.isdigit() or int(raw_department) <= 0:
                raise DistributionError(
                    f"{self.users_sheet}!C{line_number}: некорректный DepartmentID {raw_department!r}"
                )
            item = AssignmentUser(
                int(raw_id),
                _text(row.get("full_name")),
                int(raw_department),
                _text(row.get("name")),
                _text(row.get("role")),
            )
            if not item.department_name:
                raise DistributionError(
                    f"{self.users_sheet}!D{line_number}: не указано название подразделения"
                )
            if not item.role:
                raise DistributionError(
                    f"{self.users_sheet}!E{line_number}: не указана роль пользователя"
                )
            previous = result.get(item.user_id)
            if previous and previous != item:
                raise DistributionError(
                    f"Пользователь {item.user_id} указан в {self.users_sheet!r} несколько раз "
                    "с разными данными"
                )
            result[item.user_id] = item
        return list(result.values())

    def _parse_assignments(self, rows: list[dict[str, str]]) -> dict[str, int]:
        result: dict[str, int] = {}
        for line_number, row in enumerate(rows, start=2):
            raw_manager = _text(row.get("responsible_id"))
            raw_bin = _text(row.get("ORIGIN_ID"))
            if not raw_manager and not raw_bin:
                continue
            if not raw_manager.isdigit() or int(raw_manager) <= 0:
                raise DistributionError(
                    f"{self.assignments_sheet}!A{line_number}: некорректный responsible_id {raw_manager!r}"
                )
            bin_number = normalise_bin(raw_bin)
            if len(bin_number) != 12:
                raise DistributionError(
                    f"{self.assignments_sheet}!C{line_number}: БИН должен содержать 12 цифр, "
                    f"получено {raw_bin!r}"
                )
            manager_id = int(raw_manager)
            previous = result.get(bin_number)
            if previous is not None and previous != manager_id:
                raise DistributionError(
                    f"Для БИН {bin_number} в {self.assignments_sheet!r} указаны разные ответственные: "
                    f"{previous} и {manager_id}"
                )
            result[bin_number] = manager_id
        return result


BRANCH_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("astana", ("астана", "астанин", "столичн")),
    ("almaty", ("алматы", "алматин")),
    ("aktau", ("актау", "мангистау", "мангыстау")),
    ("aktobe", ("актобе", "актюбин")),
    ("atyrau", ("атырау", "атырауск")),
    ("karaganda", ("караганда", "карагандин", "улытау", "жезказган")),
    ("kokshetau", ("кокшетау", "акмолин")),
    ("kostanay", ("костанай", "костанайск")),
    ("petropavl", ("петропавл", "северо казахстан")),
    ("semey", ("семей", "абайск", "область абай")),
    ("uralsk", ("уральск", "западно казахстан")),
    ("shymkent", ("шымкент", "туркестан")),
    ("pavlodar", ("павлодар",)),
    ("kyzylorda", ("кызылорда",)),
    ("taraz", ("тараз", "жамбыл")),
    ("taldykorgan", ("талдыкорган", "жетысу", "жетису")),
    ("oskemen", ("усть каменогорск", "оскемен", "восточно казахстан")),
)


def branch_key(value: Any) -> str | None:
    normalised = _normalise(value)
    if not normalised:
        return None
    for key, variants in BRANCH_KEYWORDS:
        if any(variant in normalised for variant in variants):
            return key
    return None


def is_branch_head(role: Any) -> bool:
    value = _normalise(role)
    return value == "роп" or "руководитель отдела продаж" in value
