from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from common.bitrix import BitrixClient
from common.naming import (
    build_compact_crm_title,
    extract_eqazyna_client_hint,
    extract_eqazyna_document_number,
    short_organization_name,
)
from .dump_reader import DumpReader
from .live_source import LiveCloudSource
from .file_transfer import FileTransfer
from .reporting import Report

LOG = logging.getLogger(__name__)
MARKER_RE = re.compile(r"\[\[B24MIGRATION:([A-Z_]+):([^:\]]+):([A-Z_]+)\]\]")

READONLY_KEYS = {
    "ID", "DATE_CREATE", "DATE_MODIFY", "MOVED_TIME", "MOVED_BY_ID", "CREATED_BY_ID", "MODIFY_BY_ID",
    "CLOSED", "STAGE_SEMANTIC_ID", "STATUS_SEMANTIC_ID", "IS_NEW", "LAST_ACTIVITY_TIME",
    "LAST_ACTIVITY_BY", "LAST_COMMUNICATION_TIME", "HAS_PHONE", "HAS_EMAIL", "HAS_IMOL",
}
HELPER_SUFFIXES = ("_NAME", "_EMAIL", "_DEPARTMENTS")
CRM_OWNER_TYPES = {1: "lead", 2: "deal", 3: "contact", 4: "company"}
CRM_REF_PREFIX = {"L": 1, "D": 2, "C": 3, "CO": 4}
IMPORT_DATASETS = (
    "Users", "Companies", "Contacts", "Leads", "Deals", "Deal_UserFields",
    "Contact_Companies", "Lead_Contacts", "Deal_Contacts",
    "Requisites", "Addresses", "Requisite_Presets", "Requisite_Links",
    "Tasks", "CRM_Activities",
)


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def normalize_text(value: Any) -> str:
    return " ".join(text(value).strip().casefold().replace("ё", "е").split())


def owner_scoped_requisite_xml_id(
    source_requisite_id: Any,
    source_entity_type: Any,
    target_entity_id: Any,
) -> str:
    """Return a deterministic XML_ID for a requisite recreated under another owner.

    Bitrix24 does not reliably move a requisite between companies/contacts through
    ``crm.requisite.update``. If an XML_ID already exists under another owner,
    reusing that row makes ``crm.requisite.link.register`` fail later. The scoped
    identifier keeps the source requisite stable while making the target owner
    explicit, so repeated runs remain idempotent.
    """
    return (
        f"B24MIG_REQ_{text(source_requisite_id)}"
        f"_E{text(source_entity_type)}"
        f"_T{text(target_entity_id)}"
    )


PRESET_NAME_ALIASES = {
    "3": {
        "физ лицо",
        "физическое лицо",
        "персона",
        "частное лицо",
        "individual",
        "person",
    },
    "4": {
        "юр лицо",
        "юридическое лицо",
        "организация",
        "компания",
        "legal entity",
        "company",
        "organization",
    },
}


def normalize_preset_label(value: Any) -> str:
    normalized = normalize_text(value)
    normalized = normalized.replace("юр.", "юр ").replace("физ.", "физ ")
    normalized = re.sub(r"[^0-9a-zа-я]+", " ", normalized, flags=re.IGNORECASE)
    return " ".join(normalized.split())


def resolve_requisite_preset(
    source_preset: Mapping[str, Any],
    source_entity_type: Any,
    target_presets: Sequence[Mapping[str, Any]],
) -> tuple[int | None, str]:
    """Resolve a target requisite preset without assuming portal IDs are equal.

    ``crm.requisite.preset.list.ENTITY_TYPE_ID`` identifies the parent object
    type of the preset itself and is normally ``8`` (Requisite). It does *not*
    identify whether a concrete requisite belongs to a contact (3) or company
    (4). Therefore owner-type matching must be based on the semantic preset
    name (person/legal entity), not on the preset's ENTITY_TYPE_ID.

    Priority: reserved/default XML_ID, exact normalized name, semantic alias.
    Country is used only as a narrowing signal when it is available. Ambiguous
    matches are never guessed.
    """
    owner_type = text(source_entity_type)
    valid = [row for row in target_presets if text(row.get("ID")).isdigit()]

    source_xml = text(source_preset.get("XML_ID")).strip()
    if source_xml:
        xml_matches = [row for row in valid if text(row.get("XML_ID")).strip() == source_xml]
        if len(xml_matches) == 1:
            return int(xml_matches[0]["ID"]), "XML_ID"

    source_name = normalize_preset_label(source_preset.get("NAME"))
    if source_name:
        exact = [row for row in valid if normalize_preset_label(row.get("NAME")) == source_name]
        if len(exact) == 1:
            return int(exact[0]["ID"]), "exact name"

    aliases = PRESET_NAME_ALIASES.get(owner_type, set())
    if source_name in aliases:
        alias_matches = [
            row for row in valid
            if normalize_preset_label(row.get("NAME")) in aliases
        ]

        source_country = text(source_preset.get("COUNTRY_ID")).strip()
        if source_country:
            same_country = [
                row for row in alias_matches
                if text(row.get("COUNTRY_ID")).strip() == source_country
            ]
            if len(same_country) == 1:
                return int(same_country[0]["ID"]), "semantic alias + country"
            if len(same_country) > 1:
                alias_matches = same_country

        if len(alias_matches) == 1:
            return int(alias_matches[0]["ID"]), "semantic alias"

        candidate_names = ", ".join(
            f"{row.get('ID')}:{row.get('NAME')}" for row in alias_matches
        ) or "none"
        return None, f"ambiguous semantic presets for owner type {owner_type}: {candidate_names}"

    candidate_names = ", ".join(
        f"{row.get('ID')}:{row.get('NAME')}" for row in valid
    ) or "none"
    return None, f"target preset not matched for owner type {owner_type}; available: {candidate_names}"


def normalize_name_tokens(*values: Any) -> tuple[str, ...]:
    merged = " ".join(text(value) for value in values).casefold().replace("ё", "е")
    tokens = re.findall(r"[\w'-]+", merged, flags=re.UNICODE)
    return tuple(sorted(token.strip("'-") for token in tokens if token.strip("'-")))


SOURCE_FIO_RE = re.compile(r"(?:Исходное ФИО|ФИО(?: руководителя)?):\s*([^\r\n]+)", re.IGNORECASE)


def extract_source_fio(row: Mapping[str, Any]) -> str:
    """Return the full source name when it was stored in comments instead of fields."""
    for key in ("COMMENTS", "SOURCE_DESCRIPTION", "POST"):
        match = SOURCE_FIO_RE.search(text(row.get(key)))
        if match:
            return " ".join(match.group(1).strip().split())
    return ""


