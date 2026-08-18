from __future__ import annotations

import random
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from common.naming import short_organization_name

from .bitrix_client import BitrixClient, BitrixError
from .formatter import (
    build_company_summary,
    build_lead_comment,
    build_lead_title,
    build_timeline_comment,
)
from .models import Application, CompanyEnrichment, ProcessResult


DEFAULT_LEAD_GENERATION_FIELD = "UF_CRM_1785917145255"
DEFAULT_LEAD_GENERATION_VALUE = "ГПО Недропользователя"
DEFAULT_ORIGINATOR_ID = "EQAZYNA_LEAD"
DEFAULT_COMPANY_ORIGINATOR_ID = "EQAZYNA"
DEFAULT_REQUISITE_PRESET_ID = "1"
DEFAULT_FAILURE_REASON_FIELD = "UF_CRM_1785508658316"
DEFAULT_MANAGER_IDS = (22, 23, 16, 17, 18, 38, 44, 39, 19, 15)


@dataclass(slots=True)
class LeadPipelineConfig:
    """Create and maintain a complete e-Qazyna CRM bundle.

    One e-Qazyna application number is represented by one lead. One BIN is
    represented by one company, one company requisite and, when a valid
    director name is available, one linked director contact. The responsible
    manager is inherited only from the director contact card. If that contact
    has no approved responsible manager, a manager is selected randomly among
    the least-loaded approved managers. A new lead inherits a failed stage and
    its reason only when the latest related lead ended unsuccessfully;
    otherwise it starts NEW.
    """

    lead_status_id: str = "NEW"
    assigned_by_id: str | None = None
    overwrite_assigned_by_on_update: bool = False
    lead_generation_field: str = DEFAULT_LEAD_GENERATION_FIELD
    lead_generation_value: str = DEFAULT_LEAD_GENERATION_VALUE
    originator_id: str = DEFAULT_ORIGINATOR_ID
    company_originator_id: str = DEFAULT_COMPANY_ORIGINATOR_ID
    requisite_preset_id: str | None = DEFAULT_REQUISITE_PRESET_ID
    source_id: str = "OTHER"
    source_description: str = "e-Qazyna Minerals. ГПО недропользователи"
    dry_run: bool = False
    validate_custom_field: bool = True
    manager_ids: tuple[int, ...] = DEFAULT_MANAGER_IDS
    failure_reason_field: str = DEFAULT_FAILURE_REASON_FIELD
    random_seed: int | None = None


@dataclass(slots=True)
class EntityOutcome:
    entity_id: str | None
    action: str
    record: dict[str, Any] | None = None
    warning: str | None = None
    address_action: str | None = None


