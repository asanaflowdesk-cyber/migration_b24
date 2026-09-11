from __future__ import annotations

import csv
import io
import json
import os
from typing import Any
from urllib.parse import quote

import requests


class GoogleSheetsError(RuntimeError):
    pass


class GoogleSheetsClient:
    def __init__(
        self,
        spreadsheet_id: str,
        *,
        timeout: int = 30,
        session: requests.Session | None = None,
        authenticated: bool = False,
    ) -> None:
        self.spreadsheet_id = str(spreadsheet_id or "").strip()
        self.timeout = timeout
        self.session = session or requests.Session()
        self.authenticated = authenticated
        if not self.spreadsheet_id:
            raise GoogleSheetsError("Не задан GOOGLE_SHEETS_SPREADSHEET_ID")

    @classmethod
    def from_env(cls, spreadsheet_id: str, *, require_write: bool) -> "GoogleSheetsClient":
        raw_credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not raw_credentials:
            if require_write:
                raise GoogleSheetsError(
                    "Для apply требуется secret GOOGLE_SERVICE_ACCOUNT_JSON; "
                    "без него нельзя безопасно записать созданные ID обратно в Google Sheets"
                )
            return cls(spreadsheet_id)
        try:
            info = json.loads(raw_credentials)
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.service_account import Credentials

            credentials = Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
        except Exception as exc:  # noqa: BLE001 - normalise configuration failures
            raise GoogleSheetsError(
                f"Некорректный GOOGLE_SERVICE_ACCOUNT_JSON: {exc}"
            ) from None
        return cls(
            spreadsheet_id,
            session=AuthorizedSession(credentials),
            authenticated=True,
        )

    def read_rows(self, sheet_name: str) -> list[list[str]]:
        if self.authenticated:
            encoded_range = quote(f"'{sheet_name}'!A:AZ", safe="")
            url = (
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{self.spreadsheet_id}/values/{encoded_range}"
            )
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError, OSError) as exc:
                raise GoogleSheetsError(
                    f"Не удалось прочитать лист {sheet_name!r}: {exc}"
                ) from None
            return [
                [str(value or "").strip() for value in row]
                for row in data.get("values", [])
                if isinstance(row, list)
            ]

        url = (
            "https://docs.google.com/spreadsheets/d/"
            f"{self.spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"
        )
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            body = response.content.decode("utf-8-sig")
        except (requests.RequestException, UnicodeError, OSError) as exc:
            raise GoogleSheetsError(
                f"Не удалось прочитать лист {sheet_name!r}: {exc}"
            ) from None
        if "<html" in body[:500].casefold():
            raise GoogleSheetsError(
                f"Google Sheets вернул страницу входа вместо листа {sheet_name!r}"
            )
        return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(body))]

    def batch_update(self, updates: list[tuple[str, list[list[Any]]]]) -> None:
        if not self.authenticated:
            raise GoogleSheetsError("Запись в Google Sheets без сервисного аккаунта запрещена")
        url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{self.spreadsheet_id}/values:batchUpdate"
        )
        payload = {
            "valueInputOption": "RAW",
            "data": [
                {"range": cell_range, "majorDimension": "ROWS", "values": values}
                for cell_range, values in updates
            ],
        }
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except (requests.RequestException, OSError) as exc:
            raise GoogleSheetsError(f"Не удалось записать ID в Google Sheets: {exc}") from None

    def append_row(self, sheet_name: str, values: list[Any]) -> None:
        if not self.authenticated:
            raise GoogleSheetsError("Запись в Google Sheets без сервисного аккаунта запрещена")
        encoded_range = quote(f"'{sheet_name}'!A:E", safe="")
        url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{self.spreadsheet_id}/values/{encoded_range}:append"
            "?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
        )
        try:
            response = self.session.post(
                url,
                json={"majorDimension": "ROWS", "values": [values]},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except (requests.RequestException, OSError) as exc:
            raise GoogleSheetsError(
                f"Не удалось добавить пользователя в user_list: {exc}"
            ) from None
