from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from common.bitrix import BitrixClient, BitrixError
from .reporting import Report
from .xlsx_reader import XlsxReader, decode_jsonish

LOG = logging.getLogger(__name__)
MARKER_RE = re.compile(r"\[\[B24MIGRATION:([A-Z_]+):(\d+):([A-Z_]+)\]\]")

READONLY_KEYS = {
    "ID", "DATE_CREATE", "DATE_MODIFY", "MOVED_TIME", "MOVED_BY_ID", "CREATED_BY_ID", "MODIFY_BY_ID",
    "CLOSED", "STAGE_SEMANTIC_ID", "STATUS_SEMANTIC_ID", "IS_NEW", "LAST_ACTIVITY_TIME",
    "LAST_ACTIVITY_BY", "LAST_COMMUNICATION_TIME", "HAS_PHONE", "HAS_EMAIL", "HAS_IMOL",
}
HELPER_SUFFIXES = ("_NAME", "_EMAIL", "_DEPARTMENTS")


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def normalize_name(value: Any) -> str:
    return " ".join(text(value).strip().casefold().split())


def append_text(original: Any, addition: str) -> str:
    base = text(original).rstrip()
    if addition in base:
        return base
    return f"{base}\n\n{addition}".strip()


def migration_marker(source_type: str, source_id: Any, target_type: str) -> str:
    return f"[[B24MIGRATION:{source_type.upper()}:{source_id}:{target_type.upper()}]]"


def parse_marker(value: Any) -> tuple[str, str, str] | None:
    match = MARKER_RE.search(text(value))
    return match.groups() if match else None


def bool_y(value: Any) -> bool:
    return text(value).strip().upper() in {"Y", "YES", "TRUE", "1"}


def source_is_eqazyna(row: Mapping[str, Any]) -> bool:
    origin = normalize_name(row.get("ORIGINATOR_ID"))
    title = normalize_name(row.get("TITLE"))
    comments = normalize_name(row.get("COMMENTS"))
    return origin == "eqazyna" or title.startswith("e-qazyna") or "новая заявка e-qazyna" in comments