class LeadPipeline:
    def __init__(self, client: BitrixClient, config: LeadPipelineConfig) -> None:
        self.client = client
        self.config = config
        self._lead_generation_encoded_value: str = config.lead_generation_value
        configured_preset = str(config.requisite_preset_id or "").strip()
        self._requisite_preset_id: int | None = (
            int(configured_preset) if configured_preset.isdigit() else None
        )
        self._requisite_bin_field = "RQ_BIN"
        self.validation_warnings: list[str] = []
        self._seen_application_numbers: set[str] = set()
        self._available_requisite_fields: set[str] = {
            "RQ_BIN",
            "RQ_COMPANY_NAME",
            "RQ_COMPANY_FULL_NAME",
            "RQ_DIRECTOR",
            "RQ_OKED",
        }
        self._manager_ids = tuple(
            dict.fromkeys(
                int(value)
                for value in config.manager_ids
                if str(value).strip().isdigit() and int(value) > 0
            )
        )
        self._manager_loads: dict[int, int] = {}
        # Entities created earlier in the same run must be reused even in dry_run.
        # This keeps the preview identical to apply and prevents one director
        # from receiving different managers across several applications.
        self._run_companies_by_bin: dict[str, dict[str, Any]] = {}
        self._run_contacts_by_key: dict[str, dict[str, Any]] = {}
        self._run_requisites_by_bin: dict[str, dict[str, Any]] = {}
        self._failed_status_ids: set[str] = {"JUNK"}
        self._terminal_status_ids: set[str] = {"JUNK", "CONVERTED"}
        self._random = (
            random.Random(config.random_seed)
            if config.random_seed is not None
            else random.SystemRandom()
        )

    # ---------- preflight ----------

    def validate(self) -> None:
        """Validate only what is required to start the parser.

        The original working parser validated the lead field and did not block
        processing on the requisite preset catalogue. On this box
        ``crm.requisite.preset.list`` returns an empty list although company
        requisites already exist and use PRESET_ID=1. Therefore the preset is
        resolved from existing company requisites, with a safe fallback, and a
        missing catalogue never stops lead creation.
        """
        if self.config.validate_custom_field:
            self._validate_lead_generation_field()

        try:
            requisite_fields = self.client.get_requisite_fields()
        except Exception as exc:  # noqa: BLE001 - non-blocking metadata lookup
            requisite_fields = {}
            self.validation_warnings.append(
                f"Не удалось прочитать поля реквизитов; будет использован стандартный RQ_BIN: {exc}"
            )

        if requisite_fields:
            self._available_requisite_fields = set(requisite_fields)
            if "RQ_BIN" in requisite_fields:
                self._requisite_bin_field = "RQ_BIN"
            elif "RQ_INN" in requisite_fields:
                self._requisite_bin_field = "RQ_INN"
            else:
                self.validation_warnings.append(
                    "В метаданных реквизитов не найден RQ_BIN/RQ_INN; используется RQ_BIN."
                )

        configured = self._configured_requisite_preset_id()
        discovered: int | None = None
        try:
            discovered = self.client.discover_company_requisite_preset_id()
        except Exception as exc:  # noqa: BLE001 - existing requisites are optional for preflight
            self.validation_warnings.append(
                f"Не удалось определить PRESET_ID по существующим компаниям: {exc}"
            )

        if discovered is not None:
            self._requisite_preset_id = discovered
            if configured is not None and configured != discovered:
                self.validation_warnings.append(
                    f"BITRIX_REQUISITE_PRESET_ID={configured} не совпадает с реально используемым "
                    f"в коробке PRESET_ID={discovered}; выбран PRESET_ID={discovered}."
                )
        elif configured is not None:
            self._requisite_preset_id = configured
        else:
            # The completed cloud-to-box migration created 618 company
            # requisites with PRESET_ID=1. Keep the same target format.
            self._requisite_preset_id = 1
            self.validation_warnings.append(
                "PRESET_ID не удалось определить автоматически; используется PRESET_ID=1, "
                "как у перенесённых реквизитов компаний."
            )

        self._load_lead_status_catalog()
        self._load_manager_workloads()

    def _load_lead_status_catalog(self) -> None:
        try:
            statuses = self.client.list_lead_statuses()
        except Exception as exc:  # noqa: BLE001 - safe fallbacks remain available
            self.validation_warnings.append(
                f"Не удалось прочитать статусы лидов; для неудачи используется JUNK: {exc}"
            )
            return

        failed: set[str] = set()
        terminal: set[str] = set()
        for row in statuses:
            status_id = str(row.get("STATUS_ID") or row.get("ID") or "").strip()
            semantics = str(row.get("SEMANTICS") or "").strip().upper()
            if not status_id:
                continue
            if semantics == "F":
                failed.add(status_id)
                terminal.add(status_id)
            elif semantics == "S":
                terminal.add(status_id)
        if failed:
            self._failed_status_ids = failed
        if terminal:
            self._terminal_status_ids = terminal

    def _load_manager_workloads(self) -> None:
        if not self._manager_ids:
            self.validation_warnings.append(
                "Список менеджеров распределения пуст; Bitrix24 назначит ответственного по умолчанию."
            )
            return
        loads: dict[int, int] = {}
        try:
            for manager_id in self._manager_ids:
                loads[manager_id] = self.client.count_open_leads_for_manager(
                    manager_id,
                    self._terminal_status_ids,
                )
        except Exception as exc:  # noqa: BLE001 - distribution still remains possible
            loads = {manager_id: 0 for manager_id in self._manager_ids}
            self.validation_warnings.append(
                "Не удалось получить текущую загрузку менеджеров; "
                f"первичное распределение будет случайным между всеми: {exc}"
            )
        self._manager_loads = loads

    def _validate_lead_generation_field(self) -> None:
        fields = self.client.get_lead_fields()
        field_meta = fields.get(self.config.lead_generation_field)
        if not isinstance(field_meta, dict):
            raise BitrixError(
                "Не найдено поле лида "
                f"{self.config.lead_generation_field}. Проверьте код поля в коробке."
            )

        field_type = str(
            field_meta.get("type")
            or field_meta.get("TYPE")
            or field_meta.get("userTypeId")
            or field_meta.get("USER_TYPE_ID")
            or ""
        ).strip().lower()
        if field_type in {"", "string", "text"}:
            self._lead_generation_encoded_value = self.config.lead_generation_value
            return
        if field_type not in {"enumeration", "list"}:
            raise BitrixError(
                f"Поле {self.config.lead_generation_field} имеет неподдерживаемый тип {field_type!r}."
            )

        options: list[dict[str, Any]] = []
        for key in ("items", "ITEMS", "list", "LIST", "values", "VALUES"):
            raw = field_meta.get(key)
            if isinstance(raw, list):
                options.extend(item for item in raw if isinstance(item, dict))
        wanted = self._normalise_label(self.config.lead_generation_value)
        matches: list[str] = []
        for item in options:
            label = item.get("VALUE") or item.get("value") or item.get("NAME") or item.get("name")
            option_id = item.get("ID") or item.get("id") or item.get("VALUE_ID") or item.get("valueId")
            if option_id not in (None, "") and self._normalise_label(label) == wanted:
                matches.append(str(option_id))
        matches = list(dict.fromkeys(matches))
        if len(matches) != 1:
            available = [
                str(item.get("VALUE") or item.get("value") or item.get("NAME") or item.get("name") or "")
                for item in options
            ]
            reason = "не найдено" if not matches else "найдено несколько совпадений"
            raise BitrixError(
                f"В списке {self.config.lead_generation_field} значение "
                f"{self.config.lead_generation_value!r} {reason}. Доступно: {available}"
            )
        self._lead_generation_encoded_value = matches[0]

    def _configured_requisite_preset_id(self) -> int | None:
        raw = str(self.config.requisite_preset_id or "").strip()
        if raw.casefold() in {"", "auto", "0", "none", "null"}:
            return None
        if not raw.isdigit() or int(raw) <= 0:
            self.validation_warnings.append(
                f"Некорректный BITRIX_REQUISITE_PRESET_ID={raw!r}; значение будет определено автоматически."
            )
            return None
        return int(raw)

    @property
    def requisite_preset_id(self) -> int | None:
        return self._requisite_preset_id

    @staticmethod
    def _normalise_label(value: Any) -> str:
        return " ".join(str(value or "").casefold().replace("ё", "е").split())

    # ---------- public processing ----------

    def process(self, app: Application, enrichment: CompanyEnrichment) -> ProcessResult:
        """Create one lead per application and shared company master data.

        Duplicate control is based on the exact e-Qazyna application number.
        A repeated application is reported and skipped without creating or
        changing CRM entities. Company, director contact and requisite remain
        shared by BIN.
        """
        warnings: list[str] = []
        company = EntityOutcome(None, "company_not_processed")
        contact = EntityOutcome(None, "contact_not_processed")
        requisite = EntityOutcome(None, "requisite_not_processed")
        reserved_manager_id: int | None = None

        doc_number = str(app.doc_number or "").strip()
        if not doc_number:
            return ProcessResult(
                app,
                enrichment,
                action="error",
                error="У заявки e-Qazyna отсутствует номер; дедупликация невозможна",
            )

        if doc_number in self._seen_application_numbers:
            return ProcessResult(
                app,
                enrichment,
                action="skipped_duplicate_application_in_run",
                warning=f"Заявка {doc_number} повторяется в текущей выборке и пропущена.",
            )
        self._seen_application_numbers.add(doc_number)

        try:
            duplicate = self.client.find_lead_by_application(
                doc_number,
                app.bin,
                originator_id=self.config.originator_id,
                extra_select=[
                    self.config.lead_generation_field,
                    self.config.failure_reason_field,
                ],
            )
            if duplicate:
                return ProcessResult(
                    app,
                    enrichment,
                    action="skipped_existing_application",
                    lead_id=str(duplicate.get("ID") or "") or None,
                    company_id=str(duplicate.get("COMPANY_ID") or "") or None,
                    contact_id=str(duplicate.get("CONTACT_ID") or "") or None,
                    assigned_by_id=self._record_assigned_by_id(duplicate),
                    assignment_reason="existing_application",
                    status_id=str(duplicate.get("STATUS_ID") or "") or None,
                    status_reason="existing_application",
                    failure_reason=self._record_failure_reason(duplicate),
                    warning=f"Заявка {doc_number} уже существует в CRM и не загружена повторно.",
                )

            existing_company = self._find_existing_company(app)
            existing_contact = self._find_existing_director_contact(
                app,
                enrichment,
                existing_company,
            )
            contact_reference_lead = self._find_contact_reference_lead(existing_contact)
            company_reference_lead = self._find_reference_lead(app, existing_company)

            inherited_assigned_by_id, assignment_reason = self._resolve_assignment(
                existing_contact,
            )
            reserved_manager_id = inherited_assigned_by_id

            lead_status_id, status_reason, failure_reason, status_reference_lead = (
                self._resolve_status(
                    contact_reference_lead,
                    company_reference_lead,
                )
            )
            force_entity_assignment = assignment_reason == "least_loaded_random"

            try:
                company = self._ensure_company(
                    app,
                    enrichment,
                    existing_company=existing_company,
                    preferred_assigned_by_id=inherited_assigned_by_id,
                    force_assigned_by=force_entity_assignment,
                )
                if company.record:
                    self._run_companies_by_bin[app.bin] = company.record
                if company.warning:
                    warnings.append(company.warning)
            except Exception as exc:  # noqa: BLE001 - lead must still be created
                company = EntityOutcome(None, "company_error", warning=f"Компания не сохранена: {exc}")
                warnings.append(company.warning)

            try:
                contact = self._ensure_director_contact(
                    app,
                    enrichment,
                    company.entity_id,
                    existing_contact=existing_contact,
                    preferred_assigned_by_id=inherited_assigned_by_id,
                    force_assigned_by=force_entity_assignment,
                )
                contact_key = self._director_cache_key(app, enrichment)
                if contact.record and contact_key:
                    self._run_contacts_by_key[contact_key] = contact.record
                if contact.warning:
                    warnings.append(contact.warning)
            except Exception as exc:  # noqa: BLE001 - lead must still be created
                contact = EntityOutcome(None, "contact_error", warning=f"Контакт руководителя не сохранён: {exc}")
                warnings.append(contact.warning)

            if enrichment.error:
                warnings.append(f"eGov: {enrichment.error}")

            lead_result = self._create_lead(
                app,
                enrichment,
                company.entity_id,
                contact.entity_id,
                assigned_by_id=inherited_assigned_by_id,
                assignment_reason=assignment_reason,
                status_id=lead_status_id,
                status_reason=status_reason,
                failure_reason=failure_reason,
                status_reference_lead_id=(
                    str((status_reference_lead or {}).get("ID") or "") or None
                ),
            )

            # Requisite belongs to the company, not to the lead. It is created
            # after the lead so a preset/address problem cannot erase the main
            # parser result.
            try:
                requisite = self._ensure_requisite(app, enrichment, company.entity_id)
                if requisite.record:
                    self._run_requisites_by_bin[app.bin] = requisite.record
                if requisite.warning:
                    warnings.append(requisite.warning)
            except Exception as exc:  # noqa: BLE001 - non-blocking by design
                requisite = EntityOutcome(
                    None,
                    "requisite_error",
                    warning=f"Реквизит компании не сохранён: {exc}",
                )
                warnings.append(requisite.warning)

            if lead_result.warning:
                warnings.append(lead_result.warning)
            lead_result.warning = self._join_warnings(warnings)
            lead_result.company_id = company.entity_id
            lead_result.contact_id = contact.entity_id
            lead_result.requisite_id = requisite.entity_id
            lead_result.company_action = company.action
            lead_result.contact_action = contact.action
            lead_result.requisite_action = requisite.action
            lead_result.address_action = requisite.address_action
            return lead_result
        except Exception as exc:  # noqa: BLE001 - report error per application
            if reserved_manager_id is not None:
                self._release_manager_load(reserved_manager_id)
            return ProcessResult(
                app,
                enrichment,
                action="error",
                company_id=company.entity_id,
                contact_id=contact.entity_id,
                requisite_id=requisite.entity_id,
                company_action=company.action,
                contact_action=contact.action,
                requisite_action=requisite.action,
                warning=self._join_warnings(warnings),
                error=str(exc),
            )

    def _find_existing_company(self, app: Application) -> dict[str, Any] | None:
        planned = self._run_companies_by_bin.get(app.bin)
        if planned:
            return planned
        company = self.client.find_company_by_origin(
            app.bin,
            originator_id=self.config.company_originator_id,
        )
        if company is None:
            company = self.client.find_company_by_bin(
                app.bin,
                bin_field=self._requisite_bin_field,
            )
        if company is not None:
            return company

        legacy_lead = self.client.find_latest_lead_by_bin(
            app.bin,
            extra_select=[
                self.config.lead_generation_field,
                self.config.failure_reason_field,
            ],
        )
        lead_company_id = str((legacy_lead or {}).get("COMPANY_ID") or "")
        if lead_company_id.isdigit():
            return self.client.get_company(lead_company_id)
        return None

    def _find_existing_director_contact(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        cache_key = self._director_cache_key(app, enrichment)
        if cache_key and cache_key in self._run_contacts_by_key:
            return self._run_contacts_by_key[cache_key]
        company_id = str((company or {}).get("ID") or "")
        person = self._split_director_name(enrichment.director)
        if not company_id.isdigit() or person is None:
            return None
        last_name, name, second_name = person
        return self.client.find_director_contact(
            company_id,
            last_name,
            name,
            second_name,
        )

    def _director_cache_key(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
    ) -> str | None:
        person = self._split_director_name(enrichment.director)
        if person is None:
            return None
        normalized = "|".join(self._normalise_label(value) for value in person)
        return f"{app.bin}|{normalized}"

    def _find_contact_reference_lead(
        self,
        contact: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        contact_id = str((contact or {}).get("ID") or "")
        if not contact_id.isdigit():
            return None
        return self.client.find_latest_lead_for_contact(
            contact_id,
            extra_select=[
                self.config.lead_generation_field,
                self.config.failure_reason_field,
            ],
        )

    def _find_reference_lead(
        self,
        app: Application,
        company: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        company_id = str((company or {}).get("ID") or "")
        if company_id.isdigit():
            lead = self.client.find_latest_lead_for_company(
                company_id,
                extra_select=[
                    self.config.lead_generation_field,
                    self.config.failure_reason_field,
                ],
            )
            if lead:
                return lead
        return self.client.find_latest_lead_by_bin(
            app.bin,
            extra_select=[
                self.config.lead_generation_field,
                self.config.failure_reason_field,
            ],
        )

    def _resolve_assignment(
        self,
        contact: dict[str, Any] | None,
    ) -> tuple[int | None, str | None]:
        """Resolve the lead owner from one unambiguous source.

        If the director contact already exists, its ``ASSIGNED_BY_ID`` is the
        only historical assignment considered. Lead history and company owner
        are deliberately ignored, so conflicting owners cannot compete. When
        the contact has no approved owner (or is being created now), the lead
        bundle is distributed randomly among the least-loaded approved
        managers.
        """
        manager_id = self._approved_record_assigned_by_id(contact or {})
        if manager_id is not None:
            self._reserve_manager_load(manager_id)
            return manager_id, "director_contact_owner"

        if self._manager_ids:
            return self._select_least_loaded_manager(), "least_loaded_random"

        configured = self._configured_assigned_by_id()
        if configured:
            return configured, "configured_default_no_manager_pool"
        return None, None

    def _resolve_status(
        self,
        contact_reference_lead: dict[str, Any] | None,
        company_reference_lead: dict[str, Any] | None,
    ) -> tuple[str, str, str | None, dict[str, Any] | None]:
        reference = self._newest_lead(contact_reference_lead, company_reference_lead)
        if reference and self._is_failed_lead(reference):
            status_id = str(reference.get("STATUS_ID") or "").strip()
            if status_id:
                return (
                    status_id,
                    "failed_related_lead_inherited",
                    self._record_failure_reason(reference),
                    reference,
                )
        return (
            str(self.config.lead_status_id or "NEW"),
            "default_new",
            None,
            reference,
        )

    def _is_failed_lead(self, lead: dict[str, Any]) -> bool:
        semantic = str(lead.get("STATUS_SEMANTIC_ID") or "").strip().upper()
        if semantic == "F":
            return True
        return str(lead.get("STATUS_ID") or "").strip() in self._failed_status_ids

    def _newest_lead(
        self,
        *leads: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        valid = [lead for lead in leads if isinstance(lead, dict) and lead]
        if not valid:
            return None
        return max(valid, key=self._lead_sort_key)

    @staticmethod
    def _lead_sort_key(lead: dict[str, Any]) -> tuple[float, int]:
        raw = str(lead.get("DATE_MODIFY") or lead.get("DATE_CREATE") or "").strip()
        timestamp = 0.0
        if raw:
            try:
                timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                timestamp = 0.0
        raw_id = str(lead.get("ID") or "").strip()
        return timestamp, int(raw_id) if raw_id.isdigit() else 0

    def _select_least_loaded_manager(self) -> int:
        if not self._manager_loads:
            self._manager_loads = {manager_id: 0 for manager_id in self._manager_ids}
        minimum = min(self._manager_loads.values())
        least_loaded = sorted(
            manager_id
            for manager_id, load in self._manager_loads.items()
            if load == minimum
        )
        selected = int(self._random.choice(least_loaded))
        self._reserve_manager_load(selected)
        return selected

    def _reserve_manager_load(self, manager_id: int) -> None:
        if manager_id in self._manager_ids:
            self._manager_loads[manager_id] = self._manager_loads.get(manager_id, 0) + 1

    def _release_manager_load(self, manager_id: int) -> None:
        if manager_id in self._manager_loads and self._manager_loads[manager_id] > 0:
            self._manager_loads[manager_id] -= 1

    def _approved_record_assigned_by_id(self, record: dict[str, Any]) -> int | None:
        manager_id = self._record_assigned_by_id(record)
        if manager_id is None:
            return None
        if self._manager_ids and manager_id not in self._manager_ids:
            return None
        return manager_id

    def _record_failure_reason(self, record: dict[str, Any]) -> str | None:
        for field_name in (self.config.failure_reason_field, "STATUS_DESCRIPTION"):
            value = str(record.get(field_name) or "").strip()
            if value:
                return value
        return None

    # ---------- company ----------

    def _ensure_company(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        *,
        existing_company: dict[str, Any] | None,
        preferred_assigned_by_id: int | None,
        force_assigned_by: bool = False,
    ) -> EntityOutcome:
        company = existing_company
        desired = self._company_fields(app, enrichment, company)
        current_owner = self._record_assigned_by_id(company or {})
        if preferred_assigned_by_id and (
            company is None
            or current_owner is None
            or force_assigned_by
        ):
            desired["ASSIGNED_BY_ID"] = preferred_assigned_by_id

        if company:
            company_id = str(company.get("ID") or "")
            if not company_id:
                raise BitrixError("crm.company.list вернул компанию без ID")
            if company.get("_EQAZYNA_PLANNED"):
                return EntityOutcome(company_id, "dry_run_reuse_planned_company", company)
            changed = self._only_changed_fields(company, desired)
            if self.config.dry_run:
                return EntityOutcome(
                    company_id,
                    "dry_run_update_company" if changed else "company_unchanged",
                    company,
                )
            if changed:
                self.client.update_company(company_id, changed)
                company = {**company, **changed}
                return EntityOutcome(company_id, "updated_company", company)
            return EntityOutcome(company_id, "company_unchanged", company)

        if self.config.dry_run:
            company_id = f"DRY_RUN_COMPANY:{app.bin}"
            record = {"ID": company_id, "_EQAZYNA_PLANNED": True, **desired}
            return EntityOutcome(company_id, "dry_run_create_company", record)

        company_id = self.client.create_company(desired)
        return EntityOutcome(company_id, "created_company", {"ID": company_id, **desired})

    def _company_fields(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        current: dict[str, Any] | None,
    ) -> dict[str, object]:
        short_name = short_organization_name(enrichment.name or app.applicant_name)
        current_comments = str((current or {}).get("COMMENTS") or "")
        comments = self._append_marked_comment_once(
            current_comments,
            f"EQAZYNA_MASTER_DATA:{app.bin}",
            build_company_summary(app, enrichment),
        )
        fields: dict[str, object] = {
            "TITLE": short_name,
            "OPENED": "Y",
            "COMMENTS": comments,
            "ORIGINATOR_ID": self.config.company_originator_id,
            "ORIGIN_ID": app.bin,
        }
        phone = self._merge_multifield((current or {}).get("PHONE"), enrichment.phone)
        if phone:
            fields["PHONE"] = phone
        if enrichment.legal_address:
            fields["ADDRESS"] = enrichment.legal_address
        if enrichment.city:
            fields["ADDRESS_CITY"] = enrichment.city
        if enrichment.region:
            fields["ADDRESS_REGION"] = enrichment.region
            fields["ADDRESS_PROVINCE"] = enrichment.region
        fields["ADDRESS_COUNTRY"] = "Казахстан"
        return fields

    # ---------- requisite and address ----------

    def _ensure_requisite(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str | None,
    ) -> EntityOutcome:
        planned = self._run_requisites_by_bin.get(app.bin)
        if planned:
            return EntityOutcome(
                str(planned.get("ID") or "") or None,
                "dry_run_reuse_planned_requisite" if self.config.dry_run else "reused_run_requisite",
                planned,
                address_action="address_reused_in_run",
            )
        if not company_id:
            return EntityOutcome(None, "requisite_skipped", warning="Реквизит не создан: отсутствует компания")
        if company_id.startswith("DRY_RUN_COMPANY"):
            requisite_id = f"DRY_RUN_REQUISITE:{app.bin}"
            record = {"ID": requisite_id, "_EQAZYNA_PLANNED": True}
            return EntityOutcome(
                requisite_id,
                "dry_run_create_requisite",
                record,
                address_action=(
                    "dry_run_create_address" if enrichment.legal_address else "address_skipped_no_value"
                ),
            )

        requisite = self.client.find_company_requisite(
            company_id,
            app.bin,
            bin_field=self._requisite_bin_field,
        )
        desired = self._requisite_fields(app, enrichment, company_id)
        if requisite:
            requisite_id = str(requisite.get("ID") or "")
            if not requisite_id:
                raise BitrixError("crm.requisite.list вернул реквизит без ID")
            changed = self._only_changed_fields(requisite, desired)
            if self.config.dry_run:
                action = "dry_run_update_requisite" if changed else "requisite_unchanged"
            else:
                if changed:
                    self.client.update_requisite(requisite_id, changed)
                    action = "updated_requisite"
                else:
                    action = "requisite_unchanged"
        else:
            if self.config.dry_run:
                requisite_id = "DRY_RUN_REQUISITE"
                action = "dry_run_create_requisite"
            else:
                requisite_id = self.client.create_requisite(desired)
                action = "created_requisite"

        address_action: str | None = None
        warning: str | None = None
        if enrichment.legal_address:
            try:
                address_action = self._ensure_requisite_address(requisite_id, enrichment)
            except Exception as exc:  # noqa: BLE001 - requisite remains valid
                warning = f"Адрес реквизита не сохранён: {exc}"
        else:
            address_action = "address_skipped_no_value"

        return EntityOutcome(
            requisite_id,
            action,
            requisite,
            warning=warning,
            address_action=address_action,
        )

    def _requisite_fields(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str,
    ) -> dict[str, object]:
        preset_id = int(self._requisite_preset_id or 1)
        full_name = str(enrichment.name or app.applicant_name or "").strip()
        short_name = short_organization_name(full_name)
        fields: dict[str, object] = {
            "ENTITY_TYPE_ID": 4,
            "ENTITY_ID": int(company_id),
            "PRESET_ID": preset_id,
            "NAME": f"БИН {app.bin}. {full_name or short_name}",
            "ACTIVE": "Y",
            "ADDRESS_ONLY": "N",
            "SORT": 500,
            "XML_ID": f"EQAZYNA-REQ-{app.bin}",
            "ORIGINATOR_ID": self.config.company_originator_id,
            self._requisite_bin_field: app.bin,
        }
        optional = {
            "RQ_COMPANY_NAME": short_name,
            "RQ_COMPANY_FULL_NAME": full_name,
            "RQ_DIRECTOR": enrichment.director,
            "RQ_OKED": enrichment.oked,
        }
        for field_name, value in optional.items():
            if value and field_name in self._available_requisite_fields:
                fields[field_name] = value
        return fields

    def _ensure_requisite_address(
        self,
        requisite_id: str,
        enrichment: CompanyEnrichment,
    ) -> str:
        if requisite_id == "DRY_RUN_REQUISITE":
            return "dry_run_create_address"
        existing = self.client.find_requisite_address(requisite_id, 1)
        fields: dict[str, object] = {
            "ENTITY_TYPE_ID": 8,
            "ENTITY_ID": int(requisite_id),
            "TYPE_ID": 1,
            "ADDRESS_1": enrichment.legal_address or "",
            "CITY": enrichment.city or "",
            "REGION": enrichment.region or "",
            "PROVINCE": enrichment.region or "",
            "COUNTRY": "Казахстан",
            "COUNTRY_CODE": "KZ",
        }
        if self.config.dry_run:
            return "dry_run_update_address" if existing else "dry_run_create_address"
        if existing:
            self.client.update_address(fields)
            return "updated_address"
        self.client.create_address(fields)
        return "created_address"

    # ---------- director contact ----------

    def _ensure_director_contact(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str | None,
        *,
        existing_contact: dict[str, Any] | None = None,
        preferred_assigned_by_id: int | None = None,
        force_assigned_by: bool = False,
    ) -> EntityOutcome:
        person = self._split_director_name(enrichment.director)
        if person is None:
            return EntityOutcome(
                None,
                "contact_skipped_no_valid_fio",
                warning=(
                    "Контакт руководителя не создан: eGov не вернул полноценное ФИО"
                    if enrichment.director
                    else None
                ),
            )
        if existing_contact and existing_contact.get("_EQAZYNA_PLANNED"):
            contact_id = str(existing_contact.get("ID") or "")
            return EntityOutcome(contact_id or None, "dry_run_reuse_planned_contact", existing_contact)
        if not company_id:
            return EntityOutcome(None, "contact_skipped", warning="Контакт не создан: отсутствует компания")
        if company_id.startswith("DRY_RUN_COMPANY"):
            desired = self._contact_fields(
                app,
                enrichment,
                "0",
                person,
                None,
            )
            desired["COMPANY_ID"] = company_id
            if preferred_assigned_by_id:
                desired["ASSIGNED_BY_ID"] = preferred_assigned_by_id
            contact_key = self._director_cache_key(app, enrichment) or app.bin
            contact_id = f"DRY_RUN_CONTACT:{contact_key}"
            record = {"ID": contact_id, "_EQAZYNA_PLANNED": True, **desired}
            return EntityOutcome(contact_id, "dry_run_create_contact", record)

        last_name, name, second_name = person
        contact = existing_contact or self.client.find_director_contact(
            company_id,
            last_name,
            name,
            second_name,
        )
        desired = self._contact_fields(app, enrichment, company_id, person, contact)
        current_owner = self._record_assigned_by_id(contact or {})
        if preferred_assigned_by_id and (
            contact is None
            or current_owner is None
            or force_assigned_by
        ):
            desired["ASSIGNED_BY_ID"] = preferred_assigned_by_id

        if contact:
            contact_id = str(contact.get("ID") or "")
            if not contact_id:
                raise BitrixError("crm.contact.list вернул контакт без ID")
            if contact.get("_EQAZYNA_PLANNED"):
                return EntityOutcome(contact_id, "dry_run_reuse_planned_contact", contact)
            changed = self._only_changed_fields(contact, desired)
            if self.config.dry_run:
                return EntityOutcome(
                    contact_id,
                    "dry_run_update_contact" if changed else "contact_unchanged",
                    contact,
                )
            if changed:
                self.client.update_contact(contact_id, changed)
                return EntityOutcome(contact_id, "updated_contact", {**contact, **changed})
            return EntityOutcome(contact_id, "contact_unchanged", contact)

        if self.config.dry_run:
            contact_key = self._director_cache_key(app, enrichment) or app.bin
            contact_id = f"DRY_RUN_CONTACT:{contact_key}"
            record = {"ID": contact_id, "_EQAZYNA_PLANNED": True, **desired}
            return EntityOutcome(contact_id, "dry_run_create_contact", record)
        contact_id = self.client.create_contact(desired)
        return EntityOutcome(contact_id, "created_contact", {"ID": contact_id, **desired})

    def _contact_fields(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str,
        person: tuple[str, str, str],
        current: dict[str, Any] | None,
    ) -> dict[str, object]:
        last_name, name, second_name = person
        current_comments = str((current or {}).get("COMMENTS") or "")
        contact_comment = "\n".join(
            [
                "Руководитель организации из data.egov.kz",
                f"Исходное ФИО: {enrichment.director}",
                f"БИН компании: {app.bin}",
                "Источник: e-Qazyna / eGov",
            ]
        )
        comments = self._append_marked_comment_once(
            current_comments,
            f"EQAZYNA_DIRECTOR:{app.bin}",
            contact_comment,
        )
        fields: dict[str, object] = {
            "LAST_NAME": last_name,
            "NAME": name,
            "POST": "Руководитель",
            "COMPANY_ID": int(company_id),
            "OPENED": "Y",
            "COMMENTS": comments,
        }
        if second_name:
            fields["SECOND_NAME"] = second_name
        return fields

    # ---------- lead ----------

    def _create_lead(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str | None,
        contact_id: str | None,
        *,
        assigned_by_id: int | None,
        assignment_reason: str | None,
        status_id: str,
        status_reason: str,
        failure_reason: str | None,
        status_reference_lead_id: str | None,
    ) -> ProcessResult:
        fields = self._create_fields(
            app,
            enrichment,
            company_id,
            contact_id,
            assigned_by_id=assigned_by_id,
            status_id=status_id,
            failure_reason=failure_reason,
        )

        if self.config.dry_run:
            return ProcessResult(
                app,
                enrichment,
                action="dry_run_create_lead",
                lead_id="DRY_RUN_LEAD",
                assigned_by_id=assigned_by_id,
                assignment_reason=assignment_reason,
                status_id=status_id,
                status_reason=status_reason,
                failure_reason=failure_reason,
                status_reference_lead_id=status_reference_lead_id,
            )

        lead_id = self.client.create_lead(fields)
        warning = self._safe_add_timeline_comment(lead_id, app, enrichment)
        return ProcessResult(
            app,
            enrichment,
            action="created_lead",
            lead_id=lead_id,
            assigned_by_id=assigned_by_id,
            assignment_reason=assignment_reason,
            status_id=status_id,
            status_reason=status_reason,
            failure_reason=failure_reason,
            status_reference_lead_id=status_reference_lead_id,
            warning=warning,
        )

    def _create_fields(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str | None,
        contact_id: str | None,
        *,
        assigned_by_id: int | None,
        status_id: str,
        failure_reason: str | None,
    ) -> dict[str, object]:
        fields: dict[str, object] = {
            "TITLE": build_lead_title(app, enrichment),
            "COMPANY_TITLE": short_organization_name(enrichment.name or app.applicant_name),
            "STATUS_ID": status_id or "NEW",
            "OPENED": "Y",
            "COMMENTS": build_lead_comment(app, enrichment),
            "ORIGINATOR_ID": self.config.originator_id,
            "ORIGIN_ID": app.doc_number,
            "SOURCE_ID": self.config.source_id,
            "SOURCE_DESCRIPTION": self.config.source_description,
            self.config.lead_generation_field: self._lead_generation_encoded_value,
        }
        if failure_reason:
            fields[self.config.failure_reason_field] = failure_reason
        self._add_lead_master_links(fields, enrichment, company_id, contact_id)
        if assigned_by_id:
            fields["ASSIGNED_BY_ID"] = assigned_by_id
        return fields

    def _add_lead_master_links(
        self,
        fields: dict[str, object],
        enrichment: CompanyEnrichment,
        company_id: str | None,
        contact_id: str | None,
        current: dict[str, Any] | None = None,
    ) -> None:
        if company_id and company_id.isdigit():
            fields["COMPANY_ID"] = int(company_id)
        if contact_id and contact_id.isdigit():
            fields["CONTACT_ID"] = int(contact_id)
        person = self._split_director_name(enrichment.director)
        if person:
            last_name, name, second_name = person
            fields["LAST_NAME"] = last_name
            fields["NAME"] = name
            if second_name:
                fields["SECOND_NAME"] = second_name
        phone = self._merge_multifield((current or {}).get("PHONE"), enrichment.phone)
        if phone:
            fields["PHONE"] = phone
        if enrichment.legal_address:
            fields["ADDRESS"] = enrichment.legal_address
        if enrichment.city:
            fields["ADDRESS_CITY"] = enrichment.city
        if enrichment.region:
            fields["ADDRESS_REGION"] = enrichment.region

    # ---------- generic helpers ----------

    def _only_changed_fields(
        self,
        current: dict[str, Any],
        desired: dict[str, object],
    ) -> dict[str, object]:
        changed: dict[str, object] = {}
        for field_name, value in desired.items():
            current_value = current.get(field_name)
            if field_name in {"PHONE", "EMAIL", "WEB", "IM"}:
                if self._normalise_multifield(current_value) != self._normalise_multifield(value):
                    changed[field_name] = value
                continue
            if str(current_value or "") != str(value or ""):
                changed[field_name] = value
        return changed

    @staticmethod
    def _normalise_multifield(value: Any) -> list[tuple[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[tuple[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            field_value = str(item.get("VALUE") or "").strip()
            value_type = str(item.get("VALUE_TYPE") or "WORK").strip().upper()
            if field_value:
                result.append((field_value, value_type))
        return sorted(set(result))

    def _merge_multifield(
        self,
        current: Any,
        new_value: str | None,
    ) -> list[dict[str, str]] | None:
        values = self._normalise_multifield(current)
        candidate = str(new_value or "").strip()
        if candidate and all(value != candidate for value, _ in values):
            values.append((candidate, "WORK"))
        if not values:
            return None
        return [
            {"VALUE": value, "VALUE_TYPE": value_type or "WORK"}
            for value, value_type in sorted(set(values))
        ]

    @staticmethod
    def _append_marked_comment_once(
        current: str,
        marker: str,
        block: str,
    ) -> str:
        marker_line = f"[[{marker}]]"
        current = str(current or "").strip()
        if marker_line in current:
            return current[:65000]
        addition = f"{block.rstrip()}\n{marker_line}"
        if current:
            return f"{current}\n\n{addition}"[:65000]
        return addition[:65000]

    @staticmethod
    def _split_director_name(value: str | None) -> tuple[str, str, str] | None:
        raw = re.sub(r"\s+", " ", str(value or "")).strip(" .,-")
        if not raw:
            return None
        lowered = raw.casefold().replace("ё", "е")
        invalid = {
            "не найден",
            "не найдено",
            "нет данных",
            "без имени",
            "руководитель",
            "директор",
            "null",
            "none",
        }
        if lowered in invalid:
            return None
        parts = [part for part in raw.split(" ") if part]
        if len(parts) < 2:
            return None
        return parts[0], parts[1], " ".join(parts[2:])

    def _safe_add_timeline_comment(
        self,
        lead_id: str,
        app: Application,
        enrichment: CompanyEnrichment,
    ) -> str | None:
        try:
            self.client.add_timeline_comment("lead", lead_id, build_timeline_comment(app, enrichment))
            return None
        except Exception as exc:  # noqa: BLE001 - lead write remains successful
            return f"Лид сохранён, но комментарий таймлайна не добавлен: {exc}"

    def _configured_assigned_by_id(self) -> int | None:
        if self.config.assigned_by_id in (None, ""):
            return None
        try:
            return int(self.config.assigned_by_id)
        except (TypeError, ValueError) as exc:
            raise BitrixError(
                f"BITRIX_ASSIGNED_BY_ID должен быть числовым ID, получено: {self.config.assigned_by_id!r}"
            ) from exc

    @staticmethod
    def _record_assigned_by_id(record: dict[str, Any]) -> int | None:
        value = record.get("ASSIGNED_BY_ID")
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _join_warnings(values: list[str]) -> str | None:
        unique: list[str] = []
        for value in values:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in unique:
                unique.append(cleaned)
        return " | ".join(unique) if unique else None