def restore_contact_name_fields(row: Mapping[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
    """Restore LAST_NAME/NAME/SECOND_NAME from the source full-name note."""
    result = dict(fields)
    if text(result.get("LAST_NAME")) and text(result.get("SECOND_NAME")):
        return result
    full_name = extract_source_fio(row)
    if not full_name:
        return result
    tokens = full_name.split()
    if len(tokens) < 2:
        return result

    current_name = normalize_text(result.get("NAME") or row.get("NAME"))
    normalized_tokens = [normalize_text(token) for token in tokens]
    try:
        name_index = normalized_tokens.index(current_name) if current_name else -1
    except ValueError:
        name_index = -1

    if name_index >= 0:
        if not text(result.get("NAME")):
            result["NAME"] = tokens[name_index]
        if name_index > 0 and not text(result.get("LAST_NAME")):
            result["LAST_NAME"] = " ".join(tokens[:name_index])
        if name_index + 1 < len(tokens) and not text(result.get("SECOND_NAME")):
            result["SECOND_NAME"] = " ".join(tokens[name_index + 1:])
        return result

    if len(tokens) >= 3:
        if not text(result.get("LAST_NAME")):
            result["LAST_NAME"] = tokens[0]
        if not text(result.get("NAME")):
            result["NAME"] = tokens[1]
        if not text(result.get("SECOND_NAME")):
            result["SECOND_NAME"] = " ".join(tokens[2:])
    return result


def contact_display_name(row: Mapping[str, Any]) -> str:
    fields = restore_contact_name_fields(row, {
        "NAME": row.get("NAME"),
        "LAST_NAME": row.get("LAST_NAME"),
        "SECOND_NAME": row.get("SECOND_NAME"),
    })
    return " ".join(
        text(fields.get(key)).strip()
        for key in ("LAST_NAME", "NAME", "SECOND_NAME")
        if text(fields.get(key)).strip()
    )


def append_text(original: Any, addition: str) -> str:
    base = text(original).rstrip()
    if addition in base:
        return base
    return f"{base}\n\n{addition}".strip()


def migration_marker(source_type: str, source_id: Any, target_type: str) -> str:
    return f"[[B24MIGRATION:{source_type.upper()}:{source_id}:{target_type.upper()}]]"


def parse_markers(value: Any) -> list[tuple[str, str, str]]:
    return [match.groups() for match in MARKER_RE.finditer(text(value))]


def parse_marker(value: Any) -> tuple[str, str, str] | None:
    markers = parse_markers(value)
    return markers[0] if markers else None


def bool_y(value: Any) -> bool:
    return text(value).strip().upper() in {"Y", "YES", "TRUE", "1"}


BIN_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
GENERIC_CONTACT_NAMES = {
    "", "без имени", "не указано", "не указан", "руководитель",
    "контакт", "нет данных", "unknown",
}


def extract_bin(row: Mapping[str, Any]) -> str:
    for value in (row.get("ORIGIN_ID"), row.get("COMMENTS"), row.get("SOURCE_DESCRIPTION")):
        match = BIN_RE.search(text(value))
        if match:
            return match.group(1)
    return ""


def _multifield_values(value: Any) -> set[str]:
    result: set[str] = set()
    if not isinstance(value, list):
        return result
    for item in value:
        raw = item.get("VALUE") if isinstance(item, Mapping) else item
        normalized = re.sub(r"[^0-9a-zа-я@.+]+", "", normalize_text(raw), flags=re.IGNORECASE)
        if normalized:
            result.add(normalized)
    return result


def is_director_contact(row: Mapping[str, Any]) -> bool:
    role = f"{normalize_text(row.get('POST'))} {normalize_text(row.get('COMMENTS'))}"
    return "руководител" in role or "director" in role


def is_generic_contact(row: Mapping[str, Any]) -> bool:
    display = normalize_text(contact_display_name(row))
    return display in GENERIC_CONTACT_NAMES or len(normalize_name_tokens(display)) < 2


def source_is_eqazyna(row: Mapping[str, Any]) -> bool:
    origin = normalize_text(row.get("ORIGINATOR_ID"))
    title = normalize_text(row.get("TITLE"))
    comments = normalize_text(row.get("COMMENTS"))
    return origin == "eqazyna" or title.startswith("e-qazyna") or "новая заявка e-qazyna" in comments


def extract_id(result: Any) -> int:
    if isinstance(result, (int, str)) and str(result).isdigit():
        return int(result)
    if isinstance(result, dict):
        for key in ("ID", "id"):
            if str(result.get(key, "")).isdigit():
                return int(result[key])
        for key in ("task", "item", "result"):
            nested = result.get(key)
            value = extract_id(nested)
            if value:
                return value
    return 0


class MigrationProject:
    def __init__(
        self,
        source_dump: str | Path,
        config_path: str | Path,
        users_path: str | Path | None,
        output_dir: str | Path,
        target_client: BitrixClient | None,
        source_client: BitrixClient | None = None,
    ):
        self.source_dump = Path(source_dump)
        self._reader = DumpReader(self.source_dump)
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.users_path = Path(users_path) if users_path else None
        self.report = Report(output_dir)
        self.client = target_client
        self.source_client = source_client
        self._source: dict[str, list[dict[str, Any]]] = {}
        self._manifest: dict[str, Any] = self._reader.manifest()
        self._live_source: LiveCloudSource | None = None
        self._source_origins: dict[str, str] = {}
        self.report.extra["source_registry"] = {
            "primary": "live_cloud_api" if source_client else "dump",
            "dump_role": "offline plan/verification checkpoint; never mixed into a live import",
            "excel_role": "human-readable audit only; not used as migration source",
        }
        self._target_fields: dict[str, dict[str, Any]] = {}
        self._target_statuses: list[dict[str, Any]] = []
        self._target_userfields: dict[str, list[dict[str, Any]]] = {}
        self._source_enum_id_to_value: dict[str, str] = {}
        self._target_lead_enum_value_to_id: dict[str, str] = {}
        self._target_users: list[dict[str, Any]] = []
        self._user_overrides = self._load_user_overrides()
        self._context_user_overrides = self._load_context_user_overrides()
        self._skip_task_user_ids = {text(value) for value in self.config.get("user_assignment", {}).get("skip_tasks_if_participant_source_user_ids", [])}
        self._product_encoded: dict[str, Any] = {}
        self._current_target_user_id = 0
        self._converted_lead_aliases: dict[str, str] | None = None
        self._sample_scope: dict[str, set[str]] | None = None
        self.file_transfer: FileTransfer | None = None

    def _load_user_overrides(self) -> dict[str, int]:
        if not self.users_path or not self.users_path.exists():
            return {}
        raw = self.users_path.read_bytes()
        decoded: str | None = None
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                decoded = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            return {}
        result: dict[str, int] = {}
        for row in csv.DictReader(decoded.splitlines()):
            source_id = text(row.get("source_user_id")).strip()
            target_id = text(row.get("target_user_id")).strip()
            if source_id and target_id.isdigit():
                result[source_id] = int(target_id)
        return result

    def _load_context_user_overrides(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        raw = self.config.get("user_assignment", {}).get("context_overrides", {})
        if not isinstance(raw, dict):
            return result
        for source_id, contexts in raw.items():
            if not isinstance(contexts, dict):
                continue
            parsed: dict[str, int] = {}
            for context in ("crm", "task"):
                value = contexts.get(context)
                if str(value or "").isdigit():
                    parsed[context] = int(value)
            if parsed:
                result[text(source_id)] = parsed
        return result

    def _task_participant_ids(self, row: Mapping[str, Any]) -> set[str]:
        values = [
            row.get("createdBy"),
            row.get("responsibleId"),
            *(row.get("accomplices") or []),
            *(row.get("auditors") or []),
        ]
        return {text(value) for value in values if text(value)}

    def _task_skip_users(self, row: Mapping[str, Any]) -> list[str]:
        return sorted(self._task_participant_ids(row) & self._skip_task_user_ids, key=lambda value: int(value) if value.isdigit() else value)

    def _task_skip_reason(self, row: Mapping[str, Any]) -> str:
        users = self._task_skip_users(row)
        if not users:
            return ""
        return f"task excluded by configured source users: {users}"

    def _context_user_target(self, source_id: Any, context: str, user_map: Mapping[str, int]) -> int | None:
        key = text(source_id)
        override = self._context_user_overrides.get(key, {}).get(context)
        if override:
            return int(override)
        target = user_map.get(key)
        return int(target) if target else None

    def _required_source_user_ids(self) -> set[str]:
        self.load_source("Companies", "Contacts", "Leads", "Deals", "Tasks", "CRM_Activities")
        required: set[str] = set()
        for name in ("Companies", "Contacts", "Deals"):
            required.update(text(row.get("ASSIGNED_BY_ID")) for row in self._source[name] if text(row.get("ASSIGNED_BY_ID")))
        if not self.config.get("skip_original_leads", True):
            required.update(text(row.get("ASSIGNED_BY_ID")) for row in self._source["Leads"] if text(row.get("ASSIGNED_BY_ID")))
        required.update(text(row.get("RESPONSIBLE_ID")) for row in self._source["CRM_Activities"] if text(row.get("RESPONSIBLE_ID")))
        for row in self._source["Tasks"]:
            if self._task_skip_reason(row):
                continue
            required.update(self._task_participant_ids(row))
        return required

    def _source_warning(self, dataset: str, message: str) -> None:
        LOG.warning("Live source %s: %s", dataset, message)
        self.report.add("read_source", dataset, "", dataset, "", "WARN", message)

    def load_source(self, *datasets: str) -> None:
        """Load source datasets without mixing live and exported snapshots.

        Import runs use the cloud portal exclusively. Falling back to an old dump
        for only one failed dataset can silently combine records from different
        moments in time and break relations. Offline plan/map/verify commands may
        still read the exported dump when no source webhook is supplied.
        """
        needed = [name for name in datasets if name not in self._source]
        if not needed:
            return

        if self.source_client and self._live_source is None:
            self._live_source = LiveCloudSource(self.source_client, self._source_warning)
            self._manifest = self._live_source.manifest()

        for name in needed:
            if self._live_source is not None:
                try:
                    rows = self._live_source.rows(name)
                except Exception as exc:  # noqa: BLE001
                    self.report.add(
                        "read_source", name, "", name, "", "ERROR",
                        f"live dataset unavailable; import snapshot not created: {exc}",
                    )
                    raise RuntimeError(f"Live source dataset {name} is unavailable: {exc}") from exc
                self._source[name] = rows
                self._source_origins[name] = "live_cloud_api"
                LOG.info("Source %-22s LIVE %s rows", name, len(rows))
                continue

            rows = self._reader.rows(name)
            self._source[name] = rows
            self._source_origins[name] = "dump"
            LOG.info("Source %-22s DUMP %s rows", name, len(rows))

        self.report.extra["source_mode"] = "direct_cloud_api" if self.source_client else "dump"
        self.report.extra["source_dataset_origins"] = dict(self._source_origins)

    # ---------- target discovery and validation ----------

    def discover_target(self) -> None:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        for entity, method in {
            "company": "crm.company.fields",
            "contact": "crm.contact.fields",
            "lead": "crm.lead.fields",
            "deal": "crm.deal.fields",
            "requisite": "crm.requisite.fields",
            "address": "crm.address.fields",
        }.items():
            payload = self.client.call(method) or {}
            if isinstance(payload, dict) and "fields" in payload and isinstance(payload["fields"], dict):
                payload = payload["fields"]
            self._target_fields[entity] = payload if isinstance(payload, dict) else {}
        self._target_statuses = self.client.list_all("crm.status.list", {"order": {"SORT": "ASC"}})
        self._target_userfields["lead"] = self.client.list_all("crm.lead.userfield.list", {"order": {"ID": "ASC"}})
        self._target_userfields["deal"] = self.client.list_all("crm.deal.userfield.list", {"order": {"ID": "ASC"}})
        self._target_users = self.client.list_all("user.get", {})
        current = self.client.call("user.current") or {}
        self._current_target_user_id = int(current.get("ID") or 0)
        self._build_enum_maps()

    def _build_enum_maps(self) -> None:
        self.load_source("Deal_UserFields")
        source_code = self.config["field_mapping"]["lead_loss_reason"]["source_deal_field"]
        for field in self._source["Deal_UserFields"]:
            if field.get("FIELD_NAME") != source_code:
                continue
            values = field.get("LIST") or field.get("list") or []
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except Exception:
                    values = []
            for item in values or []:
                if isinstance(item, dict):
                    self._source_enum_id_to_value[text(item.get("ID") or item.get("id"))] = text(item.get("VALUE") or item.get("value"))
        target_code = self.config["field_mapping"]["lead_loss_reason"]["target_lead_field"]
        target_field = next((x for x in self._target_userfields.get("lead", []) if x.get("FIELD_NAME") == target_code), None)
        if target_field:
            for item in target_field.get("LIST") or []:
                self._target_lead_enum_value_to_id[normalize_text(item.get("VALUE"))] = text(item.get("ID"))

    def _resolve_product_field(self, entity: str) -> tuple[Any, str | None]:
        cfg = self.config["product_field"]
        code = cfg[f"{entity}_code"]
        value = cfg["value"]
        field = next((x for x in self._target_userfields.get(entity, []) if x.get("FIELD_NAME") == code), None)
        if not field:
            return None, f"{entity}:{code} not found"
        field_type = text(field.get("USER_TYPE_ID"))
        multiple = bool_y(field.get("MULTIPLE"))
        if field_type == "enumeration":
            match = next((item for item in field.get("LIST") or [] if normalize_text(item.get("VALUE")) == normalize_text(value)), None)
            if not match:
                return None, f"{entity}:{code} has no list value {value!r}"
            encoded: Any = text(match.get("ID"))
        elif field_type in {"string", "text", "url"}:
            encoded = value
        else:
            return None, f"{entity}:{code} has unsupported type {field_type!r}"
        if multiple:
            encoded = [encoded]
        return encoded, None

    def validate_target(self) -> dict[str, Any]:
        cfg = self.config
        statuses = {(text(x.get("ENTITY_ID")), text(x.get("STATUS_ID"))): x for x in self._target_statuses}
        missing_statuses: list[str] = []
        wrong_names: list[dict[str, str]] = []
        for entity_id, expected in cfg["target_statuses"].items():
            for status_id, name in expected.items():
                found = statuses.get((entity_id, status_id))
                if not found:
                    missing_statuses.append(f"{entity_id}:{status_id}")
                elif normalize_text(found.get("NAME")) != normalize_text(name):
                    wrong_names.append({"key": f"{entity_id}:{status_id}", "expected": name, "actual": text(found.get("NAME"))})
        missing_fields: list[str] = []
        wrong_types: list[dict[str, str]] = []
        checks = [
            ("lead", cfg["field_mapping"]["lead_loss_reason"]["target_lead_field"], "enumeration"),
            ("deal", cfg["field_mapping"]["deal_contract_number"]["target_deal_field"], "string"),
            ("deal", cfg["field_mapping"]["deal_loss_detail"]["target_deal_field"], "string"),
        ]
        for entity, field_code, expected_type in checks:
            found = next((x for x in self._target_userfields.get(entity, []) if x.get("FIELD_NAME") == field_code), None)
            if not found:
                missing_fields.append(f"{entity}:{field_code}")
            elif text(found.get("USER_TYPE_ID")) != expected_type:
                wrong_types.append({"field": f"{entity}:{field_code}", "expected": expected_type, "actual": text(found.get("USER_TYPE_ID"))})
        expected_enum = [normalize_text(v) for v in cfg["field_mapping"]["lead_loss_reason"]["expected_values"]]
        missing_enum_values = [v for v in expected_enum if v not in self._target_lead_enum_value_to_id]
        product_errors: list[str] = []
        for entity in ("lead", "deal"):
            encoded, error = self._resolve_product_field(entity)
            if error:
                product_errors.append(error)
            else:
                self._product_encoded[entity] = encoded
        result = {
            "missing_statuses": missing_statuses,
            "status_name_differences": wrong_names,
            "missing_fields": missing_fields,
            "wrong_field_types": wrong_types,
            "missing_lead_loss_reason_values": missing_enum_values,
            "product_field_errors": product_errors,
            "ok": not (missing_statuses or missing_fields or wrong_types or missing_enum_values or product_errors),
        }
        self.report.extra["target_validation"] = result
        return result

    # ---------- source plan and routing ----------

    def route_source_deal(self, row: Mapping[str, Any]) -> tuple[str, str]:
        stage = text(row.get("STAGE_ID"))
        routing = self.config["routing"]
        if stage == "LOSE":
            return "lead", routing["lost_deal_target_lead_status"]
        if stage in routing["deal_to_lead_status_map"]:
            return "lead", routing["deal_to_lead_status_map"][stage]
        if stage in routing["deal_stage_map"]:
            return "deal", routing["deal_stage_map"][stage]
        raise ValueError(f"Unmapped source deal stage {stage!r} for deal {row.get('ID')}")

    def source_plan(self) -> dict[str, Any]:
        names = (
            "Companies", "Contacts", "Leads", "Deals", "Requisites", "Addresses", "Deal_Contacts",
            "Lead_Contacts", "Contact_Companies", "Tasks", "CRM_Activities", "Users",
        )
        self.load_source(*names)
        route_counts: Counter[str] = Counter()
        stage_routes: Counter[str] = Counter()
        for deal in self._source["Deals"]:
            kind, target_status = self.route_source_deal(deal)
            route_counts[kind] += 1
            stage_routes[f"{deal.get('STAGE_ID')}->{kind}:{target_status}"] += 1
        included_tasks = [row for row in self._source["Tasks"] if not self._task_skip_reason(row)]
        skipped_tasks = [row for row in self._source["Tasks"] if self._task_skip_reason(row)]
        skipped_by_user: Counter[str] = Counter()
        for row in skipped_tasks:
            for source_user_id in self._task_skip_users(row):
                skipped_by_user[source_user_id] += 1
        task_files = sum(len(row.get("ufTaskWebdavFiles") or []) for row in included_tasks)
        task_comments = sum(int(row.get("commentsCount") or 0) for row in included_tasks)
        activity_files = sum(len(row.get("FILES") or []) for row in self._source["CRM_Activities"])
        original_leads = 0 if self.config.get("skip_original_leads", True) else len(self._source["Leads"])
        plan = {
            "portal": self._manifest.get("portal"),
            "source_counts": {name: len(self._source[name]) for name in names},
            "source_deals_routed_to_leads": route_counts["lead"],
            "source_deals_kept_as_deals": route_counts["deal"],
            "expected_target_leads_total": original_leads + route_counts["lead"],
            "expected_target_deals_total": route_counts["deal"],
            "expected_tasks": len(included_tasks),
            "skipped_tasks": len(skipped_tasks),
            "skipped_tasks_by_source_user": dict(skipped_by_user),
            "expected_activities": len(self._source["CRM_Activities"]),
            "known_task_file_references": task_files,
            "reported_task_comments": task_comments,
            "known_activity_file_references": activity_files,
            "route_breakdown": dict(stage_routes),
            "excluded": ["users creation", "departments", "projects", "workgroups", "scrum", "smart processes", "products"],
        }
        self.report.extra["source_plan"] = plan
        return plan

    # ---------- users ----------

    @staticmethod
    def _user_email(row: Mapping[str, Any]) -> str:
        return normalize_text(row.get("EMAIL") or row.get("email"))

    @staticmethod
    def _user_signature(row: Mapping[str, Any]) -> tuple[str, ...]:
        return normalize_name_tokens(
            row.get("NAME") or row.get("name"),
            row.get("LAST_NAME") or row.get("last_name"),
        )

    def build_user_map(self, *, strict: bool = False) -> dict[str, int]:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        self.load_source("Users")
        if not self._target_users:
            self._target_users = self.client.list_all("user.get", {})
        target_by_id = {int(row["ID"]): row for row in self._target_users if str(row.get("ID", "")).isdigit()}
        target_active = [row for row in self._target_users if bool_y(row.get("ACTIVE", "Y")) or row.get("ACTIVE") is True]
        active_target_ids = {int(row["ID"]) for row in target_active if str(row.get("ID", "")).isdigit()}
        by_email = {self._user_email(row): row for row in target_active if self._user_email(row)}
        by_signature: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in target_active:
            signature = self._user_signature(row)
            if signature:
                by_signature[signature].append(row)
        required_ids = self._required_source_user_ids()
        result: dict[str, int] = {}
        unresolved: list[dict[str, Any]] = []
        unused_unresolved: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []
        for source in self._source["Users"]:
            source_id = text(source.get("ID"))
            context_override = self._context_user_overrides.get(source_id)
            if context_override:
                invalid = {context: target_id for context, target_id in context_override.items() if target_id not in active_target_ids}
                if invalid:
                    self.report.add("map_user", "USER", source_id, "USER", "", "ERROR", f"context target user not found or inactive: {invalid}")
                    unresolved.append(source)
                    continue
                default_target = context_override.get("crm") or context_override.get("task")
                if default_target:
                    result[source_id] = int(default_target)
                detail = ", ".join(f"{context}={target_id}" for context, target_id in sorted(context_override.items()))
                self.report.add("map_user", "USER", source_id, "USER", default_target or "", "OK", f"context override: {detail}")
                continue
            override = self._user_overrides.get(source_id)
            if override:
                target = target_by_id.get(override)
                if target and (bool_y(target.get("ACTIVE", "Y")) or target.get("ACTIVE") is True):
                    result[source_id] = override
                    self.report.add("map_user", "USER", source_id, "USER", override, "OK", "manual target_user_id")
                    continue
                self.report.add("map_user", "USER", source_id, "USER", override, "ERROR", "manual target user not found or inactive")
                unresolved.append(source)
                continue
            email = self._user_email(source)
            if email and email in by_email:
                target = by_email[email]
                result[source_id] = int(target["ID"])
                self.report.add("map_user", "USER", source_id, "USER", target["ID"], "OK", f"email:{email}")
                continue
            signature = self._user_signature(source)
            matches = by_signature.get(signature, [])
            if len(matches) == 1:
                target = matches[0]
                result[source_id] = int(target["ID"])
                self.report.add("map_user", "USER", source_id, "USER", target["ID"], "OK", "name tokens; order ignored")
            elif len(matches) > 1:
                if source_id not in required_ids:
                    unused_unresolved.append(source)
                    self.report.add("map_user", "USER", source_id, "USER", "", "SKIP", f"unused source user; ambiguous matches {[item.get('ID') for item in matches]}")
                else:
                    ambiguous.append({"source": source, "target_ids": [item.get("ID") for item in matches]})
                    self.report.add("map_user", "USER", source_id, "USER", "", "ERROR", f"ambiguous name match: {[item.get('ID') for item in matches]}")
            else:
                if source_id not in required_ids:
                    unused_unresolved.append(source)
                    self.report.add("map_user", "USER", source_id, "USER", "", "SKIP", "source user is not used by imported objects")
                else:
                    unresolved.append(source)
                    self.report.add("map_user", "USER", source_id, "USER", "", "ERROR", "no email or unique name match")
        self.report.maps["users"].update(result)
        self.report.extra["user_mapping"] = {
            "source_users": len(self._source["Users"]),
            "mapped": len(result),
            "unresolved": [text(row.get("ID")) for row in unresolved],
            "unused_unresolved": [text(row.get("ID")) for row in unused_unresolved],
            "context_overrides": self._context_user_overrides,
            "ambiguous": [{"source_id": text(item["source"].get("ID")), "target_ids": item["target_ids"]} for item in ambiguous],
        }
        if strict and (unresolved or ambiguous):
            LOG.warning("Some required source users are not mapped; only objects depending on them will be skipped")
        return result

    def _required_user(self, source_id: Any, user_map: Mapping[str, int], operation: str, source_type: str, object_id: Any, role: str, *, context: str = "crm") -> int | None:
        key = text(source_id)
        target = self._context_user_target(key, context, user_map)
        if target:
            return int(target)
        self.report.add(operation, source_type, object_id, "USER", "", "SKIP", f"unmapped {role} source user {key} for {context}; dependent object skipped")
        return None

    # ---------- coherent sample and client display ----------

    def _build_sample_scope(self, max_items: int) -> dict[str, set[str]]:
        """Build a connected test batch instead of taking unrelated first rows.

        ``max_items`` means up to N routed leads and up to N retained deals.
        Their companies, contacts and contact-company relations are included as
        dependencies even when those records are far from the start of the
        source lists.
        """
        self.load_source("Deals", "Deal_Contacts", "Contacts", "Contact_Companies")
        routed = [
            row for row in self._source["Deals"]
            if self.route_source_deal(row)[0] == "lead"
        ][:max_items]
        retained = [
            row for row in self._source["Deals"]
            if self.route_source_deal(row)[0] == "deal"
        ][:max_items]

        lead_deal_ids = {text(row.get("ID")) for row in routed}
        deal_ids = {text(row.get("ID")) for row in retained}
        selected_deals = routed + retained
        company_ids = {
            text(row.get("COMPANY_ID"))
            for row in selected_deals
            if text(row.get("COMPANY_ID"))
        }
        contact_ids = {
            text(row.get("CONTACT_ID"))
            for row in selected_deals
            if text(row.get("CONTACT_ID"))
        }

        selected_deal_ids = lead_deal_ids | deal_ids
        for relation in self._source["Deal_Contacts"]:
            if text(relation.get("DEAL_ID")) in selected_deal_ids:
                contact_id = text(relation.get("CONTACT_ID"))
                if contact_id:
                    contact_ids.add(contact_id)

        contacts_by_id = {
            text(row.get("ID")): row for row in self._source["Contacts"]
            if text(row.get("ID"))
        }
        changed = True
        while changed:
            changed = False
            for contact_id in list(contact_ids):
                company_id = text(contacts_by_id.get(contact_id, {}).get("COMPANY_ID"))
                if company_id and company_id not in company_ids:
                    company_ids.add(company_id)
                    changed = True
            for relation in self._source["Contact_Companies"]:
                contact_id = text(relation.get("CONTACT_ID"))
                company_id = text(relation.get("COMPANY_ID"))
                if contact_id in contact_ids or company_id in company_ids:
                    if contact_id and contact_id not in contact_ids:
                        contact_ids.add(contact_id)
                        changed = True
                    if company_id and company_id not in company_ids:
                        company_ids.add(company_id)
                        changed = True

        scope = {
            "lead_deal_ids": lead_deal_ids,
            "deal_ids": deal_ids,
            "company_ids": company_ids,
            "contact_ids": contact_ids,
        }
        self.report.extra["coherent_test_scope"] = {
            key: len(value) for key, value in scope.items()
        }
        return scope

    @staticmethod
    def _filter_prepared(
        prepared: Sequence[tuple[str, dict[str, Any]]],
        allowed_source_ids: set[str] | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if allowed_source_ids is None:
            return list(prepared)
        return [
            (source_key, fields)
            for source_key, fields in prepared
            if source_key.split(":", 2)[1] in allowed_source_ids
        ]

    def _source_client_label(self, row: Mapping[str, Any]) -> str:
        self.load_source("Companies", "Contacts")
        company_id = text(row.get("COMPANY_ID"))
        if company_id:
            company = next(
                (item for item in self._source["Companies"] if text(item.get("ID")) == company_id),
                None,
            )
            if company and text(company.get("TITLE")):
                return text(company.get("TITLE")).strip()
        contact_id = text(row.get("CONTACT_ID"))
        if contact_id:
            contact = next(
                (item for item in self._source["Contacts"] if text(item.get("ID")) == contact_id),
                None,
            )
            if contact:
                return contact_display_name(contact)
        return ""

    def _enriched_crm_title(self, row: Mapping[str, Any], fallback: str) -> str:
        original = text(row.get("TITLE")).strip() or fallback
        client = self._source_client_label(row)
        return build_compact_crm_title(client, original, fallback)

    def _select_sample_task_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        max_items: int,
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
    ) -> list[Mapping[str, Any]]:
        if not max_items:
            return list(rows)
        eligible = [row for row in rows if not self._task_skip_reason(row)]
        related = [
            row for row in eligible
            if any(
                self._map_crm_ref(text(reference), company_map, contact_map, lead_map, deal_map)
                for reference in (row.get("ufCrmTask") or [])
            )
        ]
        active = [row for row in eligible if text(row.get("status")) != "5"]
        ordered: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for bucket in (related, active, eligible):
            for row in bucket:
                old_id = text(row.get("id"))
                if old_id in seen:
                    continue
                ordered.append(row)
                seen.add(old_id)
                if len(ordered) >= max_items:
                    break
            if len(ordered) >= max_items:
                break

        by_id = {text(row.get("id")): row for row in eligible}
        selected_ids = {text(row.get("id")) for row in ordered}
        queue = list(selected_ids)
        while queue:
            current = by_id.get(queue.pop())
            parent_id = text((current or {}).get("parentId"))
            if parent_id not in {"", "0"} and parent_id in by_id and parent_id not in selected_ids:
                selected_ids.add(parent_id)
                queue.append(parent_id)
        return [row for row in rows if text(row.get("id")) in selected_ids]

    def _select_sample_activity_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        max_items: int,
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
    ) -> list[Mapping[str, Any]]:
        if not max_items:
            return list(rows)
        selected = []
        for row in rows:
            bindings, _unresolved = self._activity_bindings(
                row, company_map, contact_map, lead_map, deal_map
            )
            if bindings:
                selected.append(row)
            if len(selected) >= max_items:
                break
        return selected

    # ---------- entity preparation ----------

    def _is_target_field_writable(self, entity: str, code: str) -> bool:
        if code in READONLY_KEYS or code.endswith(HELPER_SUFFIXES):
            return False
        meta = self._target_fields.get(entity, {}).get(code)
        if meta is None:
            return False
        lowered = {str(k).casefold(): v for k, v in meta.items()} if isinstance(meta, dict) else {}
        if lowered.get("isreadonly") is True or text(lowered.get("isreadonly")).upper() == "Y":
            return False
        if lowered.get("isimmutable") is True or text(lowered.get("isimmutable")).upper() == "Y":
            return False
        return True

    def _copy_standard_fields(self, entity: str, row: Mapping[str, Any], *, excluded: set[str] | None = None) -> dict[str, Any]:
        excluded = excluded or set()
        result: dict[str, Any] = {}
        for code, value in row.items():
            if code in excluded or not self._is_target_field_writable(entity, code):
                continue
            if value in (None, "", [], {}):
                continue
            result[code] = value
        return result

    def _target_source_id(self, row: Mapping[str, Any]) -> str:
        if source_is_eqazyna(row):
            return self.config["target_source_id"]
        source = text(row.get("SOURCE_ID"))
        available = {text(x.get("STATUS_ID")) for x in self._target_statuses if x.get("ENTITY_ID") == "SOURCE"}
        return source if source in available else "OTHER"

    def _enum_target_id(self, source_enum_id: Any) -> str:
        value = self._source_enum_id_to_value.get(text(source_enum_id), "")
        aliases = self.config["field_mapping"]["lead_loss_reason"].get("value_aliases", {})
        target_value = aliases.get(value, value)
        return self._target_lead_enum_value_to_id.get(normalize_text(target_value), "")

    def _source_loss_reason_value(self, source_enum_id: Any) -> str:
        return self._source_enum_id_to_value.get(text(source_enum_id), "")

    @staticmethod
    def _duplicate_identity_keys(entity: str, row: Mapping[str, Any]) -> set[str]:
        keys = {
            "MARKER:" + ":".join(marker)
            for marker in (
                parse_markers(row.get("COMMENTS"))
                + parse_markers(row.get("ADDITIONAL_INFO"))
            )
        }
        if entity == "company":
            bin_number = extract_bin(row)
            if bin_number:
                keys.add(f"BIN:{bin_number}")
            return keys

        if entity != "contact":
            return keys

        display_name = contact_display_name(row)
        name_tokens = normalize_name_tokens(display_name)
        director = is_director_contact(row)
        generic = is_generic_contact(row)
        company_id = text(row.get("COMPANY_ID"))
        if director and len(name_tokens) >= 2 and company_id:
            # Full FIO is a duplicate key only inside the same company. The
            # same person name in two unrelated companies is not safe to merge.
            keys.add(f"DIRECTOR:{company_id}:" + "|".join(name_tokens))

        # A nameless card is merged with a named director only on an exact
        # phone/email match. A generic name alone is never enough.
        if director or generic:
            for value in _multifield_values(row.get("PHONE")):
                keys.add(f"PHONE:{value}")
            for value in _multifield_values(row.get("EMAIL")):
                keys.add(f"EMAIL:{value}")
        return keys

    @staticmethod
    def _duplicate_primary(entity: str, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        def score(row: Mapping[str, Any]) -> tuple[int, int, int]:
            marker_score = 100 if parse_markers(row.get("COMMENTS")) else 0
            if entity == "company":
                identity_score = 30 if extract_bin(row) else 0
            else:
                identity_score = 30 if not is_generic_contact(row) else 0
                identity_score += min(10, len(normalize_name_tokens(contact_display_name(row))))
            filled = sum(1 for value in row.values() if value not in (None, "", [], {}))
            row_id = int(text(row.get("ID")) or 0)
            return marker_score + identity_score, filled, -row_id

        return max(rows, key=score)

    def _target_duplicate_groups(
        self,
        entity: str,
        *,
        marker_only: bool = False,
    ) -> list[list[dict[str, Any]]]:
        if not self.client or not hasattr(self.client, "list_all"):
            return []
        if entity == "company":
            select = ["ID", "TITLE", "COMMENTS", "ORIGINATOR_ID", "ORIGIN_ID", "PHONE", "EMAIL"]
        elif entity == "contact":
            select = [
                "ID", "NAME", "LAST_NAME", "SECOND_NAME", "POST", "COMMENTS",
                "COMPANY_ID", "PHONE", "EMAIL",
            ]
        else:
            return []

        rows = self.client.list_all(
            f"crm.{entity}.list",
            {"select": select, "order": {"ID": "ASC"}},
        )
        if len(rows) < 2:
            return []

        parent = list(range(len(rows)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        seen: dict[str, int] = {}
        for index, row in enumerate(rows):
            keys = self._duplicate_identity_keys(entity, row)
            if marker_only:
                keys = {key for key in keys if key.startswith("MARKER:")}
            for key in keys:
                if key in seen:
                    union(index, seen[key])
                else:
                    seen[key] = index

        if entity == "contact" and not marker_only:
            # A nameless/one-word contact is attached to the only full-FIO
            # director of the same company. If there is more than one director,
            # no guess is made.
            by_company: dict[str, list[int]] = defaultdict(list)
            for index, row in enumerate(rows):
                company_id = text(row.get("COMPANY_ID"))
                if company_id:
                    by_company[company_id].append(index)
            for indexes in by_company.values():
                directors = [
                    index for index in indexes
                    if is_director_contact(rows[index]) and not is_generic_contact(rows[index])
                ]
                generics = [index for index in indexes if is_generic_contact(rows[index])]
                if len(directors) == 1:
                    for generic_index in generics:
                        union(directors[0], generic_index)

        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for index, row in enumerate(rows):
            grouped[find(index)].append(row)
        return [group for group in grouped.values() if len(group) > 1]

    def consolidate_target_duplicates(
        self,
        *,
        dry_run: bool,
        marker_only: bool = False,
    ) -> None:
        """Merge duplicate company/director cards before rebuilding relations.

        Company duplicates use BIN or the exact migration marker. Contact
        duplicates use the exact marker, exact director FIO, or an exact
        phone/email match involving a nameless/director card.
        """
        if not self.client:
            return
        entity_type_ids = {"contact": 3, "company": 4}
        summary: dict[str, int] = {"company_groups": 0, "contact_groups": 0}

        for entity in ("company", "contact"):
            groups = self._target_duplicate_groups(entity, marker_only=marker_only)
            summary[f"{entity}_groups"] = len(groups)
            for group in groups:
                primary = self._duplicate_primary(entity, group)
                primary_id = int(primary["ID"])
                other_ids = sorted(
                    int(row["ID"]) for row in group if int(row["ID"]) != primary_id
                )
                entity_ids = [primary_id, *other_ids]
                label = text(primary.get("TITLE")) or contact_display_name(primary)
                source_id = ",".join(str(value) for value in entity_ids)
                if dry_run:
                    self.report.add(
                        f"merge_{entity}_duplicates", entity.upper(), source_id,
                        entity.upper(), primary_id, "DRY_RUN",
                        f"main={primary_id}; remove={other_ids}; {label}",
                    )
                    continue
                try:
                    result = self.client.call(
                        "crm.entity.mergeBatch",
                        {"params": {"entityTypeId": entity_type_ids[entity], "entityIds": entity_ids}},
                    ) or {}
                    status = text(result.get("STATUS") if isinstance(result, Mapping) else "")
                    if status != "SUCCESS":
                        raise RuntimeError(f"merge status={status or result}")
                    deleted = result.get("ENTITY_IDS", other_ids) if isinstance(result, Mapping) else other_ids
                    self.report.add(
                        f"merge_{entity}_duplicates", entity.upper(), source_id,
                        entity.upper(), primary_id, "OK", f"deleted={deleted}; {label}",
                    )
                except Exception as exc:  # noqa: BLE001
                    manual_path = f"/crm/{entity}/merge/?id=" + ",".join(str(value) for value in entity_ids)
                    self.report.add(
                        f"merge_{entity}_duplicates", entity.upper(), source_id,
                        entity.upper(), primary_id, "ERROR",
                        f"{exc}; manual={manual_path}",
                    )
        summary["marker_only"] = int(marker_only)
        self.report.extra["target_duplicate_consolidation"] = summary

    def normalize_existing_target_titles(
        self,
        *,
        dry_run: bool,
        full_cleanup: bool,
    ) -> None:
        """Rename already-created migration/e-Qazyna cards to compact titles.

        Limited apply runs intentionally do not perform a portal-wide cleanup.
        Their selected source objects are still renamed by ``_batch_create``.
        A full apply and every dry-run inspect all managed target cards.
        """
        summary = {
            "company_updates": 0,
            "lead_updates": 0,
            "deal_updates": 0,
            "skipped_limited_apply": int(not full_cleanup),
        }
        self.report.extra["target_title_normalization"] = summary
        if not full_cleanup or not self.client or not hasattr(self.client, "list_all"):
            return

        company_rows = self.client.list_all(
            "crm.company.list",
            {
                "select": ["ID", "TITLE", "COMMENTS", "ORIGINATOR_ID", "ORIGIN_ID"],
                "order": {"ID": "ASC"},
            },
        )
        company_titles: dict[str, str] = {}
        changes: dict[str, list[tuple[int, dict[str, Any], str]]] = {
            "company": [],
            "lead": [],
            "deal": [],
        }

        for row in company_rows:
            target_id = int(text(row.get("ID")) or 0)
            old_title = text(row.get("TITLE")).strip()
            managed = bool(parse_markers(row.get("COMMENTS"))) or normalize_text(
                row.get("ORIGINATOR_ID")
            ).startswith("eqazyna")
            new_title = short_organization_name(old_title) if managed else old_title
            company_titles[str(target_id)] = new_title or old_title
            if target_id and new_title and new_title != old_title:
                changes["company"].append((target_id, {"TITLE": new_title}, old_title))

        for entity in ("lead", "deal"):
            select = [
                "ID", "TITLE", "COMPANY_ID", "COMMENTS",
                "ORIGINATOR_ID", "ORIGIN_ID",
            ]
            if entity == "lead":
                select.append("COMPANY_TITLE")
            else:
                select.append("ADDITIONAL_INFO")
            rows = self.client.list_all(
                f"crm.{entity}.list",
                {"select": select, "order": {"ID": "ASC"}},
            )
            for row in rows:
                target_id = int(text(row.get("ID")) or 0)
                old_title = text(row.get("TITLE")).strip()
                originator = normalize_text(row.get("ORIGINATOR_ID"))
                is_eqazyna = "e-qazyna" in normalize_text(old_title) or originator.startswith("eqazyna")
                if not target_id or not is_eqazyna:
                    continue

                company_id = text(row.get("COMPANY_ID"))
                client_name = company_titles.get(company_id, "")
                if not client_name:
                    client_name = short_organization_name(row.get("COMPANY_TITLE"))
                if not client_name:
                    client_name = extract_eqazyna_client_hint(old_title)

                document_number = extract_eqazyna_document_number(old_title)
                source_title = (
                    f"e-Qazyna № {document_number}" if document_number else "e-Qazyna"
                )
                new_title = build_compact_crm_title(
                    client_name, source_title, source_title
                )
                fields: dict[str, Any] = {}
                if new_title and new_title != old_title:
                    fields["TITLE"] = new_title
                if entity == "lead" and text(row.get("COMPANY_TITLE")):
                    compact_company_title = short_organization_name(row.get("COMPANY_TITLE"))
                    if compact_company_title != text(row.get("COMPANY_TITLE")).strip():
                        fields["COMPANY_TITLE"] = compact_company_title
                if fields:
                    changes[entity].append((target_id, fields, old_title))

        for entity, entity_changes in changes.items():
            summary[f"{entity}_updates"] = len(entity_changes)
            if dry_run:
                for target_id, fields, old_title in entity_changes:
                    self.report.add(
                        f"normalize_{entity}_title", entity.upper(), target_id,
                        entity.upper(), target_id, "DRY_RUN",
                        f"{old_title} -> {fields.get('TITLE', old_title)}",
                    )
                continue

            commands: list[tuple[str, str, Mapping[str, Any]]] = []
            contexts: dict[str, tuple[int, dict[str, Any], str]] = {}
            for index, (target_id, fields, old_title) in enumerate(entity_changes):
                key = f"{entity[0]}{index}"
                commands.append(
                    (key, f"crm.{entity}.update", {"id": target_id, "fields": fields})
                )
                contexts[key] = (target_id, fields, old_title)
            for success, errors in self.client.batch_chunks(commands, size=35):
                for key in success:
                    target_id, fields, old_title = contexts[key]
                    self.report.add(
                        f"normalize_{entity}_title", entity.upper(), target_id,
                        entity.upper(), target_id, "OK",
                        f"{old_title} -> {fields.get('TITLE', old_title)}",
                    )
                for key, error in errors.items():
                    target_id, fields, old_title = contexts[key]
                    self.report.add(
                        f"normalize_{entity}_title", entity.upper(), target_id,
                        entity.upper(), target_id, "ERROR",
                        f"{error}; {old_title} -> {fields.get('TITLE', old_title)}",
                    )

    def _existing_markers(self, entity: str) -> dict[tuple[str, str, str], int]:
        method = f"crm.{entity}.list"
        select = ["ID", "COMMENTS"]
        if entity == "deal":
            select.append("ADDITIONAL_INFO")
        rows = self.client.list_all(method, {"select": select, "order": {"ID": "ASC"}}) if self.client else []
        result: dict[tuple[str, str, str], int] = {}
        for row in rows:
            markers = parse_markers(row.get("COMMENTS")) + parse_markers(row.get("ADDITIONAL_INFO"))
            for marker in markers:
                # Keep the oldest/main card when a failed previous run left two
                # cards carrying the same migration marker.
                result.setdefault(marker, int(row["ID"]))
        return result

    def _batch_create(self, entity: str, prepared: list[tuple[str, dict[str, Any]]], *, dry_run: bool, max_items: int = 0) -> dict[str, int]:
        add_method = f"crm.{entity}.add"
        update_method = f"crm.{entity}.update"
        target_type = entity.upper()
        existing = self._existing_markers(entity) if not dry_run else {}
        mapping: dict[str, int] = {}
        commands: list[tuple[str, str, Mapping[str, Any]]] = []
        contexts: dict[str, dict[str, Any]] = {}
        limited = prepared[:max_items] if max_items else prepared

        for index, (source_key, fields) in enumerate(limited):
            source_type, source_id, route = source_key.split(":", 2)
            marker_key = (source_type, source_id, route)
            if dry_run:
                synthetic_id = -(index + 1)
                mapping[source_key] = synthetic_id
                self.report.add(
                    f"create_{entity}", source_type, source_id, target_type,
                    synthetic_id, "DRY_RUN", text(fields.get("TITLE") or fields.get("NAME")),
                )
                self.report.add_transfer(
                    operation=f"create_{entity}",
                    source_type=source_type,
                    source_id=source_id,
                    target_type=target_type,
                    target_id=synthetic_id,
                    status="DRY_RUN",
                    payload=fields,
                    route=route,
                )
                continue

            if marker_key in existing:
                target_id = existing[marker_key]
                mapping[source_key] = target_id
                cmd_key = f"u{index}"
                commands.append((cmd_key, update_method, {"id": target_id, "fields": fields}))
                contexts[cmd_key] = {
                    "source_key": source_key,
                    "fields": fields,
                    "target_id": target_id,
                    "operation": f"update_{entity}",
                }
                continue

            cmd_key = f"i{index}"
            commands.append((cmd_key, add_method, {"fields": fields}))
            contexts[cmd_key] = {
                "source_key": source_key,
                "fields": fields,
                "target_id": 0,
                "operation": f"create_{entity}",
            }

        if dry_run:
            return mapping

        for success, errors in self.client.batch_chunks(commands, size=35):
            for cmd_key, raw_target in success.items():
                context = contexts[cmd_key]
                source_key = context["source_key"]
                fields = context["fields"]
                source_type, source_id, route = source_key.split(":", 2)
                operation = context["operation"]
                target_id = int(context["target_id"] or 0)
                if not target_id:
                    target_id = extract_id(raw_target)
                if not target_id:
                    self.report.add(
                        operation, source_type, source_id, target_type, "", "SKIP",
                        f"Bitrix returned no target ID: {raw_target}",
                    )
                    self.report.add_transfer(
                        operation=operation,
                        source_type=source_type,
                        source_id=source_id,
                        target_type=target_type,
                        target_id="",
                        status="SKIP",
                        payload=fields,
                        route=route,
                    )
                    continue
                mapping[source_key] = target_id
                self.report.add(operation, source_type, source_id, target_type, target_id, "OK", route)
                self.report.add_transfer(
                    operation=operation,
                    source_type=source_type,
                    source_id=source_id,
                    target_type=target_type,
                    target_id=target_id,
                    status="OK",
                    payload=fields,
                    route=route,
                )

            for cmd_key, error in errors.items():
                context = contexts[cmd_key]
                source_key = context["source_key"]
                fields = context["fields"]
                source_type, source_id, route = source_key.split(":", 2)
                operation = context["operation"]
                target_id = context["target_id"]
                status = "WARN" if target_id else "SKIP"
                self.report.add(operation, source_type, source_id, target_type, target_id, status, text(error))
                self.report.add_transfer(
                    operation=operation,
                    source_type=source_type,
                    source_id=source_id,
                    target_type=target_type,
                    target_id=target_id,
                    status=status,
                    payload=fields,
                    route=route,
                )
                if target_id:
                    mapping[source_key] = int(target_id)
        return mapping

    def prepare_companies(self, user_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Companies")
        prepared = []
        for row in self._source["Companies"]:
            old_id = text(row.get("ID"))
            responsible = self._required_user(row.get("ASSIGNED_BY_ID"), user_map, "prepare_company", "COMPANY", old_id, "responsible")
            if not responsible:
                continue
            fields = self._copy_standard_fields("company", row, excluded={"ASSIGNED_BY_ID", "COMMENTS", "SOURCE_ID"})
            compact_title = short_organization_name(row.get("TITLE"))
            if compact_title:
                fields["TITLE"] = compact_title
            fields["ASSIGNED_BY_ID"] = responsible
            if "SOURCE_ID" in self._target_fields["company"]:
                fields["SOURCE_ID"] = self._target_source_id(row)
            fields["COMMENTS"] = append_text(row.get("COMMENTS"), migration_marker("COMPANY", old_id, "COMPANY"))
            prepared.append((f"COMPANY:{old_id}:COMPANY", fields))
        return prepared

    def prepare_contacts(self, user_map: Mapping[str, int], company_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Contacts")
        prepared = []
        for row in self._source["Contacts"]:
            old_id = text(row.get("ID"))
            responsible = self._required_user(row.get("ASSIGNED_BY_ID"), user_map, "prepare_contact", "CONTACT", old_id, "responsible")
            if not responsible:
                continue
            fields = self._copy_standard_fields("contact", row, excluded={"ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "SOURCE_ID"})
            fields = restore_contact_name_fields(row, fields)
            fields["ASSIGNED_BY_ID"] = responsible
            old_company = text(row.get("COMPANY_ID"))
            if old_company and old_company in company_map:
                fields["COMPANY_ID"] = company_map[old_company]
            if "SOURCE_ID" in self._target_fields["contact"]:
                fields["SOURCE_ID"] = self._target_source_id(row)
            fields["COMMENTS"] = append_text(row.get("COMMENTS"), migration_marker("CONTACT", old_id, "CONTACT"))
            prepared.append((f"CONTACT:{old_id}:CONTACT", fields))
        return prepared

    def prepare_original_leads(self, user_map: Mapping[str, int], company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Leads")
        if self.config.get("skip_original_leads", True):
            for row in self._source["Leads"]:
                self.report.add("create_lead", "LEAD", row.get("ID"), "LEAD", "", "SKIP", "original cloud lead excluded by migration rule")
            return []
        prepared = []
        status_map = self.config["routing"]["source_lead_status_map"]
        for row in self._source["Leads"]:
            old_id = text(row.get("ID"))
            responsible = self._required_user(row.get("ASSIGNED_BY_ID"), user_map, "prepare_lead", "LEAD", old_id, "responsible")
            if not responsible:
                continue
            fields = self._copy_standard_fields("lead", row, excluded={"ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "CONTACT_ID", "STATUS_ID", "SOURCE_ID"})
            fields.update({
                "TITLE": self._enriched_crm_title(row, f"Лид {old_id}"),
                "ASSIGNED_BY_ID": responsible,
                "STATUS_ID": status_map.get(text(row.get("STATUS_ID")), "NEW"),
                "SOURCE_ID": self._target_source_id(row),
                self.config["product_field"]["lead_code"]: self._product_encoded["lead"],
            })
            if text(row.get("COMPANY_ID")) in company_map:
                fields["COMPANY_ID"] = company_map[text(row.get("COMPANY_ID"))]
            if text(row.get("CONTACT_ID")) in contact_map:
                fields["CONTACT_ID"] = contact_map[text(row.get("CONTACT_ID"))]
            fields["COMMENTS"] = append_text(row.get("COMMENTS"), migration_marker("LEAD", old_id, "LEAD"))
            prepared.append((f"LEAD:{old_id}:LEAD", fields))
        return prepared

    def prepare_routed_deal_leads(self, user_map: Mapping[str, int], company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Deals")
        prepared = []
        loss_cfg = self.config["field_mapping"]["lead_loss_reason"]
        detail_cfg = self.config["field_mapping"]["deal_loss_detail"]
        contract_cfg = self.config["field_mapping"]["deal_contract_number"]
        for row in self._source["Deals"]:
            route, target_status = self.route_source_deal(row)
            if route != "lead":
                continue
            old_id = text(row.get("ID"))
            responsible = self._required_user(row.get("ASSIGNED_BY_ID"), user_map, "prepare_lead", "DEAL", old_id, "responsible")
            if not responsible:
                continue
            fields = self._copy_standard_fields("lead", row, excluded={
                "ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "CONTACT_ID", "STAGE_ID", "STATUS_ID", "SOURCE_ID", "CATEGORY_ID"
            })
            fields.update({
                "TITLE": self._enriched_crm_title(row, f"Сделка {old_id}"),
                "ASSIGNED_BY_ID": responsible,
                "STATUS_ID": target_status,
                "SOURCE_ID": self._target_source_id(row),
                self.config["product_field"]["lead_code"]: self._product_encoded["lead"],
            })
            old_company = text(row.get("COMPANY_ID")); old_contact = text(row.get("CONTACT_ID"))
            if old_company in company_map:
                fields["COMPANY_ID"] = company_map[old_company]
            if old_contact in contact_map:
                fields["CONTACT_ID"] = contact_map[old_contact]
            enum_id = self._enum_target_id(row.get(loss_cfg["source_deal_field"]))
            if enum_id:
                fields[loss_cfg["target_lead_field"]] = enum_id
            comments = text(row.get("COMMENTS"))
            detail = text(row.get(detail_cfg["source_deal_field"]))
            contract = text(row.get(contract_cfg["source_deal_field"]))
            if detail:
                comments = append_text(comments, f"Детальная причина срыва из облака: {detail}")
            if contract:
                comments = append_text(comments, f"Номер договора из облака: {contract}")
            fields["COMMENTS"] = append_text(comments, migration_marker("DEAL", old_id, "LEAD"))
            prepared.append((f"DEAL:{old_id}:LEAD", fields))
        return prepared

    def prepare_deals(self, user_map: Mapping[str, int], company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Deals")
        prepared = []
        loss_cfg = self.config["field_mapping"]["lead_loss_reason"]
        detail_cfg = self.config["field_mapping"]["deal_loss_detail"]
        contract_cfg = self.config["field_mapping"]["deal_contract_number"]
        for row in self._source["Deals"]:
            route, target_stage = self.route_source_deal(row)
            if route != "deal":
                continue
            old_id = text(row.get("ID"))
            responsible = self._required_user(row.get("ASSIGNED_BY_ID"), user_map, "prepare_deal", "DEAL", old_id, "responsible")
            if not responsible:
                continue
            fields = self._copy_standard_fields("deal", row, excluded={
                "ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "CONTACT_ID", "LEAD_ID", "MYCOMPANY_ID", "QUOTE_ID",
                "PREVIOUS_STAGE_ID", "STAGE_ID", "SOURCE_ID", "CATEGORY_ID", "BEGINDATE", "CLOSEDATE"
            })
            fields.update({
                "TITLE": self._enriched_crm_title(row, f"Сделка {old_id}"),
                "ASSIGNED_BY_ID": responsible,
                "STAGE_ID": target_stage,
                "CATEGORY_ID": int(self.config.get("target_deal_category_id", 0)),
                "SOURCE_ID": self._target_source_id(row),
                self.config["product_field"]["deal_code"]: self._product_encoded["deal"],
            })
            old_company = text(row.get("COMPANY_ID")); old_contact = text(row.get("CONTACT_ID"))
            if old_company in company_map:
                fields["COMPANY_ID"] = company_map[old_company]
            if old_contact in contact_map:
                fields["CONTACT_ID"] = contact_map[old_contact]
            contract = text(row.get(contract_cfg["source_deal_field"]))
            if contract:
                fields[contract_cfg["target_deal_field"]] = contract
            detail = text(row.get(detail_cfg["source_deal_field"]))
            reason_value = self._source_loss_reason_value(row.get(loss_cfg["source_deal_field"]))
            combined = detail
            if reason_value:
                combined = f"Причина: {reason_value}" + (f"\nДетали: {detail}" if detail else "")
            if combined:
                fields[detail_cfg["target_deal_field"]] = combined
            fields["COMMENTS"] = append_text(row.get("COMMENTS"), migration_marker("DEAL", old_id, "DEAL"))
            prepared.append((f"DEAL:{old_id}:DEAL", fields))
        return prepared

    def validate_live_source(self) -> dict[str, Any]:
        """Probe direct cloud access without blocking on optional child data.

        Main CRM cards, relations, tasks and activities are read from the live
        cloud portal. Comments, checklists and binary files are best-effort child
        data: failures are logged and the remaining records continue.
        """
        if not self.source_client:
            raise RuntimeError("SOURCE_BITRIX_WEBHOOK_URL is required")
        self.load_source("Tasks", "CRM_Activities")
        checks: dict[str, Any] = {}
        errors: list[str] = []
        warnings: list[str] = []

        def check(label: str, callback: Any, *, blocking: bool = False) -> Any:
            try:
                value = callback()
                checks[label] = "OK"
                return value
            except Exception as exc:
                prefix = "ERROR" if blocking else "WARN"
                checks[label] = f"{prefix}: {exc}"
                target = errors if blocking else warnings
                target.append(f"{label}: {exc}")
                return None

        check("source_user", lambda: self.source_client.call("user.current"))

        # Compare the live portal with the export checkpoint, but never block
        # the migration only because the live portal now contains fewer rows.
        # Records may have been deleted, converted or hidden after the dump was
        # created. Under the project-wide skip-and-log policy we migrate every
        # record that is currently readable and record all count gaps for later
        # investigation. The checkpoint remains a diagnostic lower bound only.
        checkpoint_path = self.config_path.with_name("source_plan.json")
        if checkpoint_path.exists() and all(name in self._source for name in IMPORT_DATASETS):
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                expected_counts = dict(checkpoint.get("source_counts") or {})
                expected_counts["Addresses"] = int(
                    checkpoint.get("expected_unique_addresses")
                    or expected_counts.get("Addresses")
                    or 0
                )
                count_check: dict[str, Any] = {}
                for dataset in (
                    "Companies", "Contacts", "Deals", "Requisites", "Addresses",
                    "Contact_Companies", "Deal_Contacts", "Tasks", "CRM_Activities",
                ):
                    expected = int(expected_counts.get(dataset) or 0)
                    actual = (
                        len(self._unique_source_addresses())
                        if dataset == "Addresses"
                        else len(self._source.get(dataset, []))
                    )
                    count_check[dataset] = {"checkpoint_minimum": expected, "live": actual}
                    if expected and actual < expected:
                        missing = expected - actual
                        message = (
                            f"live source returned {actual} {dataset} rows, below checkpoint {expected} "
                            f"by {missing}; the readable rows will be migrated and the gap is logged "
                            "for follow-up (possible deletion, conversion, relation cleanup or webhook visibility)"
                        )
                        warnings.append(f"source_count_{dataset}: {message}")
                checks["source_dataset_counts"] = count_check
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"source_dataset_counts: checkpoint could not be evaluated: {exc}")
                checks["source_dataset_counts"] = f"WARN: {exc}"

        first_task = next((row for row in self._source["Tasks"] if text(row.get("id")).isdigit()), None)
        if first_task:
            task_id = int(first_task["id"])
            check("source_task", lambda: self.source_client.call("tasks.task.get", {"taskId": task_id, "select": ["ID", "TITLE", "UF_TASK_WEBDAV_FILES", "CHAT_ID"]}))

        task_with_comments = next(
            (
                row
                for row in self._source["Tasks"]
                if int(row.get("commentsCount") or 0)
                > int(row.get("serviceCommentsCount") or 0)
            ),
            None,
        )
        if task_with_comments:
            task_id = int(task_with_comments["id"])
            comments = self._fetch_task_comments(self.source_client, task_id)
            if comments:
                checks["source_task_comments"] = f"OK: {len(comments)}"
            else:
                message = (
                    f"task {task_id} reports comments but none were returned after classic REST and REST 3.0 chat lookup; "
                    "check that SOURCE_BITRIX_WEBHOOK_URL includes the im scope and that its user is a participant of the task chat"
                )
                checks["source_task_comments"] = f"WARN: {message}"
                warnings.append(f"source_task_comments: {message}")

        task_with_file = next((row for row in self._source["Tasks"] if row.get("ufTaskWebdavFiles")), None)
        if task_with_file:
            file_ref = (task_with_file.get("ufTaskWebdavFiles") or [None])[0]
            check("source_task_file", lambda: self.source_client.call("disk.attachedObject.get", {"id": int(text(file_ref).removeprefix("n"))}))
        else:
            warnings.append("No task file reference was available for the preflight check")

        first_activity = next((row for row in self._source["CRM_Activities"] if text(row.get("ID")).isdigit()), None)
        if first_activity:
            activity_id = int(first_activity["ID"])
            check("source_activity", lambda: self.source_client.call("crm.activity.get", {"id": activity_id}))
            check("source_activity_bindings", lambda: self.source_client.call("crm.activity.binding.list", {"activityId": activity_id}))

        result = {
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
            "ok": not errors,
            "count_gap_policy": "warn_and_continue",
        }
        self.report.extra["live_source_validation"] = result
        return result

    # ---------- main import ----------

    def import_all(self, *, dry_run: bool = False, max_items: int = 0) -> None:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        if not self.source_client:
            raise RuntimeError("SOURCE_BITRIX_WEBHOOK_URL is required for direct cloud-to-box import")

        # Freeze one coherent source snapshot before any target write. This avoids
        # reading companies at one moment and relations/tasks from a later state.
        self.load_source(*IMPORT_DATASETS)
        self.report.extra["source_snapshot"] = {
            "captured_at": datetime.now().astimezone().isoformat(),
            "datasets": {name: len(self._source.get(name, [])) for name in IMPORT_DATASETS},
            "origins": dict(self._source_origins),
        }
        self.discover_target()
        validation = self.validate_target()
        if not validation["ok"]:
            raise RuntimeError(f"Target validation failed: {json.dumps(validation, ensure_ascii=False)}")
        source_validation = self.validate_live_source()
        if not source_validation["ok"]:
            raise RuntimeError(f"Live cloud validation failed: {json.dumps(source_validation, ensure_ascii=False)}")

        # Clean up duplicates left by previous test runs before any relation is
        # rebuilt. In dry-run this only records the planned merge groups.
        self.consolidate_target_duplicates(
            dry_run=dry_run,
            marker_only=(not dry_run and max_items > 0),
        )
        self.normalize_existing_target_titles(
            dry_run=dry_run,
            full_cleanup=(dry_run or max_items == 0),
        )

        self.report.extra["best_effort_policy"] = {
            "enabled": True,
            "non_blocking": [
                "source task comments",
                "task and checklist files",
                "task checklists",
                "CRM activity files",
                "live activity binding enrichment",
            ],
            "note": "Any individual object or subobject that cannot be processed is skipped and recorded. The workflow continues.",
        }
        user_map = self.build_user_map(strict=True)
        portal_match = re.match(r"https?://[^/]+", text(getattr(self.client, "base", "")))
        if portal_match:
            self.report.extra["target_portal"] = portal_match.group(0)

        self._sample_scope = self._build_sample_scope(max_items) if max_items else None
        entity_limit = 0 if self._sample_scope else max_items

        if not dry_run:
            self.file_transfer = FileTransfer(
                self.source_client,
                self.client,
                self.report,
                target_folder_id=int(self.config.get("target_files_folder_id", 0)),
                folder_name=self.config.get("target_files_folder_name", "B24 migration files"),
                max_bytes=int(self.config.get("max_file_bytes", 104857600)),
            )

        companies = self.prepare_companies(user_map)
        companies = self._filter_prepared(
            companies,
            self._sample_scope["company_ids"] if self._sample_scope else None,
        )
        company_keys = self._batch_create("company", companies, dry_run=dry_run, max_items=entity_limit)
        company_map = {key.split(":")[1]: value for key, value in company_keys.items()}
        self.report.maps["companies"].update(company_map)

        contacts = self.prepare_contacts(user_map, company_map)
        contacts = self._filter_prepared(
            contacts,
            self._sample_scope["contact_ids"] if self._sample_scope else None,
        )
        contact_keys = self._batch_create("contact", contacts, dry_run=dry_run, max_items=entity_limit)
        contact_map = {key.split(":")[1]: value for key, value in contact_keys.items()}
        self.report.maps["contacts"].update(contact_map)

        if not dry_run:
            self.import_contact_company_relations(contact_map, company_map)
            self.import_requisites(company_map, contact_map)

        leads = self.prepare_original_leads(user_map, company_map, contact_map)
        routed_leads = self.prepare_routed_deal_leads(user_map, company_map, contact_map)
        if self._sample_scope:
            routed_leads = self._filter_prepared(routed_leads, self._sample_scope["lead_deal_ids"])
        leads += routed_leads
        lead_keys = self._batch_create("lead", leads, dry_run=dry_run, max_items=entity_limit)
        lead_map = dict(lead_keys)
        self.report.maps["leads"].update(lead_map)

        deals = self.prepare_deals(user_map, company_map, contact_map)
        if self._sample_scope:
            deals = self._filter_prepared(deals, self._sample_scope["deal_ids"])
        deal_keys = self._batch_create("deal", deals, dry_run=dry_run, max_items=entity_limit)
        deal_map = {key.split(":")[1]: value for key, value in deal_keys.items()}
        self.report.maps["deals"].update(deal_map)

        if dry_run:
            # Build the same target-side records as apply, but do not write them.
            # The dry-run artifact must therefore contain requisites, addresses,
            # all CRM relations, tasks and activities in addition to the main
            # company/contact/lead/deal payloads.
            self._dry_run_requisites_and_addresses(
                company_map, contact_map, max_items=max_items
            )
            self._record_crm_relation_registry(
                company_map, contact_map, lead_map, deal_map, status="DRY_RUN"
            )
            self._dry_run_tasks_activities(
                user_map, company_map, contact_map, lead_map, deal_map, max_items=max_items
            )
            return

        self.import_crm_contact_relations(contact_map, lead_map, deal_map)
        self.import_requisite_links(deal_map, lead_map)
        self._record_crm_relation_registry(
            company_map, contact_map, lead_map, deal_map, status="APPLIED"
        )
        self.import_tasks(user_map, company_map, contact_map, lead_map, deal_map, max_items=max_items)
        self.import_activities(user_map, company_map, contact_map, lead_map, deal_map, max_items=max_items)


    def _record_crm_relation_registry(
        self,
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
        *,
        status: str,
    ) -> None:
        """Write every source CRM relation to a separate audit register."""
        self.load_source("Contact_Companies", "Lead_Contacts", "Deal_Contacts", "Requisite_Links")

        for row in self._source["Contact_Companies"]:
            old_contact = text(row.get("CONTACT_ID"))
            old_company = text(row.get("COMPANY_ID"))
            mapped = old_contact in contact_map and old_company in company_map
            self.report.add_relation(
                relation_type="CONTACT_COMPANY",
                source_from_type="CONTACT",
                source_from_id=old_contact,
                source_to_type="COMPANY",
                source_to_id=old_company,
                target_from_type="CONTACT" if mapped else "",
                target_from_id=contact_map.get(old_contact, ""),
                target_to_type="COMPANY" if mapped else "",
                target_to_id=company_map.get(old_company, ""),
                status=status if mapped else "SKIP",
                details={
                    "is_primary": row.get("IS_PRIMARY"),
                    "sort": row.get("SORT"),
                    "role_id": row.get("ROLE_ID"),
                },
            )

        aliases = self._converted_lead_to_deal()
        for row in self._source["Lead_Contacts"]:
            old_lead = text(row.get("LEAD_ID"))
            old_contact = text(row.get("CONTACT_ID"))
            target_kind = ""
            target_crm_id: Any = ""
            source_key = f"LEAD:{old_lead}:LEAD"
            if source_key in lead_map:
                target_kind, target_crm_id = "LEAD", lead_map[source_key]
            elif old_lead in aliases:
                target = self._source_deal_target(aliases[old_lead], lead_map, deal_map)
                if target:
                    target_kind = target[0].upper()
                    target_crm_id = target[1]
            mapped = bool(target_kind and old_contact in contact_map)
            self.report.add_relation(
                relation_type="LEAD_CONTACT",
                source_from_type="LEAD",
                source_from_id=old_lead,
                source_to_type="CONTACT",
                source_to_id=old_contact,
                target_from_type=target_kind if mapped else "",
                target_from_id=target_crm_id if mapped else "",
                target_to_type="CONTACT" if mapped else "",
                target_to_id=contact_map.get(old_contact, ""),
                status=status if mapped else "SKIP",
                details={"is_primary": row.get("IS_PRIMARY"), "sort": row.get("SORT")},
            )

        for row in self._source["Deal_Contacts"]:
            old_deal = text(row.get("DEAL_ID"))
            old_contact = text(row.get("CONTACT_ID"))
            target = self._source_deal_target(old_deal, lead_map, deal_map)
            mapped = bool(target and old_contact in contact_map)
            self.report.add_relation(
                relation_type="DEAL_CONTACT",
                source_from_type="DEAL",
                source_from_id=old_deal,
                source_to_type="CONTACT",
                source_to_id=old_contact,
                target_from_type=target[0].upper() if mapped and target else "",
                target_from_id=target[1] if mapped and target else "",
                target_to_type="CONTACT" if mapped else "",
                target_to_id=contact_map.get(old_contact, ""),
                status=status if mapped else "SKIP",
                details={"is_primary": row.get("IS_PRIMARY"), "sort": row.get("SORT")},
            )

        req_map = self.report.maps["requisites"]
        for row in self._source["Requisite_Links"]:
            if text(row.get("ENTITY_TYPE_ID")) != "2":
                continue
            old_deal = text(row.get("ENTITY_ID"))
            old_req = text(row.get("REQUISITE_ID"))
            target = self._source_deal_target(old_deal, lead_map, deal_map)
            mapped = bool(target and old_req in req_map)
            target_status = status if mapped else "SKIP"
            details = {
                "bank_detail_id": row.get("BANK_DETAIL_ID"),
                "note": "For a deal routed to a lead, the requisite remains attached through company/contact",
            }
            self.report.add_relation(
                relation_type="CRM_REQUISITE",
                source_from_type="DEAL",
                source_from_id=old_deal,
                source_to_type="REQUISITE",
                source_to_id=old_req,
                target_from_type=target[0].upper() if target else "",
                target_from_id=target[1] if target else "",
                target_to_type="REQUISITE" if old_req in req_map else "",
                target_to_id=req_map.get(old_req, ""),
                status=target_status,
                details=details,
            )

    def _dry_run_requisites_and_addresses(
        self,
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        *,
        max_items: int = 0,
    ) -> None:
        """Prepare exact requisite/address payloads without writing to the box."""
        self.load_source("Requisites", "Addresses", "Requisite_Presets")
        target_presets = self.client.list_all(
            "crm.requisite.preset.list",
            {"select": ["ID", "NAME", "XML_ID", "ENTITY_TYPE_ID", "COUNTRY_ID"]},
        )
        source_presets = {text(row.get("ID")): row for row in self._source["Requisite_Presets"]}
        rows = self._source["Requisites"]
        if max_items and not self._sample_scope:
            rows = rows[:max_items]
        req_map: dict[str, int] = {}
        for index, row in enumerate(rows):
            old_id = text(row.get("ID"))
            source_entity_type = text(row.get("ENTITY_TYPE_ID"))
            old_entity = text(row.get("ENTITY_ID"))
            target_entity = (
                company_map.get(old_entity)
                if source_entity_type == "4"
                else contact_map.get(old_entity)
                if source_entity_type == "3"
                else None
            )
            if not target_entity:
                self.report.add("create_requisite", "REQUISITE", old_id, "REQUISITE", "", "SKIP", "owner company/contact was not mapped")
                continue
            source_preset = source_presets.get(text(row.get("PRESET_ID")), {})
            target_preset, preset_match = resolve_requisite_preset(
                source_preset,
                source_entity_type,
                target_presets,
            )
            if not target_preset:
                self.report.add(
                    "create_requisite",
                    "REQUISITE",
                    old_id,
                    "REQUISITE",
                    "",
                    "SKIP",
                    f"target preset not resolved: {source_preset.get('NAME')}; {preset_match}",
                )
                continue
            fields = self._copy_standard_fields(
                "requisite",
                row,
                excluded={"ENTITY_ID", "ENTITY_TYPE_ID", "PRESET_ID", "XML_ID"},
            )
            fields.update({
                "ENTITY_ID": target_entity,
                "ENTITY_TYPE_ID": int(source_entity_type),
                "PRESET_ID": target_preset,
                "XML_ID": text(row.get("XML_ID")) or f"B24MIG_REQ_{old_id}",
            })
            target_id = -(index + 1)
            req_map[old_id] = target_id
            self.report.maps["requisites"][old_id] = target_id
            self.report.add("create_requisite", "REQUISITE", old_id, "REQUISITE", target_id, "DRY_RUN", text(fields.get("NAME")))
            self.report.add_transfer(
                operation="create_requisite",
                source_type="REQUISITE",
                source_id=old_id,
                target_type="REQUISITE",
                target_id=target_id,
                status="DRY_RUN",
                payload=fields,
                route="REQUISITE",
            )
            owner_kind = "COMPANY" if source_entity_type == "4" else "CONTACT"
            self.report.add_relation(
                relation_type="REQUISITE_OWNER",
                source_from_type="REQUISITE",
                source_from_id=old_id,
                source_to_type=owner_kind,
                source_to_id=old_entity,
                target_from_type="REQUISITE",
                target_from_id=target_id,
                target_to_type=owner_kind,
                target_to_id=target_entity,
                status="DRY_RUN",
                details={"preset_id": target_preset},
            )

        unique_addresses = self._unique_source_addresses()
        address_rows = unique_addresses
        if max_items and not self._sample_scope:
            address_rows = address_rows[:max_items]
        for index, row in enumerate(address_rows):
            old_req = text(row.get("ENTITY_ID"))
            if text(row.get("ENTITY_TYPE_ID")) != "8" or old_req not in req_map:
                continue
            fields = self._copy_standard_fields(
                "address",
                row,
                excluded={"ENTITY_ID", "ENTITY_TYPE_ID", "ANCHOR_ID", "ANCHOR_TYPE_ID", "LOC_ADDR_ID"},
            )
            fields.update({
                "ENTITY_ID": req_map[old_req],
                "ENTITY_TYPE_ID": 8,
                "TYPE_ID": int(row.get("TYPE_ID") or 1),
            })
            source_address_id = text(row.get("ID")) or f"{old_req}:{row.get('TYPE_ID')}"
            target_address_id = -(index + 1)
            self.report.maps["addresses"][source_address_id] = target_address_id
            self.report.add("create_address", "ADDRESS", source_address_id, "ADDRESS", target_address_id, "DRY_RUN", text(fields.get("ADDRESS_1")))
            self.report.add_transfer(
                operation="create_address",
                source_type="ADDRESS",
                source_id=source_address_id,
                target_type="ADDRESS",
                target_id=target_address_id,
                status="DRY_RUN",
                payload=fields,
                route="REQUISITE_ADDRESS",
            )
            self.report.add_relation(
                relation_type="ADDRESS_REQUISITE",
                source_from_type="ADDRESS",
                source_from_id=source_address_id,
                source_to_type="REQUISITE",
                source_to_id=old_req,
                target_from_type="ADDRESS",
                target_from_id=target_address_id,
                target_to_type="REQUISITE",
                target_to_id=req_map[old_req],
                status="DRY_RUN",
                details={"type_id": row.get("TYPE_ID")},
            )

    def _task_registry_fields(
        self,
        row: Mapping[str, Any],
        user_map: Mapping[str, int],
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
        task_map: Mapping[str, int],
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        old_id = text(row.get("id"))
        blocking: list[str] = []
        warnings: list[str] = []
        created_by = self._context_user_target(row.get("createdBy"), "task", user_map)
        responsible = self._context_user_target(row.get("responsibleId"), "task", user_map)
        if not created_by:
            blocking.append(f"unmapped creator {text(row.get('createdBy'))}")
        if not responsible:
            blocking.append(f"unmapped responsible {text(row.get('responsibleId'))}")

        accomplices: list[int] = []
        for source_user in row.get("accomplices") or []:
            target_user = self._context_user_target(source_user, "task", user_map)
            if target_user:
                accomplices.append(target_user)
            else:
                warnings.append(f"unmapped accomplice {text(source_user)} omitted")

        auditors: list[int] = []
        for source_user in row.get("auditors") or []:
            target_user = self._context_user_target(source_user, "task", user_map)
            if target_user:
                auditors.append(target_user)
            else:
                warnings.append(f"unmapped auditor {text(source_user)} omitted")

        parent_old = text(row.get("parentId"))
        parent_target = task_map.get(parent_old) if parent_old not in {"", "0"} else None
        if parent_old not in {"", "0"} and not parent_target:
            warnings.append(f"parent task {parent_old} not mapped; imported as top-level task")

        crm_refs: list[str] = []
        for reference in row.get("ufCrmTask") or []:
            mapped = self._map_crm_ref(text(reference), company_map, contact_map, lead_map, deal_map)
            if mapped:
                crm_refs.append(mapped)
            else:
                warnings.append(f"unresolved CRM link {text(reference)} omitted")

        description = text(row.get("description"))
        if row.get("closedDate"):
            description = append_text(description, f"Исходная дата завершения: {text(row.get('closedDate'))}")
        description = append_text(description, migration_marker("TASK", old_id, "TASK"))
        fields: dict[str, Any] = {
            "TITLE": text(row.get("title")) or f"Задача {old_id}",
            "DESCRIPTION": description,
            "DESCRIPTION_IN_BBCODE": text(row.get("descriptionInBbcode")) or "Y",
            "CREATED_BY": created_by,
            "RESPONSIBLE_ID": responsible,
            "ACCOMPLICES": accomplices,
            "AUDITORS": auditors,
            "PRIORITY": text(row.get("priority")) or "1",
            "ALLOW_CHANGE_DEADLINE": text(row.get("allowChangeDeadline")) or "N",
            "ALLOW_TIME_TRACKING": text(row.get("allowTimeTracking")) or "N",
            "TASK_CONTROL": text(row.get("taskControl")) or "N",
            "ADD_IN_REPORT": text(row.get("addInReport")) or "N",
            "MATCH_WORK_TIME": text(row.get("matchWorkTime")) or "N",
            "TIME_ESTIMATE": int(row.get("timeEstimate") or 0),
            "GROUP_ID": 0,
            "XML_ID": f"B24MIG_TASK_{old_id}",
        }
        for source_key, target_key in {
            "deadline": "DEADLINE",
            "dateStart": "DATE_START",
            "startDatePlan": "START_DATE_PLAN",
            "endDatePlan": "END_DATE_PLAN",
            "mark": "MARK",
        }.items():
            if row.get(source_key) not in (None, ""):
                fields[target_key] = row.get(source_key)
        if parent_target:
            fields["PARENT_ID"] = parent_target
        if crm_refs:
            fields["UF_CRM_TASK"] = crm_refs
        if row.get("ufTaskWebdavFiles"):
            fields["SOURCE_FILE_REFERENCES"] = row.get("ufTaskWebdavFiles")
        return fields, blocking, warnings

    def _activity_registry_fields(
        self,
        activity: Mapping[str, Any],
        user_map: Mapping[str, int],
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
    ) -> tuple[dict[str, Any], list[tuple[int, int]], list[str], list[str]]:
        old_id = text(activity.get("ID"))
        problems: list[str] = []
        warnings: list[str] = []
        bindings, unresolved_bindings = self._activity_bindings(
            activity, company_map, contact_map, lead_map, deal_map
        )
        if unresolved_bindings:
            warnings.append(f"unresolved CRM bindings omitted: {unresolved_bindings}")
        if not bindings:
            problems.append("no mapped CRM owner/binding")
        responsible = self._context_user_target(activity.get("RESPONSIBLE_ID"), "crm", user_map)
        if not responsible:
            problems.append(f"unmapped responsible {text(activity.get('RESPONSIBLE_ID'))}")
        communications, unresolved_communications, client_warnings = self._activity_client_communications(
            activity, company_map, contact_map, lead_map, deal_map
        )
        communication_note = self._unresolved_communications_note(
            activity.get("COMMUNICATIONS") or [], unresolved_communications
        )
        if unresolved_communications:
            warnings.append(f"unresolved communications omitted: {unresolved_communications}")
        warnings.extend(client_warnings)
        settings = activity.get("SETTINGS") if isinstance(activity.get("SETTINGS"), dict) else {}
        primary_type, primary_id = bindings[0] if bindings else (0, 0)
        fields: dict[str, Any] = {
            "OWNER_TYPE_ID": primary_type,
            "OWNER_ID": primary_id,
            "TYPE_ID": int(activity.get("TYPE_ID") or 0),
            "SUBJECT": text(activity.get("SUBJECT")) or f"Дело {old_id}",
            "RESPONSIBLE_ID": responsible,
            "COMPLETED": text(activity.get("COMPLETED")) or "N",
            "STATUS": int(activity.get("STATUS") or 1),
            "PRIORITY": int(activity.get("PRIORITY") or 1),
            "DESCRIPTION": append_text(text(activity.get("DESCRIPTION")), communication_note),
            "DESCRIPTION_TYPE": int(activity.get("DESCRIPTION_TYPE") or 1),
            "DIRECTION": int(activity.get("DIRECTION") or 0),
            "LOCATION": text(activity.get("LOCATION")),
            "NOTIFY_TYPE": int(activity.get("NOTIFY_TYPE") or 0),
            "NOTIFY_VALUE": int(activity.get("NOTIFY_VALUE") or 0),
            "START_TIME": activity.get("START_TIME"),
            "END_TIME": activity.get("END_TIME"),
            "DEADLINE": activity.get("DEADLINE"),
            "COMMUNICATIONS": communications,
            "PROVIDER_ID": text(activity.get("PROVIDER_ID")),
            "PROVIDER_TYPE_ID": text(activity.get("PROVIDER_TYPE_ID")),
            "PROVIDER_GROUP_ID": text(activity.get("PROVIDER_GROUP_ID")),
            "PROVIDER_PARAMS": activity.get("PROVIDER_PARAMS") or {},
            "PROVIDER_DATA": text(activity.get("PROVIDER_DATA")),
            "IS_INCOMING_CHANNEL": text(activity.get("IS_INCOMING_CHANNEL")) or "N",
            "ORIGINATOR_ID": "B24_CLOUD_MIGRATION",
            "ORIGIN_ID": f"ACTIVITY_{old_id}",
            "SETTINGS": {**settings, "DISABLE_SENDING_MESSAGE_COPY": "Y"},
        }
        if activity.get("FILES"):
            fields["SOURCE_FILE_REFERENCES"] = activity.get("FILES")
        fields = {
            key: value
            for key, value in fields.items()
            if value not in (None, "", [], {}) or key in {"COMMUNICATIONS", "SETTINGS"}
        }
        return fields, bindings, problems, warnings

    def _dry_run_tasks_activities(
        self,
        user_map: Mapping[str, int],
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
        *,
        max_items: int = 0,
    ) -> None:
        """Validate and register every task/activity payload without writing."""
        self.load_source("Tasks", "CRM_Activities")
        tasks = self._task_rows_topological(self._source["Tasks"])
        activities = self._source["CRM_Activities"]
        tasks = self._select_sample_task_rows(
            tasks, max_items, company_map, contact_map, lead_map, deal_map
        )
        activities = self._select_sample_activity_rows(
            activities, max_items, company_map, contact_map, lead_map, deal_map
        )

        task_map: dict[str, int] = {}
        for index, row in enumerate(tasks):
            old_id = text(row.get("id"))
            skip_reason = self._task_skip_reason(row)
            if skip_reason:
                self.report.add("create_task", "TASK", old_id, "TASK", "", "SKIP", skip_reason)
                continue

            fields, blocking_problems, registry_warnings = self._task_registry_fields(
                row, user_map, company_map, contact_map, lead_map, deal_map, task_map
            )
            warnings_for_row: list[str] = list(registry_warnings)
            for source_file in row.get("ufTaskWebdavFiles") or []:
                raw = text(source_file).removeprefix("n")
                try:
                    if not raw.isdigit():
                        raise ValueError(f"invalid file reference {source_file!r}")
                    self.source_client.call("disk.attachedObject.get", {"id": int(raw)})
                except Exception as exc:
                    warnings_for_row.append(f"task file {source_file}: {exc}")
                self.report.add_relation(
                    relation_type="TASK_FILE",
                    source_from_type="TASK",
                    source_from_id=old_id,
                    source_to_type="FILE",
                    source_to_id=source_file,
                    target_from_type="TASK",
                    target_from_id="",
                    target_to_type="FILE",
                    target_to_id="",
                    status="WARN" if warnings_for_row else "DRY_RUN",
                    details="file will be downloaded from the cloud and attached to the created task",
                )

            regular_comments = int(row.get("commentsCount") or 0) - int(row.get("serviceCommentsCount") or 0)
            if regular_comments > 0:
                comments = self._fetch_task_comments(self.source_client, int(old_id))
                if not comments:
                    warnings_for_row.append(f"{regular_comments} comments reported but none readable")
                else:
                    for comment in comments:
                        comment_id = text(comment.get("ID") or comment.get("id"))
                        self.report.add_relation(
                            relation_type="TASK_COMMENT",
                            source_from_type="TASK",
                            source_from_id=old_id,
                            source_to_type="COMMENT",
                            source_to_id=comment_id,
                            target_from_type="TASK",
                            target_from_id="",
                            target_to_type="COMMENT",
                            target_to_id="",
                            status="DRY_RUN",
                            details={
                                "author_id": comment.get("AUTHOR_ID") or comment.get("authorId"),
                                "post_date": comment.get("POST_DATE") or comment.get("postDate"),
                            },
                        )

            if blocking_problems:
                status = "SKIP"
                target_id: Any = ""
                message = "Пропущено: " + "; ".join(
                    blocking_problems
                    + (["WARN: " + "; ".join(warnings_for_row)] if warnings_for_row else [])
                )
            else:
                target_id = -(index + 1)
                task_map[old_id] = target_id
                self.report.maps["tasks"][old_id] = target_id
                status = "WARN" if warnings_for_row else "DRY_RUN"
                message = "; ".join(warnings_for_row) if warnings_for_row else text(row.get("title"))

            self.report.add("create_task", "TASK", old_id, "TASK", target_id, status, message)
            self.report.add_transfer(
                operation="create_task",
                source_type="TASK",
                source_id=old_id,
                target_type="TASK",
                target_id=target_id,
                status=status,
                payload=fields,
                route="TASK_WITHOUT_PROJECT",
            )

            parent_old = text(row.get("parentId"))
            if parent_old not in {"", "0"}:
                self.report.add_relation(
                    relation_type="TASK_PARENT",
                    source_from_type="TASK",
                    source_from_id=old_id,
                    source_to_type="TASK",
                    source_to_id=parent_old,
                    target_from_type="TASK" if target_id else "",
                    target_from_id=target_id,
                    target_to_type="TASK" if parent_old in task_map else "",
                    target_to_id=task_map.get(parent_old, ""),
                    status=status if target_id and parent_old in task_map else "SKIP",
                    details="subtask hierarchy",
                )
            for reference in row.get("ufCrmTask") or []:
                mapped = self._map_crm_ref(text(reference), company_map, contact_map, lead_map, deal_map)
                prefix = text(reference).split("_", 1)[0].upper()
                target_prefix = text(mapped).split("_", 1)[0].upper() if mapped else ""
                self.report.add_relation(
                    relation_type="TASK_CRM",
                    source_from_type="TASK",
                    source_from_id=old_id,
                    source_to_type=prefix,
                    source_to_id=text(reference).split("_", 1)[-1],
                    target_from_type="TASK" if target_id else "",
                    target_from_id=target_id,
                    target_to_type=target_prefix,
                    target_to_id=text(mapped).split("_", 1)[-1] if mapped else "",
                    status=status if target_id and mapped else "SKIP",
                    details={"source_reference": reference, "target_reference": mapped},
                )

        for index, row in enumerate(activities):
            old_id = text(row.get("ID"))
            fields, bindings, blocking_problems, warnings_for_row = self._activity_registry_fields(
                row, user_map, company_map, contact_map, lead_map, deal_map
            )
            for file_index, item in enumerate(row.get("FILES") or []):
                source_file = (
                    item.get("FILE_ID") or item.get("fileId") or item.get("id")
                    if isinstance(item, dict)
                    else item
                )
                if not source_file:
                    continue
                try:
                    self.source_client.call("disk.file.get", {"id": int(source_file)})
                except Exception as exc:
                    warnings_for_row.append(f"activity file {file_index}:{source_file}: {exc}")
                self.report.add_relation(
                    relation_type="ACTIVITY_FILE",
                    source_from_type="ACTIVITY",
                    source_from_id=old_id,
                    source_to_type="FILE",
                    source_to_id=source_file,
                    target_from_type="ACTIVITY",
                    target_from_id="",
                    target_to_type="FILE",
                    target_to_id="",
                    status="WARN" if warnings_for_row else "DRY_RUN",
                    details="file will be downloaded and attached to the CRM activity",
                )

            if blocking_problems:
                status = "SKIP"
                target_id = ""
                message = "Пропущено: " + "; ".join(
                    blocking_problems
                    + (["WARN: " + "; ".join(warnings_for_row)] if warnings_for_row else [])
                )
            else:
                target_id = -(index + 1)
                self.report.maps["activities"][old_id] = target_id
                status = "WARN" if warnings_for_row else "DRY_RUN"
                message = "; ".join(warnings_for_row) if warnings_for_row else text(row.get("SUBJECT"))

            self.report.add("create_activity", "ACTIVITY", old_id, "ACTIVITY", target_id, status, message)
            self.report.add_transfer(
                operation="create_activity",
                source_type="ACTIVITY",
                source_id=old_id,
                target_type="ACTIVITY",
                target_id=target_id,
                status=status,
                payload=fields,
                route="CRM_ACTIVITY",
            )
            for owner_type, owner_id in bindings:
                self.report.add_relation(
                    relation_type="ACTIVITY_CRM_BINDING",
                    source_from_type="ACTIVITY",
                    source_from_id=old_id,
                    source_to_type=CRM_OWNER_TYPES.get(int(row.get("OWNER_TYPE_ID") or 0), "CRM").upper(),
                    source_to_id=text(row.get("OWNER_ID")),
                    target_from_type="ACTIVITY" if target_id else "",
                    target_from_id=target_id,
                    target_to_type=CRM_OWNER_TYPES.get(owner_type, "CRM").upper(),
                    target_to_id=owner_id,
                    status=status if target_id else "SKIP",
                    details={"owner_type_id": owner_type},
                )

    # ---------- relations and requisites ----------

    def import_contact_company_relations(self, contact_map: Mapping[str, int], company_map: Mapping[str, int]) -> None:
        self.load_source("Contact_Companies")
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._source["Contact_Companies"]:
            old_contact = text(row.get("CONTACT_ID")); old_company = text(row.get("COMPANY_ID"))
            if old_contact in contact_map and old_company in company_map:
                grouped[old_contact].append({"COMPANY_ID": company_map[old_company], "SORT": int(row.get("SORT") or 10), "IS_PRIMARY": text(row.get("IS_PRIMARY")) or "N"})
        commands = []
        for index, (old_contact, items) in enumerate(grouped.items()):
            commands.append((f"cc{index}", "crm.contact.company.items.set", {"id": contact_map[old_contact], "items": items}))
        for success, errors in self.client.batch_chunks(commands, size=30):
            for key in success:
                self.report.add("set_contact_companies", "CONTACT", key, "CONTACT", "", "OK", "")
            for key, err in errors.items():
                self.report.add("set_contact_companies", "CONTACT", key, "CONTACT", "", "ERROR", text(err))

    def import_crm_contact_relations(self, contact_map: Mapping[str, int], lead_map: Mapping[str, int], deal_map: Mapping[str, int]) -> None:
        self.load_source("Lead_Contacts", "Deal_Contacts")
        lead_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        deal_contact_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        aliases = self._converted_lead_to_deal()
        for row in self._source["Lead_Contacts"]:
            old_lead = text(row.get("LEAD_ID"))
            old_contact = text(row.get("CONTACT_ID"))
            if old_contact not in contact_map:
                continue
            item = {
                "CONTACT_ID": contact_map[old_contact],
                "SORT": int(row.get("SORT") or 10),
                "IS_PRIMARY": text(row.get("IS_PRIMARY")) or "N",
            }
            source_key = f"LEAD:{old_lead}:LEAD"
            if source_key in lead_map:
                lead_items[source_key].append(item)
                continue
            converted_deal = aliases.get(old_lead)
            if converted_deal:
                target = self._source_deal_target(converted_deal, lead_map, deal_map)
                if target and target[0] == "lead":
                    lead_items[f"DEAL:{converted_deal}:LEAD"].append(item)
                elif target and target[0] == "deal":
                    deal_contact_rows[converted_deal].append(item)

        for row in self._source["Deal_Contacts"]:
            old_deal = text(row.get("DEAL_ID")); old_contact = text(row.get("CONTACT_ID"))
            if old_contact not in contact_map:
                continue
            item = {"CONTACT_ID": contact_map[old_contact], "SORT": int(row.get("SORT") or 10), "IS_PRIMARY": text(row.get("IS_PRIMARY")) or "N"}
            deal_contact_rows[old_deal].append(item)
            routed_key = f"DEAL:{old_deal}:LEAD"
            if routed_key in lead_map:
                lead_items[routed_key].append(item)

        commands = []; context = {}; idx = 0
        for source_key, items in lead_items.items():
            seen = set(); unique = []
            for item in items:
                if item["CONTACT_ID"] not in seen:
                    seen.add(item["CONTACT_ID"]); unique.append(item)
            key = f"l{idx}"; idx += 1
            commands.append((key, "crm.lead.contact.items.set", {"id": lead_map[source_key], "items": unique})); context[key] = (source_key, "LEAD", lead_map[source_key])
        for old_deal, items in deal_contact_rows.items():
            if old_deal not in deal_map:
                continue
            seen = set(); unique = []
            for item in items:
                if item["CONTACT_ID"] not in seen:
                    seen.add(item["CONTACT_ID"]); unique.append(item)
            key = f"d{idx}"; idx += 1
            commands.append((key, "crm.deal.contact.items.set", {"id": deal_map[old_deal], "items": unique})); context[key] = (old_deal, "DEAL", deal_map[old_deal])
        for success, errors in self.client.batch_chunks(commands, size=35):
            for key in success:
                source_id, kind, target_id = context[key]
                self.report.add("set_crm_contacts", kind, source_id, kind, target_id, "OK", "")
            for key, err in errors.items():
                source_id, kind, target_id = context[key]
                self.report.add("set_crm_contacts", kind, source_id, kind, target_id, "ERROR", text(err))

    @staticmethod
    def _address_source_key(row: Mapping[str, Any]) -> tuple[str, ...]:
        """Semantic address identity used to remove exact API/page duplicates."""
        return tuple(
            text(row.get(code))
            for code in (
                "ENTITY_TYPE_ID", "ENTITY_ID", "TYPE_ID", "ADDRESS_1", "ADDRESS_2",
                "CITY", "POSTAL_CODE", "REGION", "PROVINCE", "COUNTRY", "COUNTRY_CODE",
            )
        )

    def _unique_source_addresses(self) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for row in self._source.get("Addresses", []):
            key = self._address_source_key(row)
            if key in seen:
                continue
            seen.add(key)
            unique.append(dict(row))
        duplicate_count = len(self._source.get("Addresses", [])) - len(unique)
        if duplicate_count:
            self.report.extra["source_address_duplicates_removed"] = duplicate_count
        return unique

    def import_requisites(self, company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> None:
        self.load_source("Requisites", "Addresses", "Requisite_Presets")
        target_presets = self.client.list_all(
            "crm.requisite.preset.list",
            {"select": ["ID", "NAME", "XML_ID", "ENTITY_TYPE_ID", "COUNTRY_ID"]},
        )
        source_presets = {text(row.get("ID")): row for row in self._source["Requisite_Presets"]}
        target_existing = self.client.list_all(
            "crm.requisite.list",
            {"select": ["ID", "XML_ID", "ENTITY_TYPE_ID", "ENTITY_ID", "PRESET_ID"]},
        )
        existing_by_key = {
            (text(row.get("XML_ID")), text(row.get("ENTITY_TYPE_ID")), text(row.get("ENTITY_ID"))): int(row["ID"])
            for row in target_existing
            if row.get("XML_ID") and text(row.get("ID")).isdigit()
        }
        existing_rows_by_xml: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in target_existing:
            xml_id = text(row.get("XML_ID"))
            if xml_id and text(row.get("ID")).isdigit():
                existing_rows_by_xml[xml_id].append(dict(row))

        req_map: dict[str, int] = dict(self.report.maps["requisites"])
        owner_conflicts_repaired = 0
        commands: list[tuple[str, str, Mapping[str, Any]]] = []
        context: dict[str, tuple[str, str, dict[str, Any], int | None]] = {}
        for index, row in enumerate(self._source["Requisites"]):
            old_id = text(row.get("ID"))
            source_entity_type = text(row.get("ENTITY_TYPE_ID"))
            old_entity = text(row.get("ENTITY_ID"))
            target_entity = (
                company_map.get(old_entity) if source_entity_type == "4"
                else contact_map.get(old_entity) if source_entity_type == "3"
                else None
            )
            if not target_entity:
                self.report.add(
                    "prepare_requisite", "REQUISITE", old_id, "REQUISITE", "", "SKIP",
                    f"owner {source_entity_type}:{old_entity} was not mapped",
                )
                continue

            base_xml_id = text(row.get("XML_ID")) or f"B24MIG_REQ_{old_id}"
            scoped_xml_id = owner_scoped_requisite_xml_id(
                old_id, source_entity_type, target_entity
            )
            source_preset = source_presets.get(text(row.get("PRESET_ID")), {})
            target_preset, preset_match = resolve_requisite_preset(
                source_preset, source_entity_type, target_presets
            )
            if not target_preset:
                self.report.add(
                    "prepare_requisite", "REQUISITE", old_id, "REQUISITE", "", "SKIP",
                    f"target preset not resolved: {source_preset.get('NAME')}; {preset_match}",
                )
                continue

            exact_base_id = existing_by_key.get(
                (base_xml_id, source_entity_type, text(target_entity))
            )
            exact_scoped_id = existing_by_key.get(
                (scoped_xml_id, source_entity_type, text(target_entity))
            )
            conflicting_base_rows = [
                item
                for item in existing_rows_by_xml.get(base_xml_id, [])
                if not (
                    text(item.get("ENTITY_TYPE_ID")) == source_entity_type
                    and text(item.get("ENTITY_ID")) == text(target_entity)
                )
            ]

            if exact_base_id:
                xml_id = base_xml_id
                existing_id = exact_base_id
            elif exact_scoped_id:
                xml_id = scoped_xml_id
                existing_id = exact_scoped_id
            elif conflicting_base_rows:
                # A requisite cannot safely be moved from one company/contact to
                # another with crm.requisite.update. Create an owner-scoped copy
                # instead; otherwise crm.requisite.link.register rejects the deal.
                xml_id = scoped_xml_id
                existing_id = None
                owner_conflicts_repaired += 1
            else:
                xml_id = base_xml_id
                existing_id = None

            fields = self._copy_standard_fields(
                "requisite", row,
                excluded={"ID", "ENTITY_ID", "ENTITY_TYPE_ID", "PRESET_ID", "XML_ID"},
            )
            fields.update({
                "ENTITY_ID": int(target_entity),
                "ENTITY_TYPE_ID": int(source_entity_type),
                "PRESET_ID": int(target_preset),
                "XML_ID": xml_id,
            })
            if existing_id:
                key = f"ru{index}"
                commands.append((key, "crm.requisite.update", {"id": existing_id, "fields": fields}))
                context[key] = (old_id, "update_requisite", fields, existing_id)
            else:
                key = f"ra{index}"
                commands.append((key, "crm.requisite.add", {"fields": fields}))
                context[key] = (old_id, "create_requisite", fields, None)

        for success, errors in self.client.batch_chunks(commands, size=30):
            for key, raw_result in success.items():
                old_id, operation, fields, existing_id = context[key]
                target_id = existing_id or extract_id(raw_result)
                if not target_id:
                    self.report.add(
                        operation, "REQUISITE", old_id, "REQUISITE", "", "ERROR",
                        f"Bitrix returned no target ID: {raw_result}",
                    )
                    continue
                req_map[old_id] = int(target_id)
                self.report.add(operation, "REQUISITE", old_id, "REQUISITE", target_id, "OK", "")
                self.report.add_transfer(
                    operation=operation, source_type="REQUISITE", source_id=old_id,
                    target_type="REQUISITE", target_id=target_id, status="OK",
                    payload=fields, route="REQUISITE",
                )
            for key, error in errors.items():
                old_id, operation, fields, existing_id = context[key]
                self.report.add(operation, "REQUISITE", old_id, "REQUISITE", existing_id or "", "SKIP", text(error))
                self.report.add_transfer(
                    operation=operation, source_type="REQUISITE", source_id=old_id,
                    target_type="REQUISITE", target_id=existing_id or "", status="SKIP",
                    payload=fields, route="REQUISITE",
                )
        self.report.maps["requisites"].update(req_map)
        if owner_conflicts_repaired:
            self.report.extra["requisite_owner_conflicts_repaired"] = owner_conflicts_repaired

        existing_addresses = self.client.list_all(
            "crm.address.list",
            {"order": {"ENTITY_ID": "ASC", "TYPE_ID": "ASC"}, "filter": {"ENTITY_TYPE_ID": 8}},
        )
        existing_address_keys = {
            (text(row.get("ENTITY_ID")), text(row.get("TYPE_ID")))
            for row in existing_addresses
        }
        address_commands: list[tuple[str, str, Mapping[str, Any]]] = []
        address_context: dict[str, tuple[str, str, str, dict[str, Any], int]] = {}
        for index, row in enumerate(self._unique_source_addresses()):
            old_req = text(row.get("ENTITY_ID"))
            if text(row.get("ENTITY_TYPE_ID")) != "8" or old_req not in req_map:
                continue
            target_req = int(req_map[old_req])
            address_type = int(row.get("TYPE_ID") or 1)
            fields = self._copy_standard_fields(
                "address", row,
                excluded={"ID", "ENTITY_ID", "ENTITY_TYPE_ID", "ANCHOR_ID", "ANCHOR_TYPE_ID", "LOC_ADDR_ID"},
            )
            fields.update({"ENTITY_ID": target_req, "ENTITY_TYPE_ID": 8, "TYPE_ID": address_type})
            source_address_id = text(row.get("ID")) or f"{old_req}:{address_type}"
            composite_target_id = target_req * 100 + address_type
            existing_key = (text(target_req), text(address_type))
            if existing_key in existing_address_keys:
                method = "crm.address.update"
                operation = "update_address"
            else:
                method = "crm.address.add"
                operation = "create_address"
            key = f"addr{index}"
            address_commands.append((key, method, {"fields": fields}))
            address_context[key] = (source_address_id, old_req, operation, fields, composite_target_id)

        for success, errors in self.client.batch_chunks(address_commands, size=30):
            for key, _raw_result in success.items():
                source_address_id, old_req, operation, fields, target_id = address_context[key]
                self.report.maps["addresses"][source_address_id] = target_id
                self.report.add(operation, "ADDRESS", source_address_id, "ADDRESS", target_id, "OK", f"requisite {old_req}")
                self.report.add_transfer(
                    operation=operation, source_type="ADDRESS", source_id=source_address_id,
                    target_type="ADDRESS", target_id=target_id, status="OK",
                    payload=fields, route="REQUISITE_ADDRESS",
                )
            for key, error in errors.items():
                source_address_id, old_req, operation, fields, target_id = address_context[key]
                self.report.add(operation, "ADDRESS", source_address_id, "ADDRESS", target_id, "SKIP", f"requisite {old_req}: {text(error)}")
                self.report.add_transfer(
                    operation=operation, source_type="ADDRESS", source_id=source_address_id,
                    target_type="ADDRESS", target_id=target_id, status="SKIP",
                    payload=fields, route="REQUISITE_ADDRESS",
                )

    def import_requisite_links(self, deal_map: Mapping[str, int], lead_map: Mapping[str, int]) -> None:
        self.load_source("Requisite_Links")
        req_map = self.report.maps["requisites"]
        commands = []; context = {}
        for index, row in enumerate(self._source["Requisite_Links"]):
            if text(row.get("ENTITY_TYPE_ID")) != "2":
                continue
            old_deal = text(row.get("ENTITY_ID")); old_req = text(row.get("REQUISITE_ID"))
            if old_deal in deal_map and old_req in req_map:
                fields = {"ENTITY_TYPE_ID": 2, "ENTITY_ID": deal_map[old_deal], "REQUISITE_ID": req_map[old_req], "BANK_DETAIL_ID": 0, "MC_REQUISITE_ID": 0, "MC_BANK_DETAIL_ID": 0}
                key = f"rl{index}"; commands.append((key, "crm.requisite.link.register", {"fields": fields})); context[key] = ("DEAL", old_deal, deal_map[old_deal])
            elif f"DEAL:{old_deal}:LEAD" in lead_map:
                # crm.requisite.link.register does not support lead as a payer entity in the same way.
                # The requisite remains correctly attached to the company/contact and is reported here.
                self.report.add("link_requisite", "DEAL", old_deal, "LEAD", lead_map[f"DEAL:{old_deal}:LEAD"], "SKIP", "routed lead uses company/contact requisite")
        for success, errors in self.client.batch_chunks(commands, size=30):
            for key in success:
                kind, source, target = context[key]
                self.report.add("link_requisite", kind, source, kind, target, "OK", "")
            for key, err in errors.items():
                kind, source, target = context[key]
                self.report.add("link_requisite", kind, source, kind, target, "ERROR", text(err))

    # ---------- task migration ----------

    def _converted_lead_to_deal(self) -> dict[str, str]:
        """Map excluded converted test leads to the deals produced by conversion.

        The four cloud leads are intentionally not recreated. Their converted
        deals remain part of the migration, so task/activity relations that still
        point to an old lead must follow the uniquely matching converted deal.
        """
        if self._converted_lead_aliases is not None:
            return self._converted_lead_aliases
        self.load_source("Leads", "Deals")
        by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for deal in self._source["Deals"]:
            title_key = normalize_text(deal.get("TITLE"))
            if title_key:
                by_title[title_key].append(deal)
        aliases: dict[str, str] = {}
        ambiguous: dict[str, list[str]] = {}
        for lead in self._source["Leads"]:
            old_lead = text(lead.get("ID"))
            candidates = by_title.get(normalize_text(lead.get("TITLE")), [])
            if len(candidates) == 1:
                aliases[old_lead] = text(candidates[0].get("ID"))
            elif len(candidates) > 1:
                ambiguous[old_lead] = [text(item.get("ID")) for item in candidates]
        self._converted_lead_aliases = aliases
        self.report.extra["converted_lead_aliases"] = {"mapped": aliases, "ambiguous": ambiguous}
        return aliases

    @staticmethod
    def _source_deal_target(old_deal: str, lead_map: Mapping[str, int], deal_map: Mapping[str, int]) -> tuple[str, int] | None:
        routed = f"DEAL:{old_deal}:LEAD"
        if routed in lead_map:
            return "lead", int(lead_map[routed])
        if old_deal in deal_map:
            return "deal", int(deal_map[old_deal])
        return None

    def _map_crm_ref(self, reference: str, company_map: Mapping[str, int], contact_map: Mapping[str, int], lead_map: Mapping[str, int], deal_map: Mapping[str, int]) -> str | None:
        match = re.fullmatch(r"(CO|L|D|C)_(\d+)", text(reference).strip(), flags=re.I)
        if not match:
            return None
        prefix, old_id = match.group(1).upper(), match.group(2)
        if prefix == "CO" and old_id in company_map:
            return f"CO_{company_map[old_id]}"
        if prefix == "C" and old_id in contact_map:
            return f"C_{contact_map[old_id]}"
        if prefix == "L":
            key = f"LEAD:{old_id}:LEAD"
            if key in lead_map:
                return f"L_{lead_map[key]}"
            converted_deal = self._converted_lead_to_deal().get(old_id)
            if converted_deal:
                target = self._source_deal_target(converted_deal, lead_map, deal_map)
                if target:
                    kind, target_id = target
                    return f"L_{target_id}" if kind == "lead" else f"D_{target_id}"
        if prefix == "D":
            target = self._source_deal_target(old_id, lead_map, deal_map)
            if target:
                kind, target_id = target
                return f"L_{target_id}" if kind == "lead" else f"D_{target_id}"
        return None

    def _existing_tasks(self, *, include_saved_maps: bool = True) -> dict[str, int]:
        rows = self.client.list_all("tasks.task.list", {"select": ["ID", "XML_ID", "DESCRIPTION"]})
        # Saved maps are an additional idempotency guard when the target webhook
        # cannot list a task assigned to another employee. Live API results
        # override them. Verification deliberately disables this fallback.
        result: dict[str, int] = (
            dict(self.report.maps["tasks"]) if include_saved_maps else {}
        )
        for row in rows:
            xml_id = text(row.get("xmlId") or row.get("XML_ID"))
            if xml_id.startswith("B24MIG_TASK_"):
                result[xml_id.removeprefix("B24MIG_TASK_")] = int(row.get("id") or row.get("ID"))
                continue
            marker = parse_marker(row.get("description") or row.get("DESCRIPTION"))
            if marker and marker[0] == "TASK":
                result[marker[1]] = int(row.get("id") or row.get("ID"))
        return result

    def _task_rows_topological(self, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {text(row.get("id")): row for row in rows}
        children: dict[str, list[str]] = defaultdict(list)
        indegree: dict[str, int] = {key: 0 for key in by_id}
        for key, row in by_id.items():
            parent = text(row.get("parentId"))
            if parent and parent != "0" and parent in by_id:
                children[parent].append(key)
                indegree[key] += 1
        queue = deque(sorted((key for key, degree in indegree.items() if degree == 0), key=lambda x: int(x)))
        order: list[dict[str, Any]] = []
        while queue:
            key = queue.popleft(); order.append(by_id[key])
            for child in children[key]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(rows):
            remaining = [by_id[key] for key, degree in indegree.items() if degree > 0]
            order.extend(sorted(remaining, key=lambda row: int(row.get("id") or 0)))
        return order

    def _transfer_task_files(self, row: Mapping[str, Any]) -> list[int]:
        if not self.file_transfer:
            return []
        result = []
        for source_id in row.get("ufTaskWebdavFiles") or []:
            target = self.file_transfer.transfer_attached_object(source_id)
            if target:
                result.append(target)
        return result

    def _target_task_disk_ids(self, target_task_id: int) -> set[int]:
        try:
            result = self.client.call(
                "tasks.task.get",
                {"taskId": target_task_id, "select": ["ID", "UF_TASK_WEBDAV_FILES"]},
            ) or {}
            task = result.get("task", result) if isinstance(result, dict) else {}
            attachment_ids = task.get("ufTaskWebdavFiles") or task.get("UF_TASK_WEBDAV_FILES") or []
        except Exception:
            return set()
        disk_ids: set[int] = set()
        for attachment_id in attachment_ids:
            raw = text(attachment_id).removeprefix("n")
            if not raw.isdigit():
                continue
            try:
                attached = self.client.call("disk.attachedObject.get", {"id": int(raw)}) or {}
                object_id = int(attached.get("OBJECT_ID") or attached.get("objectId") or 0)
                if object_id:
                    disk_ids.add(object_id)
            except Exception:
                continue
        return disk_ids

    def _attach_task_files(self, target_task_id: int, disk_ids: Iterable[int], old_task_id: str, context: str = "task") -> None:
        requested = sorted({int(value) for value in disk_ids if int(value or 0)})
        if not requested:
            return
        existing = self._target_task_disk_ids(target_task_id)
        missing = [value for value in requested if value not in existing]
        for value in sorted(existing.intersection(requested)):
            self.report.add("attach_task_file", "TASK_FILE", f"{old_task_id}:{value}", "TASK", target_task_id, "SKIP", f"{context}; already attached")
        if not missing:
            return
        failed_old: list[int] = []
        for file_id in missing:
            try:
                self.client.call("tasks.task.files.attach", {"taskId": target_task_id, "fileId": file_id})
                self.report.add("attach_task_file", "TASK_FILE", f"{old_task_id}:{file_id}", "TASK", target_task_id, "OK", context)
            except Exception as exc:
                failed_old.append(file_id)
                LOG.info("Classic task file attach failed for task %s file %s: %s", target_task_id, file_id, exc)
        if not failed_old:
            return
        try:
            self.client.call_v3("tasks.task.file.attach", {"taskId": target_task_id, "fileIds": failed_old})
            for file_id in failed_old:
                self.report.add("attach_task_file", "TASK_FILE", f"{old_task_id}:{file_id}", "TASK", target_task_id, "OK", f"{context}; REST 3.0")
        except Exception as exc:
            for file_id in failed_old:
                self.report.add("attach_task_file", "TASK_FILE", f"{old_task_id}:{file_id}", "TASK", target_task_id, "WARN", f"{context}; classic and REST 3.0 failed: {exc}")

    def _send_task_chat_message(self, target_task_id: int, message: str) -> int:
        result = self.client.call_v3(
            "tasks.task.chat.message.send",
            {"taskId": target_task_id, "text": message},
        ) or {}
        return extract_id(result)

    def _target_task_chat_id(self, target_task_id: int) -> int:
        result = self.client.call(
            "tasks.task.get", {"taskId": target_task_id, "select": ["CHAT_ID"]}
        ) or {}
        task = result.get("task", result) if isinstance(result, dict) else {}
        chat_id = int(task.get("chatId") or task.get("CHAT_ID") or 0)
        if not chat_id:
            raise RuntimeError(f"Target task {target_task_id} has no chatId")
        return chat_id

    def _commit_task_chat_files(
        self, target_task_id: int, disk_ids: Sequence[int], message: str
    ) -> int:
        """Create task-chat comment(s) with Drive files using the current API."""
        chat_id = self._target_task_chat_id(target_task_id)
        first_message_id = 0
        for index, file_id in enumerate(disk_ids):
            payload = {
                "CHAT_ID": chat_id,
                "FILE_ID": int(file_id),
                "MESSAGE": message if index == 0 else "Дополнительный файл к предыдущему комментарию миграции.",
            }
            result = self.client.call("im.disk.file.commit", payload) or {}
            message_id = extract_id(result.get("MESSAGE_ID") if isinstance(result, dict) else result)
            if not message_id and isinstance(result, dict):
                message_id = int(result.get("MESSAGE_ID") or result.get("messageId") or 0)
            if index == 0:
                first_message_id = message_id
        return first_message_id

    @staticmethod
    def _chat_datetime(value: Any) -> float | None:
        raw = text(value).strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    @classmethod
    def _chat_message_file_ids(cls, message: Mapping[str, Any]) -> set[str]:
        """Extract file references from undocumented/portal-specific message params.

        Bitrix returns chat files as a top-level ``files`` array. Depending on
        portal/module version, the message-to-file relation can be exposed in
        params under FILE_ID/FILE_IDS/DISK_ID/ATTACHMENTS (with different
        casing and nesting). We only inspect values whose key explicitly
        denotes a file/attachment to avoid treating user IDs as file IDs.
        """
        found: set[str] = set()

        def walk(value: Any, *, file_context: bool = False) -> None:
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    normalized = re.sub(r"[^a-z]", "", text(key).casefold())
                    nested_context = file_context or any(
                        token in normalized for token in ("file", "disk", "attach")
                    )
                    walk(nested, file_context=nested_context)
                return
            if isinstance(value, (list, tuple, set)):
                for nested in value:
                    walk(nested, file_context=file_context)
                return
            if file_context:
                raw = text(value).strip().removeprefix("n")
                if raw.isdigit():
                    found.add(raw)

        walk(message.get("params") or message.get("PARAMS") or {})
        for key in ("FILE_ID", "FILE_IDS", "DISK_ID", "ATTACHMENTS", "ATTACHED_OBJECTS", "files"):
            if key in message:
                walk(message[key], file_context=True)
        return found

    @classmethod
    def _merge_chat_page_files(cls, payload: Any) -> list[dict[str, Any]]:
        """Return chat messages with their matching top-level file objects.

        Exact IDs from message params are preferred. For portal versions that
        omit those IDs, files are matched conservatively by author and nearest
        timestamp. Ambiguous files are left unattached rather than assigned to
        the wrong comment.
        """
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, Mapping)]
        if not isinstance(payload, Mapping):
            return []
        raw_messages = payload.get("messages", payload.get("items", []))
        if not isinstance(raw_messages, list):
            return []
        messages = [dict(item) for item in raw_messages if isinstance(item, Mapping)]
        raw_files = payload.get("files") or []
        files = [dict(item) for item in raw_files if isinstance(item, Mapping)] if isinstance(raw_files, list) else []
        if not files or not messages:
            return messages

        files_by_id = {
            text(item.get("id") or item.get("ID") or item.get("fileId") or item.get("FILE_ID")): item
            for item in files
            if text(item.get("id") or item.get("ID") or item.get("fileId") or item.get("FILE_ID"))
        }
        used: set[str] = set()
        for message in messages:
            exact = []
            for file_id in cls._chat_message_file_ids(message):
                if file_id in files_by_id:
                    exact.append(files_by_id[file_id])
                    used.add(file_id)
            if exact:
                message["files"] = exact

        remaining = [(file_id, item) for file_id, item in files_by_id.items() if file_id not in used]
        if remaining and len(messages) == 1:
            messages[0].setdefault("files", []).extend(item for _, item in remaining)
            return messages

        for file_id, file_item in remaining:
            file_author = text(file_item.get("authorId") or file_item.get("AUTHOR_ID"))
            file_time = cls._chat_datetime(file_item.get("date") or file_item.get("DATE_CREATE"))
            scored: list[tuple[float, dict[str, Any]]] = []
            for message in messages:
                message_author = text(message.get("author_id") or message.get("authorId") or message.get("AUTHOR_ID"))
                if file_author and message_author and file_author != message_author:
                    continue
                message_time = cls._chat_datetime(message.get("date") or message.get("POST_DATE") or message.get("DATE_CREATE"))
                if file_time is None or message_time is None:
                    continue
                delta = abs(file_time - message_time)
                if delta <= 120:
                    scored.append((delta, message))
            scored.sort(key=lambda pair: pair[0])
            if not scored:
                continue
            if len(scored) > 1 and scored[0][0] == scored[1][0]:
                continue
            scored[0][1].setdefault("files", []).append(file_item)
            used.add(file_id)
        return messages

    @staticmethod
    def _task_chat_id(client: BitrixClient, task_id: int) -> int:
        """Return the task chat ID using classic REST and then REST 3.0.

        Some cloud portals with the new task card no longer return ``chatId``
        through the classic ``tasks.task.get`` call for old tasks. REST 3.0
        exposes the same chat as the related ``chat.id`` field, so it is used
        as a mandatory fallback before declaring comments unreadable.
        """
        try:
            task_result = client.call(
                "tasks.task.get",
                {"taskId": task_id, "select": ["ID", "CHAT_ID"]},
            ) or {}
            task = task_result.get("task", task_result) if isinstance(task_result, dict) else {}
            chat_id = int(task.get("chatId") or task.get("CHAT_ID") or 0)
            if chat_id:
                return chat_id
        except Exception as exc:
            LOG.info("Classic task chat lookup failed for task %s: %s", task_id, exc)

        try:
            task_result_v3 = client.call_v3(
                "tasks.task.get",
                {
                    "id": task_id,
                    "select": ["id", "chat.id", "chat.entityId", "chat.entityType"],
                },
            ) or {}
            item = (
                task_result_v3.get("item")
                if isinstance(task_result_v3, dict)
                else None
            )
            if not isinstance(item, dict) and isinstance(task_result_v3, dict):
                item = task_result_v3
            chat = item.get("chat") if isinstance(item, dict) else {}
            if isinstance(chat, dict):
                chat_id = int(chat.get("id") or chat.get("ID") or 0)
                if chat_id:
                    LOG.info("Task %s chat resolved through REST 3.0: %s", task_id, chat_id)
                    return chat_id
        except Exception as exc:
            LOG.info("REST 3.0 task chat lookup failed for task %s: %s", task_id, exc)
        return 0

    def _fetch_task_comments(self, client: BitrixClient, task_id: int) -> list[dict[str, Any]]:
        try:
            result = client.call("task.commentitem.getlist", {"TASKID": task_id, "ORDER": {"POST_DATE": "ASC"}, "FILTER": {}}) or []
            if isinstance(result, list) and result:
                return [dict(item) for item in result if isinstance(item, dict)]
        except Exception as exc:
            LOG.info("Old task comments API unavailable for task %s: %s", task_id, exc)
        try:
            chat_id = self._task_chat_id(client, task_id)
            if not chat_id:
                LOG.warning(
                    "Task %s reports comments but no chat ID was returned by classic REST or REST 3.0",
                    task_id,
                )
                return []
            messages: list[dict[str, Any]] = []
            last_id = 0
            for _ in range(1000):
                params: dict[str, Any] = {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50}
                if last_id:
                    params["LAST_ID"] = last_id
                payload = client.call("im.dialog.messages.get", params) or {}
                page = self._merge_chat_page_files(payload)
                if not page:
                    break
                # The chat API also returns service messages. They are task
                # history, not user comments, and must not be recreated as
                # discussion messages.
                messages.extend(
                    item for item in page
                    if text(item.get("author_id") or item.get("authorId") or item.get("AUTHOR_ID")) not in {"", "0"}
                )
                ids = [int(item.get("id") or item.get("ID") or 0) for item in page]
                new_last = min((value for value in ids if value), default=0)
                if len(page) < 50 or not new_last or new_last == last_id:
                    break
                last_id = new_last
            messages.sort(
                key=lambda item: (
                    self._chat_datetime(item.get("date") or item.get("POST_DATE")) or 0,
                    int(item.get("id") or item.get("ID") or 0),
                )
            )
            if not messages:
                LOG.warning(
                    "Task %s chat %s returned no user messages. The source webhook must include the im scope and its user must be a participant of the task chat.",
                    task_id,
                    chat_id,
                )
            return messages
        except Exception as exc:
            LOG.warning(
                "Cannot fetch chat comments for task %s: %s. Check the im scope and task-chat membership of the source webhook user.",
                task_id,
                exc,
            )
            return []

    @staticmethod
    def _comment_values(comment: Mapping[str, Any]) -> tuple[str, str, str, Any]:
        comment_id = text(comment.get("ID") or comment.get("id") or comment.get("messageId"))
        message = text(comment.get("POST_MESSAGE") or comment.get("message") or comment.get("text"))
        author = text(comment.get("AUTHOR_ID") or comment.get("authorId") or comment.get("author_id"))
        date = comment.get("POST_DATE") or comment.get("date") or comment.get("DATE_CREATE")
        return comment_id, message, author, date

    def _comment_attachments(self, comment: Mapping[str, Any], task_id: str, comment_id: str) -> list[int]:
        if not self.file_transfer:
            return []
        attached = comment.get("ATTACHED_OBJECTS") or comment.get("attachedObjects") or comment.get("files") or {}
        items = list(attached.values()) if isinstance(attached, dict) else attached if isinstance(attached, list) else []
        result = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            file_id = item.get("ATTACHMENT_ID") or item.get("attachmentId") or item.get("FILE_ID") or item.get("fileId") or item.get("id")
            download_url = item.get("urlDownload") or item.get("URL_DOWNLOAD") or item.get("DOWNLOAD_URL") or item.get("url")
            target = None
            # Chat file objects expose an absolute urlDownload and their ``id``
            # is not guaranteed to be a classic task attachment ID. Prefer the
            # signed URL in that case. Old comment API objects normally expose
            # a relative DOWNLOAD_URL, so resolve them through the attachment.
            if download_url and re.match(r"^https?://", text(download_url), flags=re.I):
                target = self.file_transfer.transfer_url(
                    f"task:{task_id}:comment:{comment_id}:{index}",
                    text(download_url),
                    text(item.get("NAME") or item.get("name")),
                )
            if not target and file_id:
                target = self.file_transfer.transfer_reference(file_id, prefer_attached=True)
            if not target and download_url:
                target = self.file_transfer.transfer_url(
                    f"task:{task_id}:comment:{comment_id}:{index}",
                    text(download_url),
                    text(item.get("NAME") or item.get("name")),
                )
            if target:
                result.append(target)
        return result

    def _import_task_comments(self, old_task_id: str, target_task_id: int, user_map: Mapping[str, int]) -> None:
        source_comments = self._fetch_task_comments(self.source_client, int(old_task_id))
        source_task = next(
            (row for row in self._source.get("Tasks", []) if text(row.get("id")) == text(old_task_id)),
            {},
        )
        expected_comments = max(
            0,
            int(source_task.get("commentsCount") or 0)
            - int(source_task.get("serviceCommentsCount") or 0),
        )
        if expected_comments and not source_comments:
            self.report.add(
                "copy_task_comments", "TASK", old_task_id, "TASK", target_task_id, "WARN",
                f"{expected_comments} comments reported by the dump but unavailable through the source API; task migrated without them",
            )
            return
        target_comments = self._fetch_task_comments(self.client, target_task_id)
        target_text = "\n".join(self._comment_values(item)[1] for item in target_comments)
        for index, comment in enumerate(source_comments):
            comment_id, message, source_author, post_date = self._comment_values(comment)
            attachments = self._comment_attachments(comment, old_task_id, comment_id or str(index))
            if not message and not attachments:
                continue
            if not message:
                message = "Файл из исходного комментария задачи."
            map_key = f"{old_task_id}:{comment_id or index}"
            marker = migration_marker("TASK_COMMENT", f"{old_task_id}_{comment_id or index}", "COMMENT")
            if marker in target_text or map_key in self.report.maps["task_comments"]:
                self.report.add("create_task_comment", "TASK_COMMENT", map_key, "TASK", target_task_id, "SKIP", "marker/map exists")
                continue
            source_user = next((row for row in self._source.get("Users", []) if text(row.get("ID")) == source_author), {})
            author_label = (
                text(source_user.get("FULL_NAME"))
                or " ".join(filter(None, [text(source_user.get("NAME")), text(source_user.get("LAST_NAME"))])).strip()
                or source_author
            )
            author_id = self._context_user_target(source_author, "task", user_map)
            prefix = ""
            if not author_id:
                prefix = f"Исходный автор: {author_label}\n"
                author_id = self._current_target_user_id
            fields: dict[str, Any] = {
                "POST_MESSAGE": f"{prefix}{message}\n\n{marker}",
                "AUTHOR_ID": author_id,
            }
            if post_date:
                fields["POST_DATE"] = post_date
            if attachments:
                fields["UF_FORUM_MESSAGE_DOC"] = [f"n{file_id}" for file_id in attachments]
            try:
                target_comment = self.client.call("task.commentitem.add", {"TASKID": target_task_id, "FIELDS": fields})
                mapped = extract_id(target_comment)
                if mapped:
                    self.report.maps["task_comments"][map_key] = mapped
                self.report.add("create_task_comment", "TASK_COMMENT", map_key, "TASK", target_task_id, "OK", "classic comment API")
                continue
            except Exception as first_exc:
                pass

            retry_fields = dict(fields)
            retry_fields["AUTHOR_ID"] = self._current_target_user_id
            retry_fields["POST_MESSAGE"] = (
                f"Исходный автор: {author_label}\n"
                f"Исходная дата: {text(post_date)}\n\n"
                f"{message}\n\n{marker}"
            )
            try:
                target_comment = self.client.call("task.commentitem.add", {"TASKID": target_task_id, "FIELDS": retry_fields})
                mapped = extract_id(target_comment)
                if mapped:
                    self.report.maps["task_comments"][map_key] = mapped
                self.report.add("create_task_comment", "TASK_COMMENT", map_key, "TASK", target_task_id, "WARN", f"webhook author used after: {first_exc}")
                continue
            except Exception as retry_exc:
                pass

            fallback_text = (
                f"Исходный автор: {author_label}\n"
                f"Исходная дата: {text(post_date)}\n\n"
                f"{message}\n\n{marker}"
            )
            try:
                if attachments:
                    mapped = self._commit_task_chat_files(target_task_id, attachments, fallback_text)
                    fallback_method = "im.disk.file.commit"
                else:
                    mapped = self._send_task_chat_message(target_task_id, fallback_text)
                    fallback_method = "REST 3.0 task chat"
                if mapped:
                    self.report.maps["task_comments"][map_key] = mapped
                self.report.add(
                    "create_task_comment", "TASK_COMMENT", map_key, "TASK", target_task_id, "WARN",
                    f"{fallback_method} used after: {first_exc}; retry: {retry_exc}",
                )
            except Exception as chat_exc:
                self.report.add("create_task_comment", "TASK_COMMENT", map_key, "TASK", target_task_id, "WARN", f"classic: {first_exc}; retry: {retry_exc}; chat: {chat_exc}")

    def _import_task_checklist(self, old_task_id: str, target_task_id: int, user_map: Mapping[str, int]) -> None:
        try:
            items = self.source_client.call("task.checklistitem.getlist", {"TASKID": int(old_task_id), "ORDER": {"SORT_INDEX": "ASC"}}) or []
        except Exception as exc:
            self.report.add("create_checklist", "TASK", old_task_id, "TASK", target_task_id, "WARN", str(exc))
            return
        if not isinstance(items, list):
            return
        try:
            target_items = self.client.call("task.checklistitem.getlist", {"TASKID": target_task_id, "ORDER": {"SORT_INDEX": "ASC"}}) or []
        except Exception:
            target_items = []
        existing: dict[tuple[str, str, str], int] = {}
        for target_item in target_items:
            if not isinstance(target_item, dict):
                continue
            target_id = extract_id(target_item)
            key = (
                normalize_text(target_item.get("TITLE") or target_item.get("title")),
                text(target_item.get("SORT_INDEX") or target_item.get("sortIndex")),
                text(target_item.get("PARENT_ID") or target_item.get("parentId") or 0),
            )
            if target_id:
                existing[key] = target_id
        old_to_new: dict[str, int] = {}
        pending = [dict(item) for item in items if isinstance(item, dict)]
        for _pass in range(len(pending) + 1):
            progress = False
            for item in list(pending):
                old_id = text(item.get("ID"))
                old_parent = text(item.get("PARENT_ID"))
                if old_parent not in {"", "0"} and old_parent not in old_to_new:
                    continue
                parent_id = old_to_new.get(old_parent, 0)
                key = (normalize_text(item.get("TITLE")), text(item.get("SORT_INDEX")), text(parent_id))
                if key in existing:
                    old_to_new[old_id] = existing[key]
                    self.report.maps["checklist_items"][f"{old_task_id}:{old_id}"] = existing[key]
                    pending.remove(item)
                    progress = True
                    self.report.add("create_checklist", "CHECKLIST", f"{old_task_id}:{old_id}", "CHECKLIST", existing[key], "SKIP", "matching item exists")
                    continue
                members = []
                for member in item.get("MEMBERS") or []:
                    source_user = text(member.get("ID")) if isinstance(member, dict) else text(member)
                    target_user = self._context_user_target(source_user, "task", user_map)
                    if target_user:
                        members.append(target_user)
                fields = {
                    "PARENT_ID": parent_id,
                    "TITLE": text(item.get("TITLE")),
                    "SORT_INDEX": int(item.get("SORT_INDEX") or 0),
                    "IS_COMPLETE": text(item.get("IS_COMPLETE")) or "N",
                    "IS_IMPORTANT": text(item.get("IS_IMPORTANT")) or "N",
                    "MEMBERS": members,
                }
                try:
                    result = self.client.call("task.checklistitem.add", {"TASKID": target_task_id, "FIELDS": fields})
                    new_id = extract_id(result)
                    if new_id:
                        old_to_new[old_id] = new_id
                        self.report.maps["checklist_items"][f"{old_task_id}:{old_id}"] = new_id
                    self.report.add("create_checklist", "CHECKLIST", f"{old_task_id}:{old_id}", "CHECKLIST", new_id, "OK", "")
                except Exception as exc:
                    self.report.add("create_checklist", "CHECKLIST", f"{old_task_id}:{old_id}", "TASK", target_task_id, "WARN", str(exc))

                attachments = item.get("ATTACHMENTS") or {}
                values = list(attachments.values()) if isinstance(attachments, dict) else attachments if isinstance(attachments, list) else []
                transferred = []
                for idx, attachment in enumerate(values):
                    if not isinstance(attachment, dict) or not self.file_transfer:
                        continue
                    target_file = None
                    attachment_id = attachment.get("ATTACHMENT_ID") or attachment.get("attachmentId") or attachment.get("FILE_ID") or attachment.get("fileId") or attachment.get("id")
                    if attachment_id:
                        target_file = self.file_transfer.transfer_reference(attachment_id, prefer_attached=True)
                    download_url = attachment.get("DOWNLOAD_URL") or attachment.get("urlDownload") or attachment.get("url")
                    if not target_file and download_url:
                        target_file = self.file_transfer.transfer_url(
                            f"task:{old_task_id}:checklist:{old_id}:{idx}",
                            text(download_url),
                            text(attachment.get("NAME") or attachment.get("name")),
                        )
                    if target_file:
                        transferred.append(target_file)
                if transferred:
                    marker = migration_marker("CHECKLIST_FILE", f"{old_task_id}_{old_id}", "COMMENT")
                    fields_comment = {
                        "POST_MESSAGE": f"Файлы пункта чек-листа «{text(item.get('TITLE'))}»\n\n{marker}",
                        "AUTHOR_ID": self._current_target_user_id,
                        "UF_FORUM_MESSAGE_DOC": [f"n{file_id}" for file_id in transferred],
                    }
                    try:
                        self.client.call("task.commentitem.add", {"TASKID": target_task_id, "FIELDS": fields_comment})
                        self.report.add("attach_checklist_files", "CHECKLIST", f"{old_task_id}:{old_id}", "TASK", target_task_id, "OK", "comment")
                    except Exception as first_exc:
                        try:
                            self._commit_task_chat_files(
                                target_task_id,
                                transferred,
                                f"Файлы пункта чек-листа «{text(item.get('TITLE'))}».\n\n{marker}",
                            )
                            self.report.add("attach_checklist_files", "CHECKLIST", f"{old_task_id}:{old_id}", "TASK", target_task_id, "WARN", f"im.disk.file.commit used after: {first_exc}")
                        except Exception as chat_exc:
                            self.report.add("attach_checklist_files", "CHECKLIST", f"{old_task_id}:{old_id}", "TASK", target_task_id, "WARN", f"comment: {first_exc}; chat: {chat_exc}")
                pending.remove(item)
                progress = True
            if not pending or not progress:
                break
        for item in pending:
            self.report.add("create_checklist", "CHECKLIST", f"{old_task_id}:{item.get('ID')}", "TASK", target_task_id, "WARN", "parent checklist item was not created")

    def _set_task_status(self, old_task_id: str, target_task_id: int, status: str) -> None:
        methods: list[str] = []
        if status == "3":
            methods = ["tasks.task.start"]
        elif status in {"4", "5"}:
            methods = ["tasks.task.complete"]
        elif status == "6":
            methods = ["tasks.task.start", "tasks.task.defer"]
        if not methods:
            return
        try:
            for method in methods:
                self.client.call(method, {"taskId": target_task_id})
            self.report.add("set_task_status", "TASK", old_task_id, "TASK", target_task_id, "OK", status)
        except Exception as exc:
            self.report.add("set_task_status", "TASK", old_task_id, "TASK", target_task_id, "WARN", str(exc))

    def _record_applied_task_relations(
        self,
        row: Mapping[str, Any],
        target_task_id: int,
        task_map: Mapping[str, int],
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
    ) -> None:
        old_id = text(row.get("id"))
        parent_old = text(row.get("parentId"))
        if parent_old not in {"", "0"}:
            self.report.add_relation(
                relation_type="TASK_PARENT",
                source_from_type="TASK",
                source_from_id=old_id,
                source_to_type="TASK",
                source_to_id=parent_old,
                target_from_type="TASK",
                target_from_id=target_task_id,
                target_to_type="TASK" if parent_old in task_map else "",
                target_to_id=task_map.get(parent_old, ""),
                status="APPLIED" if parent_old in task_map else "SKIP",
                details="subtask hierarchy",
            )
        for reference in row.get("ufCrmTask") or []:
            mapped = self._map_crm_ref(
                text(reference), company_map, contact_map, lead_map, deal_map
            )
            self.report.add_relation(
                relation_type="TASK_CRM",
                source_from_type="TASK",
                source_from_id=old_id,
                source_to_type=text(reference).split("_", 1)[0].upper(),
                source_to_id=text(reference).split("_", 1)[-1],
                target_from_type="TASK",
                target_from_id=target_task_id,
                target_to_type=text(mapped).split("_", 1)[0].upper() if mapped else "",
                target_to_id=text(mapped).split("_", 1)[-1] if mapped else "",
                status="APPLIED" if mapped else "SKIP",
                details={"source_reference": reference, "target_reference": mapped},
            )

    def import_tasks(self, user_map: Mapping[str, int], company_map: Mapping[str, int], contact_map: Mapping[str, int], lead_map: Mapping[str, int], deal_map: Mapping[str, int], *, max_items: int = 0) -> None:
        self.load_source("Tasks", "Users")
        rows = self._task_rows_topological(self._source["Tasks"])
        rows = self._select_sample_task_rows(
            rows, max_items, company_map, contact_map, lead_map, deal_map
        )
        existing = self._existing_tasks()
        task_map = dict(self.report.maps["tasks"])

        for row in rows:
            old_id = text(row.get("id"))
            skip_reason = self._task_skip_reason(row)
            if skip_reason:
                self.report.add("create_task", "TASK", old_id, "TASK", "", "SKIP", skip_reason)
                continue

            if old_id in existing:
                task_map[old_id] = existing[old_id]

            fields, blocking, warnings = self._task_registry_fields(
                row, user_map, company_map, contact_map, lead_map, deal_map, task_map
            )
            for warning in warnings:
                self.report.add("prepare_task", "TASK", old_id, "TASK", existing.get(old_id, ""), "WARN", warning)

            preview_fields = dict(fields)
            target_fields = dict(fields)
            target_fields.pop("SOURCE_FILE_REFERENCES", None)

            if old_id in existing:
                target_id = existing[old_id]
                task_map[old_id] = target_id
                self.report.maps["tasks"][old_id] = target_id
                update_status = "OK"
                update_message = "existing task updated"
                if blocking:
                    update_status = "WARN"
                    update_message = "main fields not updated: " + "; ".join(blocking)
                else:
                    # Creator and XML_ID identify the historical task and should
                    # not be rewritten on reruns. All operational fields are
                    # refreshed so earlier test imports can be repaired.
                    update_fields = dict(target_fields)
                    update_fields.pop("CREATED_BY", None)
                    update_fields.pop("XML_ID", None)
                    try:
                        self.client.call(
                            "tasks.task.update",
                            {"taskId": target_id, "fields": update_fields},
                        )
                    except Exception as exc:  # noqa: BLE001
                        update_status = "WARN"
                        update_message = f"existing task main fields could not be updated: {exc}"
                self.report.add("update_task", "TASK", old_id, "TASK", target_id, update_status, update_message)
                self.report.add_transfer(
                    operation="update_task",
                    source_type="TASK",
                    source_id=old_id,
                    target_type="TASK",
                    target_id=target_id,
                    status="WARN" if blocking or warnings or update_status == "WARN" else "OK",
                    payload=preview_fields,
                    route="TASK_WITHOUT_PROJECT",
                )
            else:
                if blocking:
                    message = "Пропущено: " + "; ".join(blocking)
                    self.report.add("create_task", "TASK", old_id, "TASK", "", "SKIP", message)
                    self.report.add_transfer(
                        operation="create_task",
                        source_type="TASK",
                        source_id=old_id,
                        target_type="TASK",
                        target_id="",
                        status="SKIP",
                        payload=preview_fields,
                        route="TASK_WITHOUT_PROJECT",
                    )
                    continue
                try:
                    result = self.client.call("tasks.task.add", {"fields": target_fields})
                    target_id = extract_id(result)
                    if not target_id:
                        raise RuntimeError(f"tasks.task.add returned no task ID: {result}")
                except Exception as exc:  # noqa: BLE001
                    self.report.add("create_task", "TASK", old_id, "TASK", "", "SKIP", str(exc))
                    self.report.add_transfer(
                        operation="create_task",
                        source_type="TASK",
                        source_id=old_id,
                        target_type="TASK",
                        target_id="",
                        status="SKIP",
                        payload=preview_fields,
                        route="TASK_WITHOUT_PROJECT",
                    )
                    continue
                task_map[old_id] = target_id
                self.report.maps["tasks"][old_id] = target_id
                self.report.add(
                    "create_task", "TASK", old_id, "TASK", target_id,
                    "WARN" if warnings else "OK",
                    "; ".join(warnings) if warnings else "project/group removed; CRM links remapped",
                )
                self.report.add_transfer(
                    operation="create_task",
                    source_type="TASK",
                    source_id=old_id,
                    target_type="TASK",
                    target_id=target_id,
                    status="WARN" if warnings else "OK",
                    payload=preview_fields,
                    route="TASK_WITHOUT_PROJECT",
                )

            self._record_applied_task_relations(
                row, target_id, task_map, company_map, contact_map, lead_map, deal_map
            )
            disk_ids = self._transfer_task_files(row)
            self._attach_task_files(target_id, disk_ids, old_id, "existing task" if old_id in existing else "new task")
            self._import_task_checklist(old_id, target_id, user_map)
            self._import_task_comments(old_id, target_id, user_map)
            self._set_task_status(old_id, target_id, text(row.get("status")))

    def _map_owner(self, owner_type: int, old_id: str, company_map: Mapping[str, int], contact_map: Mapping[str, int], lead_map: Mapping[str, int], deal_map: Mapping[str, int]) -> tuple[int, int] | None:
        if owner_type == 4 and old_id in company_map:
            return 4, company_map[old_id]
        if owner_type == 3 and old_id in contact_map:
            return 3, contact_map[old_id]
        if owner_type == 1:
            key = f"LEAD:{old_id}:LEAD"
            if key in lead_map:
                return 1, lead_map[key]
            converted_deal = self._converted_lead_to_deal().get(old_id)
            if converted_deal:
                target = self._source_deal_target(converted_deal, lead_map, deal_map)
                if target:
                    kind, target_id = target
                    return (1, target_id) if kind == "lead" else (2, target_id)
        if owner_type == 2:
            target = self._source_deal_target(old_id, lead_map, deal_map)
            if target:
                kind, target_id = target
                return (1, target_id) if kind == "lead" else (2, target_id)
        return None

    def _existing_activities(self, *, include_saved_maps: bool = True) -> dict[str, int]:
        rows = self.client.list_all("crm.activity.list", {"select": ["ID", "ORIGINATOR_ID", "ORIGIN_ID"]})
        result: dict[str, int] = (
            dict(self.report.maps["activities"]) if include_saved_maps else {}
        )
        result.update({
            text(row.get("ORIGIN_ID")).removeprefix("ACTIVITY_"): int(row["ID"])
            for row in rows
            if text(row.get("ORIGINATOR_ID")) == "B24_CLOUD_MIGRATION" and text(row.get("ORIGIN_ID")).startswith("ACTIVITY_")
        })
        return result

    def _map_activity_communications(self, rows: Iterable[Mapping[str, Any]], company_map: Mapping[str, int], contact_map: Mapping[str, int], lead_map: Mapping[str, int], deal_map: Mapping[str, int]) -> tuple[list[dict[str, Any]], list[str]]:
        result: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for item in rows:
            mapped = dict(item)
            old_type = int(item.get("ENTITY_TYPE_ID") or 0)
            old_id = text(item.get("ENTITY_ID"))
            if old_type and old_id:
                owner = self._map_owner(old_type, old_id, company_map, contact_map, lead_map, deal_map)
                if not owner:
                    unresolved.append(f"{old_type}:{old_id}")
                    continue
                mapped["ENTITY_TYPE_ID"], mapped["ENTITY_ID"] = owner
            result.append(mapped)
        return result, unresolved

    @staticmethod
    def _first_communication_value(row: Mapping[str, Any]) -> tuple[str, str]:
        """Return the first usable phone/email/IM value from a CRM client row."""
        for field, communication_type in (("PHONE", "PHONE"), ("EMAIL", "EMAIL"), ("IM", "IM"), ("WEB", "WEB")):
            values = row.get(field) or []
            if isinstance(values, Mapping):
                values = [values]
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            for item in values:
                if isinstance(item, Mapping):
                    value = text(item.get("VALUE")).strip()
                else:
                    value = text(item).strip()
                if value:
                    return communication_type, value
        return "", ""

    def _activity_source_client_candidates(self, activity: Mapping[str, Any]) -> list[tuple[int, str]]:
        """Resolve the real source client behind an activity owner.

        Bitrix stores the activity owner (lead/deal) separately from the client
        shown in the ``Клиент`` column. The latter must be a contact/company in
        ``COMMUNICATIONS``. Source activities often have an empty communication
        list, so the client is reconstructed from the linked CRM card.
        """
        self.load_source("Companies", "Contacts", "Leads", "Deals", "Deal_Contacts", "Lead_Contacts")
        companies = {text(row.get("ID")): row for row in self._source["Companies"]}
        contacts = {text(row.get("ID")): row for row in self._source["Contacts"]}
        leads = {text(row.get("ID")): row for row in self._source["Leads"]}
        deals = {text(row.get("ID")): row for row in self._source["Deals"]}

        candidates: list[tuple[int, str]] = []

        def add(entity_type: int, entity_id: Any) -> None:
            key = text(entity_id)
            if key and (entity_type, key) not in candidates:
                candidates.append((entity_type, key))

        def add_contact_and_company(contact_id: Any) -> None:
            key = text(contact_id)
            if not key:
                return
            contact = contacts.get(key) or {}
            company_id = text(contact.get("COMPANY_ID"))
            if company_id:
                add(4, company_id)
            add(3, key)

        def add_from_crm_row(row: Mapping[str, Any], relation_dataset: str, parent_column: str) -> None:
            company_id = text(row.get("COMPANY_ID"))
            if company_id:
                add(4, company_id)
            primary_contact = text(row.get("CONTACT_ID"))
            if primary_contact:
                add_contact_and_company(primary_contact)
            parent_id = text(row.get("ID"))
            relation_rows = sorted(
                [
                    item for item in self._source[relation_dataset]
                    if text(item.get(parent_column)) == parent_id
                ],
                key=lambda item: (
                    0 if text(item.get("IS_PRIMARY")) == "Y" else 1,
                    int(item.get("SORT") or item.get("RELATION_ORDER") or 9999),
                ),
            )
            for relation in relation_rows:
                add_contact_and_company(relation.get("CONTACT_ID"))

        owner_type = int(activity.get("OWNER_TYPE_ID") or 0)
        owner_id = text(activity.get("OWNER_ID"))
        if owner_type == 4:
            add(4, owner_id)
        elif owner_type == 3:
            add_contact_and_company(owner_id)
        elif owner_type == 2 and owner_id in deals:
            add_from_crm_row(deals[owner_id], "Deal_Contacts", "DEAL_ID")
        elif owner_type == 1 and owner_id in leads:
            add_from_crm_row(leads[owner_id], "Lead_Contacts", "LEAD_ID")

        # Some source activities carry additional bindings whose first entry is
        # not necessarily the client-bearing lead/deal. Use them as a fallback.
        for binding in activity.get("BINDINGS") or []:
            if not isinstance(binding, Mapping):
                continue
            entity_type = int(binding.get("OWNER_TYPE_ID") or binding.get("ENTITY_TYPE_ID") or 0)
            entity_id = text(binding.get("OWNER_ID") or binding.get("ENTITY_ID"))
            if entity_type == 4:
                add(4, entity_id)
            elif entity_type == 3:
                add_contact_and_company(entity_id)
            elif entity_type == 2 and entity_id in deals:
                add_from_crm_row(deals[entity_id], "Deal_Contacts", "DEAL_ID")
            elif entity_type == 1 and entity_id in leads:
                add_from_crm_row(leads[entity_id], "Lead_Contacts", "LEAD_ID")

        return candidates

    def _activity_client_communications(
        self,
        activity: Mapping[str, Any],
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """Build communications that point to the actual client, not owner lead/deal."""
        mapped, unresolved = self._map_activity_communications(
            activity.get("COMMUNICATIONS") or [], company_map, contact_map, lead_map, deal_map
        )
        warnings: list[str] = []
        existing_clients = [
            item for item in mapped
            if int(item.get("ENTITY_TYPE_ID") or 0) in {3, 4}
        ]
        dropped_owner_links = [
            item for item in mapped
            if int(item.get("ENTITY_TYPE_ID") or 0) in {1, 2}
        ]
        if dropped_owner_links:
            warnings.append("lead/deal communication replaced by actual client")

        self.load_source("Companies", "Contacts")
        source_rows = {
            4: {text(row.get("ID")): row for row in self._source["Companies"]},
            3: {text(row.get("ID")): row for row in self._source["Contacts"]},
        }

        result: list[dict[str, Any]] = []
        seen: set[tuple[int, int, str, str]] = set()

        def append(item: Mapping[str, Any]) -> None:
            entity_type = int(item.get("ENTITY_TYPE_ID") or 0)
            entity_id = int(item.get("ENTITY_ID") or 0)
            communication_type = text(item.get("TYPE"))
            value = text(item.get("VALUE"))
            key = (entity_type, entity_id, communication_type, value)
            if entity_type not in {3, 4} or not entity_id or key in seen:
                return
            seen.add(key)
            cleaned: dict[str, Any] = {
                "ENTITY_TYPE_ID": entity_type,
                "ENTITY_ID": entity_id,
            }
            if communication_type:
                cleaned["TYPE"] = communication_type
            if value:
                cleaned["VALUE"] = value
            result.append(cleaned)

        for old_type, old_id in self._activity_source_client_candidates(activity):
            target_id = company_map.get(old_id) if old_type == 4 else contact_map.get(old_id)
            if not target_id:
                continue
            matching = [
                item for item in existing_clients
                if int(item.get("ENTITY_TYPE_ID") or 0) == old_type
                and int(item.get("ENTITY_ID") or 0) == int(target_id)
            ]
            if matching:
                for item in matching:
                    append(item)
                continue
            source_row = source_rows.get(old_type, {}).get(old_id, {})
            communication_type, value = self._first_communication_value(source_row)
            append({
                "TYPE": communication_type,
                "VALUE": value,
                "ENTITY_TYPE_ID": old_type,
                "ENTITY_ID": int(target_id),
            })

        for item in existing_clients:
            append(item)

        if not result:
            warnings.append("actual client communication is unavailable; Client column will be empty")
        return result, unresolved, warnings

    def _ensure_activity_client_communications(
        self, old_id: str, target_id: int, communications: Sequence[Mapping[str, Any]]
    ) -> None:
        """Repair already-created activities during an idempotent rerun."""
        if not communications:
            return
        try:
            self.client.call(
                "crm.activity.update",
                {"id": target_id, "fields": {"COMMUNICATIONS": [dict(item) for item in communications]}},
            )
            self.report.add(
                "update_activity_client", "ACTIVITY", old_id, "ACTIVITY", target_id, "OK",
                "Client communication points to contact/company",
            )
        except Exception as exc:
            self.report.add(
                "update_activity_client", "ACTIVITY", old_id, "ACTIVITY", target_id, "WARN", str(exc)
            )

    @staticmethod
    def _unresolved_communications_note(
        rows: Iterable[Mapping[str, Any]], unresolved: Iterable[str]
    ) -> str:
        unresolved_set = {text(value) for value in unresolved}
        notes: list[str] = []
        for item in rows:
            old_type = int(item.get("ENTITY_TYPE_ID") or 0)
            old_id = text(item.get("ENTITY_ID"))
            key = f"{old_type}:{old_id}"
            if key not in unresolved_set:
                continue
            communication_type = text(item.get("TYPE")) or "UNKNOWN"
            value = text(item.get("VALUE")) or "без значения"
            notes.append(
                f"{communication_type} {value} (исходная CRM-сущность {key} отсутствует в дампе)"
            )
        if not notes:
            return ""
        return "Непривязанные коммуникации из облачного Bitrix24: " + "; ".join(notes)

    def _activity_files(self, activity: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Download source activity files and build crm.activity fileData payloads."""
        if not self.file_transfer:
            return []
        old_id = text(activity.get("ID"))
        live: Mapping[str, Any] = {}
        try:
            result = self.source_client.call("crm.activity.get", {"id": int(old_id)}) or {}
            if isinstance(result, dict):
                live = result
        except Exception as exc:
            self.report.add("read_activity_files", "ACTIVITY", old_id, "ACTIVITY", "", "WARN", str(exc))
        files = live.get("FILES") or activity.get("FILES") or []
        result: list[dict[str, Any]] = []
        for index, item in enumerate(files):
            resolved = None
            if isinstance(item, dict):
                source_file_id = item.get("FILE_ID") or item.get("fileId") or item.get("id")
                if source_file_id:
                    resolved = self.file_transfer.activity_payload_reference(
                        source_file_id, prefer_attached=False
                    )
                download_url = item.get("DOWNLOAD_URL") or item.get("urlDownload") or item.get("url")
                if not resolved and download_url:
                    resolved = self.file_transfer.activity_payload_url(
                        f"activity:{old_id}:{index}",
                        text(download_url),
                        text(item.get("NAME") or item.get("name")),
                    )
            elif item:
                resolved = self.file_transfer.activity_payload_reference(item, prefer_attached=False)
            if resolved:
                key, name, payload = resolved
                result.append({"key": key, "name": name, "payload": payload})
        return result

    def _activity_bindings(self, activity: Mapping[str, Any], company_map: Mapping[str, int], contact_map: Mapping[str, int], lead_map: Mapping[str, int], deal_map: Mapping[str, int]) -> tuple[list[tuple[int, int]], list[str]]:
        old_id = text(activity.get("ID"))
        source_bindings: list[dict[str, Any]] = []
        try:
            result = self.source_client.call("crm.activity.binding.list", {"activityId": int(old_id)}) or []
            if isinstance(result, list):
                source_bindings = [dict(item) for item in result if isinstance(item, dict)]
        except Exception as exc:
            self.report.add("read_activity_bindings", "ACTIVITY", old_id, "ACTIVITY", "", "WARN", str(exc))
        source_bindings.append({"OWNER_TYPE_ID": activity.get("OWNER_TYPE_ID"), "OWNER_ID": activity.get("OWNER_ID")})
        mapped: list[tuple[int, int]] = []
        unresolved: list[str] = []
        seen_source: set[tuple[int, str]] = set()
        for binding in source_bindings:
            source_type = int(binding.get("OWNER_TYPE_ID") or binding.get("ownerTypeId") or 0)
            source_id = text(binding.get("OWNER_ID") or binding.get("ownerId"))
            if not source_type or not source_id:
                continue
            source_key = (source_type, source_id)
            if source_key in seen_source:
                continue
            seen_source.add(source_key)
            owner = self._map_owner(source_type, source_id, company_map, contact_map, lead_map, deal_map)
            if owner:
                if owner not in mapped:
                    mapped.append(owner)
            else:
                unresolved.append(f"{source_type}:{source_id}")
        return mapped, unresolved

    def _ensure_activity_bindings(self, old_id: str, target_id: int, bindings: Sequence[tuple[int, int]]) -> None:
        try:
            current = self.client.call("crm.activity.binding.list", {"activityId": target_id}) or []
            current_pairs = {
                (int(item.get("OWNER_TYPE_ID") or item.get("ownerTypeId") or 0), int(item.get("OWNER_ID") or item.get("ownerId") or 0))
                for item in current
                if isinstance(item, dict)
            }
        except Exception:
            current_pairs = set()
        for owner_type, owner_id in bindings:
            if (owner_type, owner_id) in current_pairs:
                self.report.add("bind_activity", "ACTIVITY", old_id, "ACTIVITY", target_id, "SKIP", f"{owner_type}:{owner_id}")
                continue
            try:
                self.client.call("crm.activity.binding.add", {"activityId": target_id, "entityTypeId": owner_type, "entityId": owner_id})
                self.report.add("bind_activity", "ACTIVITY", old_id, "ACTIVITY", target_id, "OK", f"{owner_type}:{owner_id}")
            except Exception as exc:
                self.report.add("bind_activity", "ACTIVITY", old_id, "ACTIVITY", target_id, "WARN", f"{owner_type}:{owner_id}: {exc}")

    @staticmethod
    def _activity_target_file_info(activity: Mapping[str, Any]) -> list[tuple[str, int]]:
        result: list[tuple[str, int]] = []
        for item in activity.get("FILES") or []:
            if not isinstance(item, dict):
                continue
            name = text(item.get("NAME") or item.get("name"))
            file_id = int(item.get("ID") or item.get("id") or item.get("FILE_ID") or item.get("fileId") or 0)
            result.append((name, file_id))
        return result

    def _ensure_activity_files(
        self, old_id: str, target_id: int, files: Sequence[Mapping[str, Any]]
    ) -> None:
        if not files:
            return
        try:
            current = self.client.call("crm.activity.get", {"id": target_id}) or {}
            current = current if isinstance(current, dict) else {}
        except Exception as exc:
            self.report.add("attach_activity_files", "ACTIVITY", old_id, "ACTIVITY", target_id, "WARN", f"cannot read target activity: {exc}")
            return
        current_info = self._activity_target_file_info(current)
        current_by_name = {name: file_id for name, file_id in current_info if name}
        missing = [item for item in files if text(item.get("name")) not in current_by_name]
        for item in files:
            name = text(item.get("name"))
            if name in current_by_name:
                key = text(item.get("key"))
                file_id = current_by_name[name]
                if key and file_id:
                    self.report.maps["files"][key] = file_id
                self.report.add("attach_activity_file", "FILE", key, "ACTIVITY", target_id, "SKIP", name)
        if not missing:
            return
        if current_info:
            self.report.add(
                "attach_activity_files", "ACTIVITY", old_id, "ACTIVITY", target_id, "WARN",
                "target activity already has different files; refusing to replace them automatically",
            )
            return
        try:
            self.client.call(
                "crm.activity.update",
                {"id": target_id, "fields": {"FILES": [item["payload"] for item in missing]}},
            )
            refreshed = self.client.call("crm.activity.get", {"id": target_id}) or {}
            refreshed = refreshed if isinstance(refreshed, dict) else {}
            refreshed_by_name = {name: file_id for name, file_id in self._activity_target_file_info(refreshed)}
            for item in missing:
                key = text(item.get("key"))
                name = text(item.get("name"))
                file_id = refreshed_by_name.get(name, 0)
                if key and file_id:
                    self.report.maps["files"][key] = file_id
                self.report.add(
                    "attach_activity_file", "FILE", key, "ACTIVITY", target_id,
                    "OK" if file_id else "WARN", name,
                )
        except Exception as exc:
            self.report.add("attach_activity_files", "ACTIVITY", old_id, "ACTIVITY", target_id, "WARN", str(exc))

    def import_activities(
        self,
        user_map: Mapping[str, int],
        company_map: Mapping[str, int],
        contact_map: Mapping[str, int],
        lead_map: Mapping[str, int],
        deal_map: Mapping[str, int],
        *,
        max_items: int = 0,
    ) -> None:
        self.load_source("CRM_Activities")
        rows = self._select_sample_activity_rows(
            self._source["CRM_Activities"], max_items,
            company_map, contact_map, lead_map, deal_map,
        )
        existing = self._existing_activities()
        for activity in rows:
            old_id = text(activity.get("ID"))
            fields, bindings, problems, warnings = self._activity_registry_fields(
                activity, user_map, company_map, contact_map, lead_map, deal_map
            )
            for warning in warnings:
                self.report.add(
                    "prepare_activity", "ACTIVITY", old_id, "ACTIVITY", existing.get(old_id, ""),
                    "WARN", warning,
                )
            files = self._activity_files(activity)
            preview_fields = dict(fields)
            target_fields = dict(fields)
            target_fields.pop("SOURCE_FILE_REFERENCES", None)
            # File payloads are handled separately and idempotently.
            target_fields.pop("FILES", None)

            if old_id in existing:
                target_id = existing[old_id]
                self.report.maps["activities"][old_id] = target_id
                update_status = "OK"
                update_message = "existing activity updated"
                if problems:
                    update_status = "WARN"
                    update_message = "main fields not updated: " + "; ".join(problems)
                else:
                    update_fields = dict(target_fields)
                    update_fields.pop("ORIGINATOR_ID", None)
                    update_fields.pop("ORIGIN_ID", None)
                    try:
                        self.client.call(
                            "crm.activity.update",
                            {"id": target_id, "fields": update_fields},
                        )
                    except Exception as exc:  # noqa: BLE001
                        update_status = "WARN"
                        update_message = f"existing activity main fields could not be updated: {exc}"
                self.report.add(
                    "update_activity", "ACTIVITY", old_id, "ACTIVITY", target_id,
                    update_status, update_message,
                )
                self.report.add_transfer(
                    operation="update_activity", source_type="ACTIVITY", source_id=old_id,
                    target_type="ACTIVITY", target_id=target_id,
                    status="WARN" if problems or warnings or update_status == "WARN" else "OK",
                    payload=preview_fields, route="CRM_ACTIVITY",
                )
            else:
                if problems:
                    message = "Пропущено: " + "; ".join(problems)
                    self.report.add("create_activity", "ACTIVITY", old_id, "ACTIVITY", "", "SKIP", message)
                    self.report.add_transfer(
                        operation="create_activity", source_type="ACTIVITY", source_id=old_id,
                        target_type="ACTIVITY", target_id="", status="SKIP",
                        payload=preview_fields, route="CRM_ACTIVITY",
                    )
                    continue
                try:
                    raw_result = self.client.call("crm.activity.add", {"fields": target_fields})
                    target_id = extract_id(raw_result)
                    if not target_id:
                        raise RuntimeError(f"crm.activity.add returned no ID: {raw_result}")
                except Exception as exc:  # noqa: BLE001
                    self.report.add(
                        "create_activity", "ACTIVITY", old_id, "ACTIVITY", "", "SKIP",
                        f"activity could not be created: {exc}",
                    )
                    self.report.add_transfer(
                        operation="create_activity", source_type="ACTIVITY", source_id=old_id,
                        target_type="ACTIVITY", target_id="", status="SKIP",
                        payload=preview_fields, route="CRM_ACTIVITY",
                    )
                    continue
                self.report.maps["activities"][old_id] = target_id
                self.report.add(
                    "create_activity", "ACTIVITY", old_id, "ACTIVITY", target_id,
                    "WARN" if warnings else "OK", "; ".join(warnings),
                )
                self.report.add_transfer(
                    operation="create_activity", source_type="ACTIVITY", source_id=old_id,
                    target_type="ACTIVITY", target_id=target_id,
                    status="WARN" if warnings else "OK", payload=preview_fields, route="CRM_ACTIVITY",
                )

            for owner_type, owner_id in bindings:
                self.report.add_relation(
                    relation_type="ACTIVITY_CRM_BINDING",
                    source_from_type="ACTIVITY", source_from_id=old_id,
                    source_to_type="CRM", source_to_id=text(activity.get("OWNER_ID")),
                    target_from_type="ACTIVITY", target_from_id=target_id,
                    target_to_type=CRM_OWNER_TYPES.get(owner_type, "CRM").upper(),
                    target_to_id=owner_id, status="APPLIED",
                    details={"owner_type_id": owner_type},
                )
            self._ensure_activity_bindings(old_id, target_id, bindings)
            self._ensure_activity_client_communications(
                old_id, target_id, target_fields.get("COMMUNICATIONS") or []
            )
            self._ensure_activity_files(old_id, target_id, files)

    def verify(self) -> dict[str, Any]:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        plan = self.source_plan()
        result: dict[str, Any] = {"expected": plan, "markers": {}}
        for label, entity, source_type, route in (
            ("companies", "company", "COMPANY", "COMPANY"),
            ("contacts", "contact", "CONTACT", "CONTACT"),
        ):
            markers = self._existing_markers(entity)
            result["markers"][label] = sum(
                1 for key in markers if key[0] == source_type and key[2] == route
            )
        lead_markers = self._existing_markers("lead")
        deal_markers = self._existing_markers("deal")
        result["markers"]["deals_routed_to_leads"] = sum(
            1 for key in lead_markers if key[0] == "DEAL" and key[2] == "LEAD"
        )
        result["markers"]["deals_kept_as_deals"] = sum(
            1 for key in deal_markers if key[0] == "DEAL" and key[2] == "DEAL"
        )
        result["markers"]["tasks"] = len(self._existing_tasks(include_saved_maps=False))
        result["markers"]["activities"] = len(self._existing_activities(include_saved_maps=False))

        requisites = self.client.list_all(
            "crm.requisite.list",
            {"order": {"ID": "ASC"}, "filter": {}, "select": ["ID", "XML_ID"]},
        )
        result["markers"]["requisites"] = sum(
            1 for row in requisites if text(row.get("XML_ID")).startswith("B24MIG_REQ_")
        )
        addresses = self.client.list_all(
            "crm.address.list",
            {"filter": {"ENTITY_TYPE_ID": 8}, "order": {"ENTITY_ID": "ASC", "TYPE_ID": "ASC"}},
        )
        requisite_ids = {
            text(row.get("ID"))
            for row in requisites
            if text(row.get("XML_ID")).startswith("B24MIG_REQ_")
        }
        result["markers"]["addresses"] = len({
            (text(row.get("ENTITY_ID")), text(row.get("TYPE_ID")))
            for row in addresses
            if text(row.get("ENTITY_ID")) in requisite_ids
        })

        self.load_source("Addresses")
        expected_counts = {
            "companies": plan["source_counts"]["Companies"],
            "contacts": plan["source_counts"]["Contacts"],
            "deals_routed_to_leads": plan["source_deals_routed_to_leads"],
            "deals_kept_as_deals": plan["source_deals_kept_as_deals"],
            "tasks": plan["expected_tasks"],
            "activities": plan["expected_activities"],
            "requisites": plan["source_counts"]["Requisites"],
            "addresses": len(self._unique_source_addresses()),
        }
        gaps = {
            name: max(0, int(expected) - int(result["markers"].get(name, 0)))
            for name, expected in expected_counts.items()
        }
        result["expected_counts"] = expected_counts
        result["gaps"] = gaps
        result["count_complete"] = not any(gaps.values())
        result["complete"] = result["count_complete"]
        result["ok"] = result["count_complete"]
        result["scope"] = (
            "Entity marker counts plus migrated requisite and unique address counts."
        )
        result["limitations"] = [
            "Does not compare every field value in every target card.",
            "Does not prove every task comment, checklist item or binary file was readable in the source.",
            "Does not call every relation endpoint again; relation-level failures remain in the import reports.",
        ]
        result["policy"] = (
            "Verification reports gaps honestly and does not delete or recreate data automatically."
        )
        self.report.extra["verification"] = result
        return result

