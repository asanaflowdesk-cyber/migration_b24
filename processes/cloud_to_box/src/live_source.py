from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from common.bitrix import BitrixClient

LOG = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _extract_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) if isinstance(item, Mapping) else {"VALUE": item} for item in value]
    if isinstance(value, Mapping):
        for key in ("items", "tasks", "users", "result", "addresses", "presets"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [dict(item) if isinstance(item, Mapping) else {"VALUE": item} for item in nested]
        return [dict(value)]
    return []


class LiveCloudSource:
    """Read migration datasets directly from the source Bitrix24 portal.

    The cloud portal is the primary source of truth for every dry-run and apply
    execution. The exported dump is handled outside this class only as a
    fallback/checkpoint when an entire live dataset cannot be read.
    """

    COMMUNICATION_FIELDS = ["PHONE", "EMAIL", "WEB", "IM", "LINK"]

    def __init__(self, client: BitrixClient, warning: Callable[[str, str], None] | None = None):
        self.client = client
        self.warning = warning or (lambda _dataset, _message: None)
        self._cache: dict[str, list[dict[str, Any]]] = {}
        parsed = urlsplit(str(getattr(client, "base", "")))
        self.portal = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""

    def manifest(self) -> dict[str, Any]:
        return {
            "format_version": "live",
            "portal": self.portal,
            "source_mode": "direct_cloud_api",
        }

    def rows(self, dataset: str) -> list[dict[str, Any]]:
        if dataset in self._cache:
            return [dict(item) for item in self._cache[dataset]]
        method = getattr(self, f"_fetch_{dataset}", None)
        if method is None:
            raise KeyError(f"Live source dataset is not implemented: {dataset}")
        LOG.info("Reading %-22s directly from cloud", dataset)
        rows = method()
        normalized = [dict(item) for item in rows if isinstance(item, Mapping)]
        self._cache[dataset] = normalized
        LOG.info("Loaded %-22s %s live rows", dataset, len(normalized))
        return [dict(item) for item in normalized]

    def _field_codes(self, method: str) -> list[str]:
        try:
            payload = self.client.call(method) or {}
        except Exception as exc:  # noqa: BLE001
            self.warning(method, f"field catalog unavailable: {exc}")
            return []
        if isinstance(payload, Mapping) and isinstance(payload.get("fields"), Mapping):
            payload = payload["fields"]
        if not isinstance(payload, Mapping):
            return []
        return [str(code) for code in payload.keys() if str(code)]

    def _crm_entity(
        self,
        *,
        list_method: str,
        fields_method: str,
        communications: bool = False,
    ) -> list[dict[str, Any]]:
        select = self._field_codes(fields_method)
        if communications:
            for code in self.COMMUNICATION_FIELDS:
                if code not in select:
                    select.append(code)
        params: dict[str, Any] = {"order": {"ID": "ASC"}, "filter": {}}
        if select:
            params["select"] = select
        try:
            return self.client.list_all(list_method, params)
        except Exception as first_exc:  # noqa: BLE001
            fallback = ["*", "UF_*"]
            if communications:
                fallback.extend(self.COMMUNICATION_FIELDS)
            self.warning(list_method, f"full field select failed, wildcard retry used: {first_exc}")
            return self.client.list_all(
                list_method,
                {"order": {"ID": "ASC"}, "filter": {}, "select": fallback},
            )

    def _fetch_Companies(self) -> list[dict[str, Any]]:
        return self._crm_entity(
            list_method="crm.company.list",
            fields_method="crm.company.fields",
            communications=True,
        )

    def _fetch_Contacts(self) -> list[dict[str, Any]]:
        return self._crm_entity(
            list_method="crm.contact.list",
            fields_method="crm.contact.fields",
            communications=True,
        )

    def _fetch_Leads(self) -> list[dict[str, Any]]:
        return self._crm_entity(
            list_method="crm.lead.list",
            fields_method="crm.lead.fields",
            communications=True,
        )

    def _fetch_Deals(self) -> list[dict[str, Any]]:
        return self._crm_entity(
            list_method="crm.deal.list",
            fields_method="crm.deal.fields",
            communications=False,
        )

    def _fetch_Requisites(self) -> list[dict[str, Any]]:
        select = self._field_codes("crm.requisite.fields") or ["*"]
        return self.client.list_all(
            "crm.requisite.list",
            {"order": {"ID": "ASC"}, "filter": {}, "select": select},
        )

    def _fetch_Addresses(self) -> list[dict[str, Any]]:
        return self.client.list_all(
            "crm.address.list",
            {"order": {"TYPE_ID": "ASC"}, "filter": {}},
        )

    def _fetch_Requisite_Presets(self) -> list[dict[str, Any]]:
        return self.client.list_all(
            "crm.requisite.preset.list",
            {"order": {"ID": "ASC"}, "filter": {}, "select": ["*"]},
        )

    def _fetch_Requisite_Links(self) -> list[dict[str, Any]]:
        return self.client.list_all(
            "crm.requisite.link.list",
            {"order": {"ENTITY_TYPE_ID": "ASC", "ENTITY_ID": "ASC"}, "filter": {}},
        )

    def _fetch_Users(self) -> list[dict[str, Any]]:
        return self.client.list_all("user.get", {"filter": {}})

    def _fetch_Deal_UserFields(self) -> list[dict[str, Any]]:
        return self.client.list_all(
            "crm.deal.userfield.list",
            {"order": {"ID": "ASC"}, "filter": {}},
        )

    def _fetch_Tasks(self) -> list[dict[str, Any]]:
        fields = self._field_codes("tasks.task.getFields")
        fallback = [
            "ID", "PARENT_ID", "TITLE", "DESCRIPTION", "MARK", "PRIORITY", "STATUS",
            "MULTITASK", "REPLICATE", "GROUP_ID", "STAGE_ID", "CREATED_BY", "CREATED_DATE",
            "RESPONSIBLE_ID", "ACCOMPLICES", "AUDITORS", "CHANGED_BY", "CHANGED_DATE",
            "STATUS_CHANGED_BY", "STATUS_CHANGED_DATE", "CLOSED_BY", "CLOSED_DATE", "DATE_START",
            "DEADLINE", "START_DATE_PLAN", "END_DATE_PLAN", "GUID", "XML_ID", "COMMENTS_COUNT",
            "SERVICE_COMMENTS_COUNT", "ALLOW_CHANGE_DEADLINE", "ALLOW_TIME_TRACKING", "TASK_CONTROL",
            "ADD_IN_REPORT", "TIME_ESTIMATE", "TIME_SPENT_IN_LOGS", "MATCH_WORK_TIME", "SITE_ID",
            "UF_CRM_TASK", "UF_TASK_WEBDAV_FILES", "TAGS", "CHAT_ID",
        ]
        select = fields or fallback
        params = {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": select,
            "params": {
                "WITH_RESULT_INFO": True,
                "WITH_TIMER_INFO": True,
                "WITH_PARSED_DESCRIPTION": False,
            },
        }
        try:
            return self.client.list_all("tasks.task.list", params)
        except Exception as first_exc:  # noqa: BLE001
            self.warning("Tasks", f"dynamic task select failed, stable select retry used: {first_exc}")
            params["select"] = fallback
            return self.client.list_all("tasks.task.list", params)

    def _fetch_CRM_Activities(self) -> list[dict[str, Any]]:
        select = self._field_codes("crm.activity.fields")
        for code in ("COMMUNICATIONS", "BINDINGS", "FILES"):
            if code not in select:
                select.append(code)
        if not select:
            select = ["*", "COMMUNICATIONS", "BINDINGS", "FILES"]
        try:
            return self.client.list_all(
                "crm.activity.list",
                {"order": {"ID": "ASC"}, "filter": {}, "select": select},
            )
        except Exception as first_exc:  # noqa: BLE001
            self.warning("CRM_Activities", f"full activity select failed, wildcard retry used: {first_exc}")
            return self.client.list_all(
                "crm.activity.list",
                {"order": {"ID": "ASC"}, "filter": {}, "select": ["*", "COMMUNICATIONS", "FILES"]},
            )

    def _batch_relations(
        self,
        *,
        source_dataset: str,
        method: str,
        parent_column: str,
    ) -> list[dict[str, Any]]:
        source_rows = self.rows(source_dataset)
        commands: list[tuple[str, str, Mapping[str, Any]]] = []
        for row in source_rows:
            item_id = _text(row.get("ID") or row.get("id"))
            if item_id:
                commands.append((f"item_{item_id}", method, {"id": item_id}))
        output: list[dict[str, Any]] = []
        for success, errors in self.client.batch_chunks(commands, size=40):
            for alias, payload in success.items():
                parent_id = alias.removeprefix("item_")
                for position, record in enumerate(_extract_records(payload), start=1):
                    row = {parent_column: parent_id, "RELATION_ORDER": position}
                    row.update(record)
                    output.append(row)
            for alias, error in errors.items():
                self.warning(
                    parent_column,
                    f"relation {alias.removeprefix('item_')} skipped: {error}",
                )
        return output

    def _fetch_Deal_Contacts(self) -> list[dict[str, Any]]:
        return self._batch_relations(
            source_dataset="Deals",
            method="crm.deal.contact.items.get",
            parent_column="DEAL_ID",
        )

    def _fetch_Lead_Contacts(self) -> list[dict[str, Any]]:
        return self._batch_relations(
            source_dataset="Leads",
            method="crm.lead.contact.items.get",
            parent_column="LEAD_ID",
        )

    def _fetch_Contact_Companies(self) -> list[dict[str, Any]]:
        return self._batch_relations(
            source_dataset="Contacts",
            method="crm.contact.company.items.get",
            parent_column="CONTACT_ID",
        )
