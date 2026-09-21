from __future__ import annotations

import json
import os
from typing import Any, Iterable
from urllib.parse import quote

import requests


class GoogleSheetsError(RuntimeError):
    pass


def column_letter(index: int) -> str:
    if index < 1:
        raise ValueError("Column index must be >= 1")
    result = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


class GoogleSheetsClient:
    def __init__(
        self,
        spreadsheet_id: str,
        *,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.spreadsheet_id = str(spreadsheet_id or "").strip()
        self.timeout = timeout
        self.session = session or requests.Session()
        if not self.spreadsheet_id:
            raise GoogleSheetsError("Не задан GOOGLE_SHEETS_SPREADSHEET_ID")

    @classmethod
    def from_env(cls, spreadsheet_id: str) -> "GoogleSheetsClient":
        raw_credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not raw_credentials:
            raise GoogleSheetsError("Не задан secret GOOGLE_SERVICE_ACCOUNT_JSON")
        try:
            info = json.loads(raw_credentials)
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.service_account import Credentials

            credentials = Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
        except Exception as exc:
            raise GoogleSheetsError(f"Некорректный GOOGLE_SERVICE_ACCOUNT_JSON: {exc}") from None
        return cls(spreadsheet_id, session=AuthorizedSession(credentials))

    def read_rows(self, sheet_name: str) -> list[list[str]]:
        encoded_range = quote(f"'{sheet_name}'!A:ZZ", safe="")
        url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{self.spreadsheet_id}/values/{encoded_range}"
        )
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError, OSError) as exc:
            raise GoogleSheetsError(f"Не удалось прочитать лист {sheet_name!r}: {exc}") from None
        return [
            [str(value or "").strip() for value in row]
            for row in data.get("values", [])
            if isinstance(row, list)
        ]

    def batch_update(
        self,
        updates: Iterable[tuple[str, list[list[Any]]]],
        *,
        chunk_size: int = 200,
    ) -> None:
        items = list(updates)
        if not items:
            return
        url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{self.spreadsheet_id}/values:batchUpdate"
        )
        for start in range(0, len(items), chunk_size):
            chunk = items[start : start + chunk_size]
            payload = {
                "valueInputOption": "RAW",
                "data": [
                    {"range": cell_range, "majorDimension": "ROWS", "values": values}
                    for cell_range, values in chunk
                ],
            }
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()
            except (requests.RequestException, OSError) as exc:
                raise GoogleSheetsError(f"Не удалось записать результаты в Google Sheets: {exc}") from None

    def ensure_columns(
        self,
        sheet_name: str,
        headers: list[str],
        required: list[str],
    ) -> dict[str, int]:
        normalized = {
            str(value or "").strip().casefold(): index
            for index, value in enumerate(headers, start=1)
            if str(value or "").strip()
        }
        updates: list[tuple[str, list[list[Any]]]] = []
        next_index = max(len(headers), max(normalized.values(), default=0)) + 1
        result: dict[str, int] = {}
        for header in required:
            key = header.casefold()
            existing = normalized.get(key)
            if existing is None:
                existing = next_index
                next_index += 1
                updates.append((f"'{sheet_name}'!{column_letter(existing)}1", [[header]]))
                normalized[key] = existing
            result[header] = existing
        self.batch_update(updates)
        return result
