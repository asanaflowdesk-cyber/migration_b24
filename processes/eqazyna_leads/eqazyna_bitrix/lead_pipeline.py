from __future__ import annotations

import re
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


@dataclass(slots=True)
class LeadPipelineConfig:
    """Create and maintain a complete e-Qazyna CRM bundle.

    One BIN is represented by one lead, one company, one company requisite and,
    when a valid director name is available, one linked director contact.
    Existing lead stage and responsible user are preserved on updates.
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
        self._available_requisite_fields: set[str] = {
            "RQ_BIN",
            "RQ_COMPANY_NAME",
            "RQ_COMPANY_FULL_NAME",
            "RQ_DIRECTOR",
            "RQ_OKED",
        }

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
        """Create/update the lead first-class bundle without optional blockers.

        Company, director contact and requisite are attempted independently. A
        failure of the requisite/address API is recorded as a warning and does
        not cancel a correctly created lead.
        """
        warnings: list[str] = []
        lead: dict[str, Any] | None = None
        company = EntityOutcome(None, "company_not_processed")
        contact = EntityOutcome(None, "contact_not_processed")
        requisite = EntityOutcome(None, "requisite_not_processed")

        try:
            lead = self.client.find_lead_by_origin(
                app.bin,
                originator_id=self.config.originator_id,
                extra_select=[self.config.lead_generation_field],
            )

            try:
                company = self._ensure_company(app, enrichment, lead)
                if company.warning:
                    warnings.append(company.warning)
            except Exception as exc:  # noqa: BLE001 - lead must still be created
                company = EntityOutcome(None, "company_error", warning=f"Компания не сохранена: {exc}")
                warnings.append(company.warning)

            try:
                contact = self._ensure_director_contact(app, enrichment, company.entity_id)
                if contact.warning:
                    warnings.append(contact.warning)
            except Exception as exc:  # noqa: BLE001 - lead must still be created
                contact = EntityOutcome(None, "contact_error", warning=f"Контакт руководителя не сохранён: {exc}")
                warnings.append(contact.warning)

            if enrichment.error:
                warnings.append(f"eGov: {enrichment.error}")

            if lead:
                lead_result = self._update_existing_lead(
                    lead, app, enrichment, company.entity_id, contact.entity_id
                )
            else:
                lead_result = self._create_lead(
                    app, enrichment, company.entity_id, contact.entity_id
                )

            # Requisite belongs to the company, not to the lead. It is created
            # after the lead so a preset/address problem cannot erase the main
            # parser result.
            try:
                requisite = self._ensure_requisite(app, enrichment, company.entity_id)
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

    # ---------- company ----------

    def _ensure_company(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        lead: dict[str, Any] | None,
    ) -> EntityOutcome:
        company: dict[str, Any] | None = self.client.find_company_by_origin(
            app.bin,
            originator_id=self.config.company_originator_id,
        )
        if company is None:
            company = self.client.find_company_by_bin(
                app.bin,
                bin_field=self._requisite_bin_field,
            )
        if company is None:
            lead_company_id = str((lead or {}).get("COMPANY_ID") or "")
            if lead_company_id.isdigit():
                company = self.client.get_company(lead_company_id)

        desired = self._company_fields(app, enrichment, company)
        assigned_by_id = self._configured_assigned_by_id()
        if assigned_by_id and company is None:
            desired["ASSIGNED_BY_ID"] = assigned_by_id

        if company:
            company_id = str(company.get("ID") or "")
            if not company_id:
                raise BitrixError("crm.company.list вернул компанию без ID")
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
            return EntityOutcome("DRY_RUN_COMPANY", "dry_run_create_company", desired)

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
        if not company_id:
            return EntityOutcome(None, "requisite_skipped", warning="Реквизит не создан: отсутствует компания")
        if company_id == "DRY_RUN_COMPANY":
            return EntityOutcome(
                "DRY_RUN_REQUISITE",
                "dry_run_create_requisite",
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
        if not company_id:
            return EntityOutcome(None, "contact_skipped", warning="Контакт не создан: отсутствует компания")
        if company_id == "DRY_RUN_COMPANY":
            return EntityOutcome("DRY_RUN_CONTACT", "dry_run_create_contact")

        last_name, name, second_name = person
        contact = self.client.find_director_contact(
            company_id,
            last_name,
            name,
            second_name,
        )
        desired = self._contact_fields(app, enrichment, company_id, person, contact)
        assigned_by_id = self._configured_assigned_by_id()
        if assigned_by_id and contact is None:
            desired["ASSIGNED_BY_ID"] = assigned_by_id

        if contact:
            contact_id = str(contact.get("ID") or "")
            if not contact_id:
                raise BitrixError("crm.contact.list вернул контакт без ID")
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
            return EntityOutcome("DRY_RUN_CONTACT", "dry_run_create_contact", desired)
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
    ) -> ProcessResult:
        fields = self._create_fields(app, enrichment, company_id, contact_id)
        assigned_by_id = self._configured_assigned_by_id()

        if self.config.dry_run:
            return ProcessResult(
                app,
                enrichment,
                action="dry_run_create_lead",
                lead_id="DRY_RUN_LEAD",
                assigned_by_id=assigned_by_id,
                assignment_reason="configured_on_create" if assigned_by_id else None,
            )

        lead_id = self.client.create_lead(fields)
        warning = self._safe_add_timeline_comment(lead_id, app, enrichment)
        return ProcessResult(
            app,
            enrichment,
            action="created_lead",
            lead_id=lead_id,
            assigned_by_id=assigned_by_id,
            assignment_reason="configured_on_create" if assigned_by_id else None,
            warning=warning,
        )

    def _update_existing_lead(
        self,
        lead: dict[str, Any],
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str | None,
        contact_id: str | None,
    ) -> ProcessResult:
        lead_id = str(lead.get("ID") or "")
        if not lead_id:
            raise BitrixError("crm.lead.list вернул лид без ID")

        existing_comments = str(lead.get("COMMENTS") or "")
        new_application = app.application_key not in existing_comments
        fields = self._update_fields(
            app,
            enrichment,
            existing_comments,
            company_id,
            contact_id,
            lead,
        )

        if (
            str(lead.get("ORIGINATOR_ID") or "") != self.config.originator_id
            or str(lead.get("ORIGIN_ID") or "") != app.bin
        ):
            fields["ORIGINATOR_ID"] = self.config.originator_id
            fields["ORIGIN_ID"] = app.bin

        changed_fields = self._only_changed_fields(lead, fields)
        assigned_by_id = self._record_assigned_by_id(lead)

        if self.config.dry_run:
            action = "dry_run_update_lead" if changed_fields else "dry_run_existing_lead_unchanged"
            return ProcessResult(
                app,
                enrichment,
                action=action,
                lead_id=lead_id,
                assigned_by_id=assigned_by_id,
                assignment_reason="existing_lead_owner_preserved",
            )

        if changed_fields:
            self.client.update_lead(lead_id, changed_fields)

        warning = None
        if new_application:
            warning = self._safe_add_timeline_comment(lead_id, app, enrichment)

        if new_application:
            action = "existing_lead_new_application_added"
        elif changed_fields:
            action = "existing_lead_backfilled"
        else:
            action = "existing_lead_unchanged"

        return ProcessResult(
            app,
            enrichment,
            action=action,
            lead_id=lead_id,
            assigned_by_id=assigned_by_id,
            assignment_reason="existing_lead_owner_preserved",
            warning=warning,
        )

    def _create_fields(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        company_id: str | None,
        contact_id: str | None,
    ) -> dict[str, object]:
        fields: dict[str, object] = {
            "TITLE": build_lead_title(app, enrichment),
            "COMPANY_TITLE": short_organization_name(enrichment.name or app.applicant_name),
            "STATUS_ID": self.config.lead_status_id or "NEW",
            "OPENED": "Y",
            "COMMENTS": build_lead_comment(app, enrichment),
            "ORIGINATOR_ID": self.config.originator_id,
            "ORIGIN_ID": app.bin,
            "SOURCE_ID": self.config.source_id,
            "SOURCE_DESCRIPTION": self.config.source_description,
            self.config.lead_generation_field: self._lead_generation_encoded_value,
        }
        self._add_lead_master_links(fields, enrichment, company_id, contact_id)
        assigned_by_id = self._configured_assigned_by_id()
        if assigned_by_id:
            fields["ASSIGNED_BY_ID"] = assigned_by_id
        return fields

    def _update_fields(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        existing_comments: str,
        company_id: str | None,
        contact_id: str | None,
        current: dict[str, Any],
    ) -> dict[str, object]:
        # Existing status and responsible are deliberately preserved.
        fields: dict[str, object] = {
            "TITLE": build_lead_title(app, enrichment),
            "COMPANY_TITLE": short_organization_name(enrichment.name or app.applicant_name),
            "COMMENTS": build_lead_comment(app, enrichment, existing_comments),
            self.config.lead_generation_field: self._lead_generation_encoded_value,
        }
        self._add_lead_master_links(fields, enrichment, company_id, contact_id, current)
        if self.config.overwrite_assigned_by_on_update:
            assigned_by_id = self._configured_assigned_by_id()
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