class MigrationProject:
    def __init__(
        self,
        source_xlsx: str | Path,
        config_path: str | Path,
        users_path: str | Path,
        output_dir: str | Path,
        client: BitrixClient | None,
    ):
        self.source_xlsx = Path(source_xlsx)
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.users_path = Path(users_path)
        self.report = Report(output_dir)
        self.client = client
        self._source: dict[str, list[dict[str, Any]]] = {}
        self._target_fields: dict[str, dict[str, Any]] = {}
        self._target_statuses: list[dict[str, Any]] = []
        self._target_userfields: dict[str, list[dict[str, Any]]] = {}
        self._source_enum_id_to_value: dict[str, str] = {}
        self._target_lead_enum_value_to_id: dict[str, str] = {}
        self._target_users: list[dict[str, Any]] = []
        self._user_config = self._load_users_csv()

    def _load_users_csv(self) -> list[dict[str, str]]:
        raw = self.users_path.read_bytes()
        text: str | None = None
        last_error: UnicodeDecodeError | None = None

        # GitHub/Windows may preserve CSV files saved either as UTF-8 or
        # Windows-1251. Support both so a spreadsheet editor cannot break the run.
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError as exc:
                last_error = exc

        if text is None:
            raise UnicodeError(
                f"Unable to decode users CSV as UTF-8 or Windows-1251: {self.users_path}"
            ) from last_error

        return [dict(row) for row in csv.DictReader(text.splitlines())]

    def load_source(self, *sheets: str) -> None:
        needed = [sheet for sheet in sheets if sheet not in self._source]
        if not needed:
            return
        with XlsxReader(self.source_xlsx) as reader:
            for sheet in needed:
                if sheet not in reader.sheet_names():
                    LOG.warning("Source sheet missing: %s", sheet)
                    self._source[sheet] = []
                else:
                    self._source[sheet] = reader.rows(sheet)
                    LOG.info("Loaded %-22s %s rows", sheet, len(self._source[sheet]))

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
        self._target_users = self.client.list_all("user.get", {"FILTER": {"ACTIVE": "Y"}})
        self._build_enum_maps()

    def _build_enum_maps(self) -> None:
        self.load_source("Deal_UserFields")
        source_code = self.config["field_mapping"]["lead_loss_reason"]["source_deal_field"]
        for field in self._source["Deal_UserFields"]:
            if field.get("FIELD_NAME") != source_code:
                continue
            values = field.get("LIST")
            if isinstance(values, str):
                values = decode_jsonish(values)
            for item in values or []:
                self._source_enum_id_to_value[text(item.get("ID"))] = text(item.get("VALUE"))
        target_code = self.config["field_mapping"]["lead_loss_reason"]["target_lead_field"]
        target_field = next((x for x in self._target_userfields.get("lead", []) if x.get("FIELD_NAME") == target_code), None)
        if target_field:
            values = target_field.get("LIST") or []
            for item in values:
                self._target_lead_enum_value_to_id[normalize_name(item.get("VALUE"))] = text(item.get("ID"))

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
                elif normalize_name(found.get("NAME")) != normalize_name(name):
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
        expected_enum = [normalize_name(v) for v in cfg["field_mapping"]["lead_loss_reason"]["expected_values"]]
        missing_enum_values = [v for v in expected_enum if v not in self._target_lead_enum_value_to_id]
        result = {
            "missing_statuses": missing_statuses,
            "status_name_differences": wrong_names,
            "missing_fields": missing_fields,
            "wrong_field_types": wrong_types,
            "missing_lead_loss_reason_values": missing_enum_values,
            "ok": not (missing_statuses or missing_fields or wrong_types or missing_enum_values),
        }
        self.report.extra["target_validation"] = result
        return result

    # ---------- source plan ----------

    def route_source_deal(self, row: Mapping[str, Any]) -> tuple[str, str]:
        stage = text(row.get("STAGE_ID"))
        previous = text(row.get("PREVIOUS_STAGE_ID"))
        routing = self.config["routing"]
        if stage == "LOSE":
            if previous in routing["lost_deal_previous_stages_kept_as_deal"]:
                return "deal", routing["deal_stage_map"]["LOSE"]
            return "lead", routing["lost_deal_target_lead_status"]
        if stage in routing["deal_to_lead_status_map"]:
            return "lead", routing["deal_to_lead_status_map"][stage]
        if stage in routing["deal_stage_map"]:
            return "deal", routing["deal_stage_map"][stage]
        raise ValueError(f"Unmapped source deal stage {stage!r} for deal {row.get('ID')}")

    def source_plan(self) -> dict[str, Any]:
        self.load_source("Companies", "Contacts", "Leads", "Deals", "Requisites", "Addresses", "Deal_Contacts", "Lead_Contacts", "Contact_Companies")
        route_counts: Counter[str] = Counter()
        stage_routes: Counter[str] = Counter()
        for deal in self._source["Deals"]:
            kind, target_status = self.route_source_deal(deal)
            route_counts[kind] += 1
            stage_routes[f"{deal.get('STAGE_ID')}->{kind}:{target_status}"] += 1
        plan = {
            "source_counts": {name: len(rows) for name, rows in self._source.items()},
            "source_deals_routed_to_leads": route_counts["lead"],
            "source_deals_kept_as_deals": route_counts["deal"],
            "expected_target_leads_total": len(self._source["Leads"]) + route_counts["lead"],
            "expected_target_deals_total": route_counts["deal"],
            "route_breakdown": dict(stage_routes),
            "only_crm_scope": [
                "users needed as CRM responsibles", "companies", "contacts", "leads", "deals",
                "company/contact/lead/deal relations", "requisites and addresses", "three target user fields",
            ],
            "excluded": ["tasks", "projects", "workgroups", "activities", "timeline", "files", "smart processes", "products"],
        }
        self.report.extra["source_plan"] = plan
        return plan

    # ---------- users ----------

    def _target_users_by_email(self) -> dict[str, dict[str, Any]]:
        return {normalize_name(x.get("EMAIL")): x for x in self._target_users if x.get("EMAIL")}

    @staticmethod
    def _target_users_by_full_name(users: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for user in users:
            full_name = normalize_name(f"{text(user.get('NAME'))} {text(user.get('LAST_NAME'))}")
            if full_name:
                result[full_name].append(user)
        return result

    @staticmethod
    def _match_target_user(
        row: Mapping[str, Any],
        by_email: Mapping[str, dict[str, Any]],
        by_full_name: Mapping[str, list[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        email = normalize_name(row.get("email"))
        if email and email in by_email:
            return by_email[email]
        full_name = normalize_name(f"{text(row.get('name'))} {text(row.get('last_name'))}")
        matches = by_full_name.get(full_name, [])
        return matches[0] if len(matches) == 1 else None

    def build_user_map(self, *, strict: bool = True) -> dict[str, int]:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        if not self._target_users:
            self._target_users = self.client.list_all("user.get", {"FILTER": {"ACTIVE": "Y"}})
        by_email = self._target_users_by_email()
        by_full_name = self._target_users_by_full_name(self._target_users)
        current = self.client.call("user.current") or {}
        fallback_id = int(current.get("ID", 0) or 0)
        result: dict[str, int] = {}
        missing: list[dict[str, str]] = []
        for row in self._user_config:
            source_id = text(row.get("source_user_id"))
            target = self._match_target_user(row, by_email, by_full_name)
            if target:
                result[source_id] = int(target["ID"])
                self.report.add("map_user", "USER", source_id, "USER", target["ID"], "OK", text(target.get("EMAIL")))
            else:
                missing.append(row)
        if missing and strict:
            emails = ", ".join(row.get("email", "") for row in missing)
            raise RuntimeError(f"Target users are missing: {emails}. Run workflow 02 first.")
        for row in missing:
            source_id = text(row.get("source_user_id"))
            result[source_id] = fallback_id
            self.report.add("map_user", "USER", source_id, "USER", fallback_id, "WARN", f"fallback for {row.get('email')}")
        self.report.maps["users"] = {k: int(v) for k, v in result.items()}
        return result

    def invite_users(self, *, default_department_id: int = 0, dry_run: bool = False) -> dict[str, int]:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        target_users = self.client.list_all("user.get", {})
        by_email = {normalize_name(x.get("EMAIL")): x for x in target_users if x.get("EMAIL")}
        by_full_name = self._target_users_by_full_name(target_users)
        departments = self.client.list_all("department.get", {})
        by_department_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for department in departments:
            by_department_name[normalize_name(department.get("NAME"))].append(department)
        mapping: dict[str, int] = {}
        for row in self._user_config:
            source_id = text(row.get("source_user_id"))
            existing = self._match_target_user(row, by_email, by_full_name)
            if existing:
                mapping[source_id] = int(existing["ID"])
                self.report.add("invite_user", "USER", source_id, "USER", existing["ID"], "SKIP", "already exists")
                continue
            if not bool_y(row.get("invite", "Y")):
                self.report.add("invite_user", "USER", source_id, "USER", "", "SKIP", "invite disabled; existing user not resolved")
                continue
            department_id = int(row.get("target_department_id") or 0)
            if not department_id:
                matches = by_department_name.get(normalize_name(row.get("target_department_name")), [])
                if len(matches) == 1:
                    department_id = int(matches[0]["ID"])
            if not department_id:
                department_id = default_department_id
            if not department_id:
                self.report.add("invite_user", "USER", source_id, "USER", "", "ERROR", "target department not resolved")
                continue
            fields = {
                "EMAIL": row.get("email", "").strip(),
                "NAME": row.get("name", "").strip(),
                "LAST_NAME": row.get("last_name", "").strip(),
                "UF_DEPARTMENT": [department_id],
            }
            if dry_run:
                self.report.add("invite_user", "USER", source_id, "USER", "", "DRY_RUN", json.dumps(fields, ensure_ascii=False))
                continue
            try:
                target_id = int(self.client.call("user.add", fields))
                mapping[source_id] = target_id
                self.report.add("invite_user", "USER", source_id, "USER", target_id, "OK", fields["EMAIL"])
            except Exception as exc:
                self.report.add("invite_user", "USER", source_id, "USER", "", "ERROR", str(exc))
        self.report.maps["users"].update({k: v for k, v in mapping.items()})
        return mapping

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
            if code in excluded or code.startswith("UF_") or not self._is_target_field_writable(entity, code):
                continue
            if value in (None, "", [], {}):
                continue
            value = decode_jsonish(value)
            result[code] = value
        return result

    def _target_source_id(self, row: Mapping[str, Any]) -> str:
        if source_is_eqazyna(row):
            return self.config["target_source_id"]
        source = text(row.get("SOURCE_ID"))
        available = {text(x.get("STATUS_ID")) for x in self._target_statuses if x.get("ENTITY_ID") == "SOURCE"}
        return source if source in available else "OTHER"

    def _responsible(self, row: Mapping[str, Any], user_map: Mapping[str, int], fallback: int) -> int:
        return int(user_map.get(text(row.get("ASSIGNED_BY_ID")), fallback))

    def _enum_target_id(self, source_enum_id: Any) -> str:
        value = self._source_enum_id_to_value.get(text(source_enum_id), "")
        aliases = self.config["field_mapping"]["lead_loss_reason"].get("value_aliases", {})
        target_value = aliases.get(value, value)
        return self._target_lead_enum_value_to_id.get(normalize_name(target_value), "")

    def _source_loss_reason_value(self, source_enum_id: Any) -> str:
        return self._source_enum_id_to_value.get(text(source_enum_id), "")

    def _existing_markers(self, entity: str) -> dict[tuple[str, str, str], int]:
        method = f"crm.{entity}.list"
        select = ["ID", "COMMENTS"]
        if entity == "deal":
            select.append("ADDITIONAL_INFO")
        rows = self.client.list_all(method, {"select": select, "order": {"ID": "ASC"}}) if self.client else []
        result: dict[tuple[str, str, str], int] = {}
        for row in rows:
            marker = parse_marker(row.get("COMMENTS")) or parse_marker(row.get("ADDITIONAL_INFO"))
            if marker:
                result[marker] = int(row["ID"])
        return result

    def _batch_create(
        self,
        entity: str,
        prepared: list[tuple[str, dict[str, Any]]],
        *,
        dry_run: bool,
        max_items: int = 0,
    ) -> dict[str, int]:
        method = f"crm.{entity}.add"
        target_type = entity.upper()
        existing = self._existing_markers(entity) if not dry_run else {}
        mapping: dict[str, int] = {}
        commands: list[tuple[str, str, Mapping[str, Any]]] = []
        contexts: dict[str, tuple[str, tuple[str, str, str]]] = {}
        limited = prepared[:max_items] if max_items else prepared
        for index, (source_key, fields) in enumerate(limited):
            source_type, source_id, route = source_key.split(":", 2)
            marker_key = (source_type, source_id, route)
            if marker_key in existing:
                target_id = existing[marker_key]
                mapping[source_key] = target_id
                self.report.add(f"create_{entity}", source_type, source_id, target_type, target_id, "SKIP", "migration marker exists")
                continue
            if dry_run:
                self.report.add(f"create_{entity}", source_type, source_id, target_type, "", "DRY_RUN", text(fields.get("TITLE") or fields.get("NAME")))
                continue
            cmd_key = f"i{index}"
            commands.append((cmd_key, method, {"fields": fields}))
            contexts[cmd_key] = (source_key, marker_key)
        if dry_run:
            return mapping
        for success, errors in self.client.batch_chunks(commands, size=35):
            for cmd_key, target_id in success.items():
                source_key, marker_key = contexts[cmd_key]
                source_type, source_id, route = source_key.split(":", 2)
                mapping[source_key] = int(target_id)
                self.report.add(f"create_{entity}", source_type, source_id, target_type, target_id, "OK", route)
            for cmd_key, error in errors.items():
                source_key, marker_key = contexts[cmd_key]
                source_type, source_id, route = source_key.split(":", 2)
                self.report.add(f"create_{entity}", source_type, source_id, target_type, "", "ERROR", text(error))
        return mapping

    def prepare_companies(self, user_map: Mapping[str, int], fallback: int) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Companies")
        prepared = []
        for row in self._source["Companies"]:
            old_id = text(row.get("ID"))
            fields = self._copy_standard_fields("company", row, excluded={"ASSIGNED_BY_ID", "COMMENTS", "SOURCE_ID"})
            fields["ASSIGNED_BY_ID"] = self._responsible(row, user_map, fallback)
            if "SOURCE_ID" in self._target_fields["company"]:
                fields["SOURCE_ID"] = self._target_source_id(row)
            marker = migration_marker("COMPANY", old_id, "COMPANY")
            fields["COMMENTS"] = append_text(row.get("COMMENTS"), marker)
            prepared.append((f"COMPANY:{old_id}:COMPANY", fields))
        return prepared

    def prepare_contacts(self, user_map: Mapping[str, int], fallback: int, company_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Contacts")
        prepared = []
        for row in self._source["Contacts"]:
            old_id = text(row.get("ID"))
            fields = self._copy_standard_fields("contact", row, excluded={"ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "SOURCE_ID"})
            fields["ASSIGNED_BY_ID"] = self._responsible(row, user_map, fallback)
            old_company = text(row.get("COMPANY_ID"))
            if old_company and old_company in company_map:
                fields["COMPANY_ID"] = company_map[old_company]
            if "SOURCE_ID" in self._target_fields["contact"]:
                fields["SOURCE_ID"] = self._target_source_id(row)
            marker = migration_marker("CONTACT", old_id, "CONTACT")
            fields["COMMENTS"] = append_text(row.get("COMMENTS"), marker)
            prepared.append((f"CONTACT:{old_id}:CONTACT", fields))
        return prepared

    def prepare_original_leads(self, user_map: Mapping[str, int], fallback: int, company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
        self.load_source("Leads")
        prepared = []
        status_map = self.config["routing"]["source_lead_status_map"]
        for row in self._source["Leads"]:
            old_id = text(row.get("ID"))
            fields = self._copy_standard_fields("lead", row, excluded={"ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "CONTACT_ID", "STATUS_ID", "SOURCE_ID"})
            fields["ASSIGNED_BY_ID"] = self._responsible(row, user_map, fallback)
            fields["STATUS_ID"] = status_map.get(text(row.get("STATUS_ID")), "NEW")
            fields["SOURCE_ID"] = self._target_source_id(row)
            old_company = text(row.get("COMPANY_ID"))
            old_contact = text(row.get("CONTACT_ID"))
            if old_company in company_map:
                fields["COMPANY_ID"] = company_map[old_company]
            if old_contact in contact_map:
                fields["CONTACT_ID"] = contact_map[old_contact]
            marker = migration_marker("LEAD", old_id, "LEAD")
            fields["COMMENTS"] = append_text(row.get("COMMENTS"), marker)
            prepared.append((f"LEAD:{old_id}:LEAD", fields))
        return prepared

    def prepare_routed_deal_leads(self, user_map: Mapping[str, int], fallback: int, company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
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
            fields = self._copy_standard_fields("lead", row, excluded={
                "ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "CONTACT_ID", "STAGE_ID", "STATUS_ID", "SOURCE_ID", "CATEGORY_ID"
            })
            fields["TITLE"] = text(row.get("TITLE")) or f"Сделка {old_id}"
            fields["ASSIGNED_BY_ID"] = self._responsible(row, user_map, fallback)
            fields["STATUS_ID"] = target_status
            fields["SOURCE_ID"] = self._target_source_id(row)
            old_company = text(row.get("COMPANY_ID"))
            old_contact = text(row.get("CONTACT_ID"))
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
            comments = append_text(comments, migration_marker("DEAL", old_id, "LEAD"))
            fields["COMMENTS"] = comments
            prepared.append((f"DEAL:{old_id}:LEAD", fields))
        return prepared

    def prepare_deals(self, user_map: Mapping[str, int], fallback: int, company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> list[tuple[str, dict[str, Any]]]:
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
            fields = self._copy_standard_fields("deal", row, excluded={
                "ASSIGNED_BY_ID", "COMMENTS", "COMPANY_ID", "CONTACT_ID", "LEAD_ID", "MYCOMPANY_ID", "QUOTE_ID",
                "PREVIOUS_STAGE_ID", "STAGE_ID", "SOURCE_ID", "CATEGORY_ID"
            })
            fields["ASSIGNED_BY_ID"] = self._responsible(row, user_map, fallback)
            fields["STAGE_ID"] = target_stage
            fields["CATEGORY_ID"] = int(self.config.get("target_deal_category_id", 0))
            fields["SOURCE_ID"] = self._target_source_id(row)
            old_company = text(row.get("COMPANY_ID"))
            old_contact = text(row.get("CONTACT_ID"))
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

    # ---------- import ----------

    def import_crm(self, *, dry_run: bool = False, max_items: int = 0, strict_users: bool = True) -> None:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        self.discover_target()
        validation = self.validate_target()
        if not validation["ok"]:
            raise RuntimeError(f"Target validation failed: {json.dumps(validation, ensure_ascii=False)}")
        user_map = self.build_user_map(strict=strict_users)
        current = self.client.call("user.current") or {}
        fallback = int(current.get("ID", 0) or 0)

        companies = self.prepare_companies(user_map, fallback)
        company_keys = self._batch_create("company", companies, dry_run=dry_run, max_items=max_items)
        company_map = {key.split(":")[1]: value for key, value in company_keys.items()}
        self.report.maps["companies"].update(company_map)

        contacts = self.prepare_contacts(user_map, fallback, company_map)
        contact_keys = self._batch_create("contact", contacts, dry_run=dry_run, max_items=max_items)
        contact_map = {key.split(":")[1]: value for key, value in contact_keys.items()}
        self.report.maps["contacts"].update(contact_map)

        if not dry_run:
            self.import_contact_company_relations(contact_map, company_map)
            self.import_requisites(company_map, contact_map)

        leads = self.prepare_original_leads(user_map, fallback, company_map, contact_map)
        leads += self.prepare_routed_deal_leads(user_map, fallback, company_map, contact_map)
        lead_keys = self._batch_create("lead", leads, dry_run=dry_run, max_items=max_items)
        lead_map = {key: value for key, value in lead_keys.items()}
        self.report.maps["leads"].update(lead_map)

        deals = self.prepare_deals(user_map, fallback, company_map, contact_map)
        deal_keys = self._batch_create("deal", deals, dry_run=dry_run, max_items=max_items)
        deal_map = {key.split(":")[1]: value for key, value in deal_keys.items()}
        self.report.maps["deals"].update(deal_map)

        if not dry_run:
            self.import_crm_contact_relations(contact_map, lead_map, deal_map)
            self.import_requisite_links(deal_map)

    # ---------- relations / requisites ----------

    def import_contact_company_relations(self, contact_map: Mapping[str, int], company_map: Mapping[str, int]) -> None:
        self.load_source("Contact_Companies")
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._source["Contact_Companies"]:
            old_contact = text(row.get("CONTACT_ID"))
            old_company = text(row.get("COMPANY_ID"))
            if old_contact not in contact_map or old_company not in company_map:
                continue
            grouped[old_contact].append({
                "COMPANY_ID": company_map[old_company],
                "IS_PRIMARY": text(row.get("IS_PRIMARY")) or "N",
                "SORT": int(row.get("SORT") or 1000),
            })
        commands = []
        context = {}
        for index, (old_contact, items) in enumerate(grouped.items()):
            key = f"c{index}"
            commands.append((key, "crm.contact.company.items.set", {"id": contact_map[old_contact], "items": items}))
            context[key] = old_contact
        for success, errors in self.client.batch_chunks(commands, size=35):
            for key in success:
                old = context[key]
                self.report.add("set_contact_companies", "CONTACT", old, "CONTACT", contact_map[old], "OK", f"{len(grouped[old])} companies")
            for key, err in errors.items():
                old = context[key]
                self.report.add("set_contact_companies", "CONTACT", old, "CONTACT", contact_map[old], "ERROR", text(err))

    def import_crm_contact_relations(self, contact_map: Mapping[str, int], lead_map: Mapping[str, int], deal_map: Mapping[str, int]) -> None:
        self.load_source("Lead_Contacts", "Deal_Contacts")
        lead_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._source["Lead_Contacts"]:
            source_key = f"LEAD:{text(row.get('LEAD_ID'))}:LEAD"
            old_contact = text(row.get("CONTACT_ID"))
            if source_key in lead_map and old_contact in contact_map:
                lead_items[source_key].append({
                    "CONTACT_ID": contact_map[old_contact],
                    "SORT": int(row.get("SORT") or 10),
                    "IS_PRIMARY": text(row.get("IS_PRIMARY")) or "N",
                })
        # Deal contacts must follow the routed destination.
        deal_contact_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._source["Deal_Contacts"]:
            old_deal = text(row.get("DEAL_ID"))
            old_contact = text(row.get("CONTACT_ID"))
            if old_contact not in contact_map:
                continue
            item = {
                "CONTACT_ID": contact_map[old_contact],
                "SORT": int(row.get("SORT") or 10),
                "IS_PRIMARY": text(row.get("IS_PRIMARY")) or "N",
            }
            deal_contact_rows[old_deal].append(item)
        for old_deal, items in deal_contact_rows.items():
            lead_key = f"DEAL:{old_deal}:LEAD"
            if lead_key in lead_map:
                lead_items[lead_key].extend(items)

        commands = []
        context: dict[str, tuple[str, str, int]] = {}
        idx = 0
        for source_key, items in lead_items.items():
            # Deduplicate contacts while retaining first order.
            seen = set(); unique = []
            for item in items:
                if item["CONTACT_ID"] not in seen:
                    seen.add(item["CONTACT_ID"]); unique.append(item)
            key = f"l{idx}"; idx += 1
            commands.append((key, "crm.lead.contact.items.set", {"id": lead_map[source_key], "items": unique}))
            context[key] = (source_key, "LEAD", lead_map[source_key])
        for old_deal, items in deal_contact_rows.items():
            if old_deal not in deal_map:
                continue
            key = f"d{idx}"; idx += 1
            commands.append((key, "crm.deal.contact.items.set", {"id": deal_map[old_deal], "items": items}))
            context[key] = (old_deal, "DEAL", deal_map[old_deal])
        for success, errors in self.client.batch_chunks(commands, size=35):
            for key in success:
                source, kind, target_id = context[key]
                self.report.add("set_crm_contacts", kind, source, kind, target_id, "OK", "")
            for key, err in errors.items():
                source, kind, target_id = context[key]
                self.report.add("set_crm_contacts", kind, source, kind, target_id, "ERROR", text(err))

    def import_requisites(self, company_map: Mapping[str, int], contact_map: Mapping[str, int]) -> None:
        self.load_source("Requisites", "Addresses", "Requisite_Presets")
        target_presets = self.client.list_all("crm.requisite.preset.list", {"select": ["ID", "NAME", "XML_ID", "ENTITY_TYPE_ID"]})
        preset_by_xml = {text(x.get("XML_ID")): int(x["ID"]) for x in target_presets if x.get("XML_ID")}
        preset_by_name = {normalize_name(x.get("NAME")): int(x["ID"]) for x in target_presets if x.get("NAME")}
        source_preset = {text(x.get("ID")): x for x in self._source["Requisite_Presets"]}
        target_existing = self.client.list_all("crm.requisite.list", {"select": ["ID", "XML_ID", "ENTITY_TYPE_ID", "ENTITY_ID"]})
        existing_by_xml = {text(x.get("XML_ID")): int(x["ID"]) for x in target_existing if x.get("XML_ID")}
        req_map: dict[str, int] = {}
        commands = []; context = {}
        for index, row in enumerate(self._source["Requisites"]):
            old_id = text(row.get("ID")); source_entity_type = text(row.get("ENTITY_TYPE_ID")); old_entity = text(row.get("ENTITY_ID"))
            if source_entity_type == "4":
                target_entity = company_map.get(old_entity)
            elif source_entity_type == "3":
                target_entity = contact_map.get(old_entity)
            else:
                target_entity = None
            if not target_entity:
                continue
            xml_id = text(row.get("XML_ID")) or f"B24MIG_REQ_{old_id}"
            if xml_id in existing_by_xml:
                req_map[old_id] = existing_by_xml[xml_id]
                self.report.add("create_requisite", "REQUISITE", old_id, "REQUISITE", req_map[old_id], "SKIP", "XML_ID exists")
                continue
            src_preset = source_preset.get(text(row.get("PRESET_ID")), {})
            target_preset = preset_by_xml.get(text(src_preset.get("XML_ID"))) or preset_by_name.get(normalize_name(src_preset.get("NAME")))
            if not target_preset:
                self.report.add("create_requisite", "REQUISITE", old_id, "REQUISITE", "", "ERROR", f"preset not found: {src_preset.get('NAME')}")
                continue
            fields = self._copy_standard_fields("requisite", row, excluded={"ENTITY_ID", "ENTITY_TYPE_ID", "PRESET_ID", "XML_ID"})
            fields.update({"ENTITY_ID": target_entity, "ENTITY_TYPE_ID": int(source_entity_type), "PRESET_ID": target_preset, "XML_ID": xml_id})
            key = f"r{index}"
            commands.append((key, "crm.requisite.add", {"fields": fields}))
            context[key] = old_id
        for success, errors in self.client.batch_chunks(commands, size=30):
            for key, target_id in success.items():
                old_id = context[key]; req_map[old_id] = int(target_id)
                self.report.add("create_requisite", "REQUISITE", old_id, "REQUISITE", target_id, "OK", "")
            for key, err in errors.items():
                old_id = context[key]
                self.report.add("create_requisite", "REQUISITE", old_id, "REQUISITE", "", "ERROR", text(err))
        self.report.maps["requisites"].update(req_map)

        # Addresses use ENTITY_TYPE_ID=8 and ENTITY_ID=source requisite ID in this export.
        existing_addresses = self.client.list_all("crm.address.list", {"order": {"ENTITY_ID": "ASC"}})
        address_index = {
            (text(x.get("ENTITY_ID")), text(x.get("TYPE_ID")), normalize_name(x.get("ADDRESS_1")), normalize_name(x.get("CITY")))
            for x in existing_addresses
        }
        commands = []; context = {}
        for index, row in enumerate(self._source["Addresses"]):
            old_req = text(row.get("ENTITY_ID"))
            if text(row.get("ENTITY_TYPE_ID")) != "8" or old_req not in req_map:
                continue
            target_req = req_map[old_req]
            key_tuple = (text(target_req), text(row.get("TYPE_ID")), normalize_name(row.get("ADDRESS_1")), normalize_name(row.get("CITY")))
            if key_tuple in address_index:
                continue
            fields = self._copy_standard_fields("address", row, excluded={"ENTITY_ID", "ENTITY_TYPE_ID", "ANCHOR_ID", "ANCHOR_TYPE_ID", "LOC_ADDR_ID"})
            fields.update({"ENTITY_ID": target_req, "ENTITY_TYPE_ID": 8, "TYPE_ID": int(row.get("TYPE_ID") or 1)})
            key = f"a{index}"; commands.append((key, "crm.address.add", {"fields": fields})); context[key] = old_req
        for success, errors in self.client.batch_chunks(commands, size=30):
            for key in success:
                self.report.add("create_address", "REQUISITE", context[key], "ADDRESS", "", "OK", "")
            for key, err in errors.items():
                self.report.add("create_address", "REQUISITE", context[key], "ADDRESS", "", "ERROR", text(err))

    def import_requisite_links(self, deal_map: Mapping[str, int]) -> None:
        self.load_source("Requisite_Links")
        req_map = self.report.maps["requisites"]
        commands = []; context = {}
        for index, row in enumerate(self._source["Requisite_Links"]):
            if text(row.get("ENTITY_TYPE_ID")) != "2":
                continue
            old_deal = text(row.get("ENTITY_ID")); old_req = text(row.get("REQUISITE_ID"))
            if old_deal not in deal_map or old_req not in req_map:
                continue
            fields = {
                "ENTITY_TYPE_ID": 2,
                "ENTITY_ID": deal_map[old_deal],
                "REQUISITE_ID": req_map[old_req],
                "BANK_DETAIL_ID": 0,
                "MC_REQUISITE_ID": 0,
                "MC_BANK_DETAIL_ID": 0,
            }
            key = f"rl{index}"; commands.append((key, "crm.requisite.link.register", {"fields": fields})); context[key] = old_deal
        for success, errors in self.client.batch_chunks(commands, size=30):
            for key in success:
                self.report.add("link_requisite", "DEAL", context[key], "DEAL", deal_map[context[key]], "OK", "")
            for key, err in errors.items():
                self.report.add("link_requisite", "DEAL", context[key], "DEAL", deal_map[context[key]], "ERROR", text(err))

    # ---------- verify ----------

    def verify(self) -> dict[str, Any]:
        if not self.client:
            raise RuntimeError("Target Bitrix client is required")
        plan = self.source_plan()
        entities = {
            "companies": ("company", "COMPANY", "COMPANY"),
            "contacts": ("contact", "CONTACT", "CONTACT"),
        }
        result: dict[str, Any] = {"expected": plan, "markers": {}}
        for label, (entity, source_type, route) in entities.items():
            markers = self._existing_markers(entity)
            count = sum(1 for key in markers if key[0] == source_type and key[2] == route)
            result["markers"][label] = count
        lead_markers = self._existing_markers("lead")
        deal_markers = self._existing_markers("deal")
        result["markers"]["original_leads"] = sum(1 for key in lead_markers if key[0] == "LEAD")
        result["markers"]["deals_routed_to_leads"] = sum(1 for key in lead_markers if key[0] == "DEAL" and key[2] == "LEAD")
        result["markers"]["deals_kept_as_deals"] = sum(1 for key in deal_markers if key[0] == "DEAL" and key[2] == "DEAL")
        result["ok"] = (
            result["markers"]["companies"] >= plan["source_counts"]["Companies"]
            and result["markers"]["contacts"] >= plan["source_counts"]["Contacts"]
            and result["markers"]["original_leads"] >= plan["source_counts"]["Leads"]
            and result["markers"]["deals_routed_to_leads"] >= plan["source_deals_routed_to_leads"]
            and result["markers"]["deals_kept_as_deals"] >= plan["source_deals_kept_as_deals"]
        )
        self.report.extra["verification"] = result
        return result
