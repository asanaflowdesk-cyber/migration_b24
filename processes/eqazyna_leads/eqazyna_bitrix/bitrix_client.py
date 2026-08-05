from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

try:
    import truststore
except ImportError:  # pragma: no cover - installed by the workflow
    truststore = None
else:
    truststore.inject_into_ssl()

import requests


class BitrixError(RuntimeError):
    pass


TRANSIENT_CODES = {
    "QUERY_LIMIT_EXCEEDED",
    "OPERATION_TIME_LIMIT",
    "OVERLOAD_LIMIT",
}


@dataclass(slots=True)
class BitrixClient:
    webhook_url: str
    timeout: int = 30
    retries: int = 5
    polite_delay_seconds: float = 0.05
    verify_ssl: bool | str = True
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        self.webhook_url = (self.webhook_url or "").strip().rstrip("/") + "/"
        if self.webhook_url == "/":
            raise ValueError("TARGET_BITRIX_WEBHOOK_URL is empty")
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "migration-b24-eqazyna-leads/1.0",
            }
        )

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = self.webhook_url + method + ".json"
        payload = payload or {}
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
                try:
                    data = response.json()
                except Exception as exc:
                    preview = (response.text or "")[:500]
                    raise BitrixError(
                        f"{method}: Bitrix returned non-JSON response: {preview}"
                    ) from exc

                if response.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {response.status_code}: {data}")
                if response.status_code >= 400 or "error" in data:
                    code = str(data.get("error") or f"HTTP_{response.status_code}")
                    description = str(
                        data.get("error_description")
                        or data.get("error")
                        or response.reason
                    )
                    if code in TRANSIENT_CODES and attempt < self.retries:
                        time.sleep(min(30.0, 2.0**attempt))
                        continue
                    raise BitrixError(f"{method}: {code}: {description}")

                if self.polite_delay_seconds:
                    time.sleep(self.polite_delay_seconds)
                return data.get("result")
            except BitrixError:
                raise
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(20.0, 1.5**attempt))

        raise BitrixError(f"{method}: request failed after retries: {last_error}")

    def get_lead_fields(self) -> dict[str, Any]:
        result = self.call("crm.lead.fields")
        return result if isinstance(result, dict) else {}

    def find_lead_by_origin(
        self,
        origin_id: str,
        originator_id: str = "EQAZYNA_LEAD",
        extra_select: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Find the canonical lead and then a migrated legacy e-Qazyna lead.

        The historical cloud parser stored one deal per application using
        ORIGINATOR_ID=EQAZYNA and ORIGIN_ID=eQazyna|<document>|<BIN>. During
        migration those values are preserved on the new leads. The current
        parser maintains one lead per BIN, so it first searches the canonical
        marker and then falls back to the legacy marker containing the BIN.
        """
        select = [
            "ID",
            "TITLE",
            "STATUS_ID",
            "ASSIGNED_BY_ID",
            "COMPANY_TITLE",
            "COMMENTS",
            "PHONE",
            "ORIGINATOR_ID",
            "ORIGIN_ID",
        ]
        for field_name in extra_select or []:
            if field_name and field_name not in select:
                select.append(field_name)

        result = self.call(
            "crm.lead.list",
            {
                "order": {"ID": "DESC"},
                "filter": {
                    "ORIGINATOR_ID": originator_id,
                    "ORIGIN_ID": origin_id,
                },
                "select": select,
            },
        )
        if isinstance(result, list) and result:
            return result[0]

        legacy_result = self.call(
            "crm.lead.list",
            {
                "order": {"ID": "DESC"},
                "filter": {
                    "ORIGINATOR_ID": "EQAZYNA",
                    "%ORIGIN_ID": origin_id,
                },
                "select": select,
            },
        )
        if isinstance(legacy_result, list) and legacy_result:
            return legacy_result[0]
        return None

    def create_lead(self, fields: dict[str, Any]) -> str:
        result = self.call(
            "crm.lead.add",
            {
                "fields": fields,
                "params": {"REGISTER_SONET_EVENT": "N"},
            },
        )
        if result in (None, ""):
            raise BitrixError("crm.lead.add returned an empty lead ID")
        return str(result)

    def update_lead(self, lead_id: str, fields: dict[str, Any]) -> None:
        self.call(
            "crm.lead.update",
            {
                "id": int(lead_id),
                "fields": fields,
                "params": {"REGISTER_SONET_EVENT": "N"},
            },
        )

    def add_timeline_comment(
        self,
        entity_type: str,
        entity_id: str,
        comment: str,
    ) -> str | None:
        result = self.call(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_ID": int(entity_id),
                    "ENTITY_TYPE": entity_type,
                    "COMMENT": comment,
                }
            },
        )
        return str(result) if result is not None else None
