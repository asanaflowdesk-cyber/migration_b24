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
                "User-Agent": "migration-b24-eqazyna-leads/2.0",
            }
        )

    def _request_data(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                return data if isinstance(data, dict) else {"result": data}
            except BitrixError:
                raise
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(20.0, 1.5**attempt))

        raise BitrixError(f"{method}: request failed after retries: {last_error}")

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request_data(method, payload).get("result")

    def list_all(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        base = dict(payload or {})
        start = int(base.pop("start", 0) or 0)
        rows: list[dict[str, Any]] = []
        while True:
            request_payload = {**base, "start": start}
            data = self._request_data(method, request_payload)
            result = data.get("result")
            if isinstance(result, list):
                rows.extend(row for row in result if isinstance(row, dict))
            next_value = data.get("next")
            if next_value in (None, ""):
                break
            start = int(next_value)
        return rows

    # ---------- field metadata ----------

    def get_lead_fields(self) -> dict[str, Any]:
        result = self.call("crm.lead.fields")
        return result if isinstance(result, dict) else {}

    def get_company_fields(self) -> dict[str, Any]:
        result = self.call("crm.company.fields")
        return result if isinstance(result, dict) else {}

    def get_contact_fields(self) -> dict[str, Any]:
        result = self.call("crm.contact.fields")
        return result if isinstance(result, dict) else {}

    def get_requisite_fields(self) -> dict[str, Any]:
        result = self.call("crm.requisite.fields")
        return result if isinstance(result, dict) else {}

    def list_lead_statuses(self) -> list[dict[str, Any]]:
        result = self.call(
            "crm.status.list",
            {
                "order": {"SORT": "ASC", "ID": "ASC"},
                "filter": {"ENTITY_ID": "STATUS"},
                "select": [
                    "ID",
                    "STATUS_ID",
                    "NAME",
                    "SEMANTICS",
                    "SORT",
                ],
            },
        )
        return result if isinstance(result, list) else []

    def count_open_leads_for_manager(
        self,
        manager_id: int,
        terminal_status_ids: set[str] | None = None,
    ) -> int:
        terminal = {str(value) for value in (terminal_status_ids or set()) if str(value)}
        rows = self.list_all(
            "crm.lead.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ASSIGNED_BY_ID": int(manager_id)},
                "select": ["ID", "STATUS_ID"],
            },
        )
        return sum(1 for row in rows if str(row.get("STATUS_ID") or "") not in terminal)

    def discover_company_requisite_preset_id(self) -> int | None:
        """Return the preset already used by company requisites in the box.

        This is more reliable for this portal than ``crm.requisite.preset.list``:
        the latter can be empty for the webhook while migrated company
        requisites are fully readable and already prove the valid PRESET_ID.
        """
        result = self.call(
            "crm.requisite.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ENTITY_TYPE_ID": 4},
                "select": ["ID", "PRESET_ID"],
            },
        )
        if not isinstance(result, list):
            return None

        counts: dict[int, int] = {}
        for row in result:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("PRESET_ID") or "").strip()
            if raw.isdigit() and int(raw) > 0:
                preset_id = int(raw)
                counts[preset_id] = counts.get(preset_id, 0) + 1
        if not counts:
            return None
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    def list_requisite_presets(self) -> list[dict[str, Any]]:
        # Preset.ENTITY_TYPE_ID describes the preset object itself and is normally 8
        # (requisite). It does not mean that the future requisite owner is a company
        # (4) or a contact (3), so filtering presets by ENTITY_TYPE_ID=4 returns an
        # empty list on standard Bitrix24 installations.
        result = self.call(
            "crm.requisite.preset.list",
            {
                "order": {"SORT": "ASC", "ID": "ASC"},
                "filter": {},
                "select": [
                    "ID",
                    "NAME",
                    "XML_ID",
                    "ACTIVE",
                    "SORT",
                    "ENTITY_TYPE_ID",
                    "COUNTRY_ID",
                ],
            },
        )
        return result if isinstance(result, list) else []

    # ---------- leads ----------

    def find_lead_by_origin(
        self,
        origin_id: str,
        originator_id: str = "EQAZYNA_LEAD",
        extra_select: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Find canonical lead, then a migrated legacy e-Qazyna lead."""
        select = [
            "ID",
            "TITLE",
            "STATUS_ID",
            "STATUS_DESCRIPTION",
            "STATUS_SEMANTIC_ID",
            "DATE_CREATE",
            "DATE_MODIFY",
            "ASSIGNED_BY_ID",
            "COMPANY_ID",
            "CONTACT_ID",
            "COMPANY_TITLE",
            "NAME",
            "LAST_NAME",
            "SECOND_NAME",
            "COMMENTS",
            "PHONE",
            "ADDRESS",
            "ADDRESS_CITY",
            "ADDRESS_REGION",
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

    def find_lead_by_application(
        self,
        doc_number: str,
        bin_number: str,
        originator_id: str = "EQAZYNA_LEAD",
        extra_select: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Find an existing lead for one exact e-Qazyna application.

        New parser records use ORIGIN_ID equal to the application number.
        Legacy parser records are also detected by the old composite origin,
        title or comments so an already loaded application is never created
        twice.
        """
        select = [
            "ID",
            "TITLE",
            "STATUS_ID",
            "STATUS_DESCRIPTION",
            "STATUS_SEMANTIC_ID",
            "DATE_CREATE",
            "DATE_MODIFY",
            "ASSIGNED_BY_ID",
            "COMPANY_ID",
            "CONTACT_ID",
            "COMPANY_TITLE",
            "NAME",
            "LAST_NAME",
            "SECOND_NAME",
            "COMMENTS",
            "PHONE",
            "ADDRESS",
            "ADDRESS_CITY",
            "ADDRESS_REGION",
            "ORIGINATOR_ID",
            "ORIGIN_ID",
        ]
        for field_name in extra_select or []:
            if field_name and field_name not in select:
                select.append(field_name)

        doc_number = str(doc_number or "").strip()
        bin_number = str(bin_number or "").strip()
        if not doc_number:
            return None

        exact_queries = [
            {"ORIGINATOR_ID": originator_id, "ORIGIN_ID": doc_number},
            {"ORIGINATOR_ID": originator_id, "ORIGIN_ID": f"eQazyna|{doc_number}|{bin_number}"},
            {"ORIGINATOR_ID": "EQAZYNA", "ORIGIN_ID": f"eQazyna|{doc_number}|{bin_number}"},
        ]
        for filter_fields in exact_queries:
            result = self.call(
                "crm.lead.list",
                {
                    "order": {"ID": "DESC"},
                    "filter": filter_fields,
                    "select": select,
                },
            )
            if isinstance(result, list) and result:
                return result[0]

        # Compatibility with old consolidated leads where one lead contained
        # several application blocks and ORIGIN_ID was the BIN.
        fuzzy_queries = [
            {"%TITLE": doc_number},
            {"%COMMENTS": f"Номер заявки: {doc_number}"},
            {"%COMMENTS": f"eQazyna|{doc_number}|{bin_number}"},
        ]
        for filter_fields in fuzzy_queries:
            result = self.call(
                "crm.lead.list",
                {
                    "order": {"ID": "DESC"},
                    "filter": filter_fields,
                    "select": select,
                },
            )
            if isinstance(result, list) and result:
                return result[0]
        return None

    def find_latest_lead_for_company(
        self,
        company_id: str | int,
        extra_select: list[str] | None = None,
    ) -> dict[str, Any] | None:
        select = [
            "ID",
            "TITLE",
            "STATUS_ID",
            "STATUS_DESCRIPTION",
            "STATUS_SEMANTIC_ID",
            "DATE_CREATE",
            "DATE_MODIFY",
            "ASSIGNED_BY_ID",
            "COMPANY_ID",
            "CONTACT_ID",
            "COMMENTS",
            "ORIGINATOR_ID",
            "ORIGIN_ID",
        ]
        for field_name in extra_select or []:
            if field_name and field_name not in select:
                select.append(field_name)
        result = self.call(
            "crm.lead.list",
            {
                "order": {"DATE_MODIFY": "DESC", "ID": "DESC"},
                "filter": {"COMPANY_ID": int(company_id)},
                "select": select,
            },
        )
        return result[0] if isinstance(result, list) and result else None

    def find_latest_lead_for_contact(
        self,
        contact_id: str | int,
        extra_select: list[str] | None = None,
    ) -> dict[str, Any] | None:
        select = [
            "ID",
            "TITLE",
            "STATUS_ID",
            "STATUS_DESCRIPTION",
            "STATUS_SEMANTIC_ID",
            "DATE_CREATE",
            "DATE_MODIFY",
            "ASSIGNED_BY_ID",
            "COMPANY_ID",
            "CONTACT_ID",
            "COMMENTS",
            "ORIGINATOR_ID",
            "ORIGIN_ID",
        ]
        for field_name in extra_select or []:
            if field_name and field_name not in select:
                select.append(field_name)
        result = self.call(
            "crm.lead.list",
            {
                "order": {"DATE_MODIFY": "DESC", "ID": "DESC"},
                "filter": {"CONTACT_ID": int(contact_id)},
                "select": select,
            },
        )
        return result[0] if isinstance(result, list) and result else None

    def find_latest_lead_by_bin(
        self,
        bin_number: str,
        extra_select: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fallback for legacy leads not linked to a company."""
        select = [
            "ID",
            "TITLE",
            "STATUS_ID",
            "STATUS_DESCRIPTION",
            "STATUS_SEMANTIC_ID",
            "DATE_CREATE",
            "DATE_MODIFY",
            "ASSIGNED_BY_ID",
            "COMPANY_ID",
            "CONTACT_ID",
            "COMMENTS",
            "ORIGINATOR_ID",
            "ORIGIN_ID",
        ]
        for field_name in extra_select or []:
            if field_name and field_name not in select:
                select.append(field_name)
        bin_number = str(bin_number or "").strip()
        if not bin_number:
            return None
        queries = [
            {"ORIGINATOR_ID": "EQAZYNA_LEAD", "ORIGIN_ID": bin_number},
            {"ORIGINATOR_ID": "EQAZYNA", "%ORIGIN_ID": bin_number},
            {"%COMMENTS": f"БИН: {bin_number}"},
        ]
        for filter_fields in queries:
            result = self.call(
                "crm.lead.list",
                {
                    "order": {"DATE_MODIFY": "DESC", "ID": "DESC"},
                    "filter": filter_fields,
                    "select": select,
                },
            )
            if isinstance(result, list) and result:
                return result[0]
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

    # ---------- companies ----------

    def get_company(self, company_id: str | int) -> dict[str, Any] | None:
        result = self.call("crm.company.get", {"id": int(company_id)})
        return result if isinstance(result, dict) else None

    def find_company_by_origin(
        self,
        origin_id: str,
        originator_id: str = "EQAZYNA",
    ) -> dict[str, Any] | None:
        result = self.call(
            "crm.company.list",
            {
                "order": {"ID": "ASC"},
                "filter": {
                    "ORIGINATOR_ID": originator_id,
                    "ORIGIN_ID": origin_id,
                },
                "select": [
                    "ID",
                    "TITLE",
                    "ASSIGNED_BY_ID",
                    "COMMENTS",
                    "PHONE",
                    "ADDRESS",
                    "ADDRESS_CITY",
                    "ADDRESS_REGION",
                    "ADDRESS_PROVINCE",
                    "ADDRESS_COUNTRY",
                    "ORIGINATOR_ID",
                    "ORIGIN_ID",
                ],
            },
        )
        return result[0] if isinstance(result, list) and result else None

    def find_company_by_bin(
        self,
        bin_number: str,
        bin_field: str = "RQ_BIN",
    ) -> dict[str, Any] | None:
        requisites = self.call(
            "crm.requisite.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ENTITY_TYPE_ID": 4, bin_field: bin_number},
                "select": ["*"],
            },
        )
        if not isinstance(requisites, list):
            return None
        for requisite in requisites:
            company_id = requisite.get("ENTITY_ID") if isinstance(requisite, dict) else None
            if company_id not in (None, ""):
                company = self.get_company(company_id)
                if company:
                    return company
        return None

    def create_company(self, fields: dict[str, Any]) -> str:
        result = self.call(
            "crm.company.add",
            {
                "fields": fields,
                "params": {"REGISTER_SONET_EVENT": "N"},
            },
        )
        if result in (None, ""):
            raise BitrixError("crm.company.add returned an empty company ID")
        return str(result)

    def update_company(self, company_id: str, fields: dict[str, Any]) -> None:
        self.call(
            "crm.company.update",
            {
                "id": int(company_id),
                "fields": fields,
                "params": {"REGISTER_SONET_EVENT": "N"},
            },
        )

    # ---------- contacts ----------

    def find_director_contact(
        self,
        company_id: str | int,
        last_name: str,
        name: str,
        second_name: str = "",
    ) -> dict[str, Any] | None:
        filter_fields: dict[str, Any] = {
            "COMPANY_ID": int(company_id),
            "LAST_NAME": last_name,
            "NAME": name,
        }
        if second_name:
            filter_fields["SECOND_NAME"] = second_name
        result = self.call(
            "crm.contact.list",
            {
                "order": {"ID": "ASC"},
                "filter": filter_fields,
                "select": [
                    "ID",
                    "NAME",
                    "LAST_NAME",
                    "SECOND_NAME",
                    "POST",
                    "COMPANY_ID",
                    "ASSIGNED_BY_ID",
                    "COMMENTS",
                    "ORIGINATOR_ID",
                    "ORIGIN_ID",
                ],
            },
        )
        return result[0] if isinstance(result, list) and result else None

    def create_contact(self, fields: dict[str, Any]) -> str:
        result = self.call(
            "crm.contact.add",
            {
                "fields": fields,
                "params": {"REGISTER_SONET_EVENT": "N"},
            },
        )
        if result in (None, ""):
            raise BitrixError("crm.contact.add returned an empty contact ID")
        return str(result)

    def update_contact(self, contact_id: str, fields: dict[str, Any]) -> None:
        self.call(
            "crm.contact.update",
            {
                "id": int(contact_id),
                "fields": fields,
                "params": {"REGISTER_SONET_EVENT": "N"},
            },
        )

    # ---------- requisites and addresses ----------

    def find_company_requisite(
        self,
        company_id: str | int,
        bin_number: str,
        bin_field: str = "RQ_BIN",
    ) -> dict[str, Any] | None:
        result = self.call(
            "crm.requisite.list",
            {
                "order": {"ID": "ASC"},
                "filter": {
                    "ENTITY_TYPE_ID": 4,
                    "ENTITY_ID": int(company_id),
                },
                "select": ["*"],
            },
        )
        if not isinstance(result, list):
            return None
        wanted = str(bin_number or "").strip()
        expected_xml = f"EQAZYNA-REQ-{wanted}"
        for row in result:
            if not isinstance(row, dict):
                continue
            if str(row.get(bin_field) or "").strip() == wanted:
                return row
            if str(row.get("XML_ID") or "").strip() == expected_xml:
                return row
        return None

    def create_requisite(self, fields: dict[str, Any]) -> str:
        result = self.call("crm.requisite.add", {"fields": fields})
        if result in (None, ""):
            raise BitrixError("crm.requisite.add returned an empty requisite ID")
        return str(result)

    def update_requisite(self, requisite_id: str, fields: dict[str, Any]) -> None:
        self.call(
            "crm.requisite.update",
            {"id": int(requisite_id), "fields": fields},
        )

    def find_requisite_address(
        self,
        requisite_id: str | int,
        address_type_id: int = 1,
    ) -> dict[str, Any] | None:
        result = self.call(
            "crm.address.list",
            {
                "order": {"TYPE_ID": "ASC"},
                "filter": {
                    "ENTITY_TYPE_ID": 8,
                    "ENTITY_ID": int(requisite_id),
                    "TYPE_ID": int(address_type_id),
                },
            },
        )
        return result[0] if isinstance(result, list) and result else None

    def create_address(self, fields: dict[str, Any]) -> None:
        self.call("crm.address.add", {"fields": fields})

    def update_address(self, fields: dict[str, Any]) -> None:
        self.call("crm.address.update", {"fields": fields})

    # ---------- timeline ----------

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
