from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .bitrix_client import BitrixClient, BitrixError
from .formatter import build_lead_comment, build_lead_title, build_timeline_comment
from .models import Application, CompanyEnrichment, ProcessResult


DEFAULT_LEAD_GENERATION_FIELD = "UF_CRM_1785917145255"
DEFAULT_LEAD_GENERATION_VALUE = "ГПО недропользователя"
DEFAULT_ORIGINATOR_ID = "EQAZYNA_LEAD"


@dataclass(slots=True)
class LeadPipelineConfig:
    """Configuration for the lead-only e-Qazyna integration.

    The parser intentionally does not create companies, contacts, requisites or
    deals. One lead is maintained per BIN and each new e-Qazyna application is
    appended to the lead history.
    """

    lead_status_id: str = "NEW"
    assigned_by_id: str | None = None
    overwrite_assigned_by_on_update: bool = False
    lead_generation_field: str = DEFAULT_LEAD_GENERATION_FIELD
    lead_generation_value: str = DEFAULT_LEAD_GENERATION_VALUE
    originator_id: str = DEFAULT_ORIGINATOR_ID
    source_id: str = "OTHER"
    source_description: str = "e-Qazyna Minerals — ГПО недропользователи"
    dry_run: bool = False
    validate_custom_field: bool = True


class LeadPipeline:
    def __init__(self, client: BitrixClient, config: LeadPipelineConfig) -> None:
        self.client = client
        self.config = config

    def validate(self) -> None:
        """Fail before processing if the target custom field is unavailable.

        Bitrix silently ignores unknown fields in some update scenarios. A
        preflight check is therefore safer than discovering the problem after a
        successful-looking run.
        """
        if not self.config.validate_custom_field:
            return

        fields = self.client.get_lead_fields()
        field_meta = fields.get(self.config.lead_generation_field)
        if not isinstance(field_meta, dict):
            raise BitrixError(
                "Не найдено поле лида "
                f"{self.config.lead_generation_field}. Проверьте код поля в коробке."
            )

        field_type = str(field_meta.get("type") or field_meta.get("TYPE") or "").strip().lower()
        if field_type and field_type not in {"string", "text"}:
            raise BitrixError(
                f"Поле {self.config.lead_generation_field} имеет тип {field_type!r}, "
                "а интеграция ожидает текстовое поле."
            )

    def process(self, app: Application, enrichment: CompanyEnrichment) -> ProcessResult:
        try:
            lead = self.client.find_lead_by_origin(
                app.bin,
                originator_id=self.config.originator_id,
                extra_select=[self.config.lead_generation_field],
            )

            if lead:
                return self._update_existing_lead(lead, app, enrichment)
            return self._create_lead(app, enrichment)
        except Exception as exc:  # noqa: BLE001 - log error per application
            return ProcessResult(app, enrichment, action="error", error=str(exc))

    def _create_lead(self, app: Application, enrichment: CompanyEnrichment) -> ProcessResult:
        fields = self._create_fields(app, enrichment)
        assigned_by_id = self._configured_assigned_by_id()

        if self.config.dry_run:
            return ProcessResult(
                app,
                enrichment,
                action="dry_run_create_lead",
                lead_id="DRY_RUN",
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
    ) -> ProcessResult:
        lead_id = str(lead.get("ID") or "")
        if not lead_id:
            raise BitrixError("crm.lead.list вернул лид без ID")

        existing_comments = str(lead.get("COMMENTS") or "")
        new_application = app.application_key not in existing_comments
        fields = self._update_fields(app, enrichment, existing_comments)

        # Leads created from the cloud dump may still have the legacy marker
        # EQAZYNA / eQazyna|<document>|<BIN>. Canonicalise one matched lead so
        # all later parser runs find it directly by BIN and do not create a new
        # duplicate after the migration.
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

    def _create_fields(self, app: Application, enrichment: CompanyEnrichment) -> dict[str, object]:
        fields: dict[str, object] = {
            "TITLE": build_lead_title(app, enrichment),
            "COMPANY_TITLE": enrichment.name or app.applicant_name,
            "STATUS_ID": self.config.lead_status_id or "NEW",
            "OPENED": "Y",
            "COMMENTS": build_lead_comment(app, enrichment),
            "ORIGINATOR_ID": self.config.originator_id,
            "ORIGIN_ID": app.bin,
            "SOURCE_ID": self.config.source_id,
            "SOURCE_DESCRIPTION": self.config.source_description,
            self.config.lead_generation_field: self.config.lead_generation_value,
        }
        phone = self._phone_multifield(enrichment)
        if phone:
            fields["PHONE"] = phone
        assigned_by_id = self._configured_assigned_by_id()
        if assigned_by_id:
            fields["ASSIGNED_BY_ID"] = assigned_by_id
        return fields

    def _update_fields(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        existing_comments: str,
    ) -> dict[str, object]:
        # STATUS_ID is deliberately not updated. A daily parser must not return
        # a qualified/closed lead to the first stage.
        fields: dict[str, object] = {
            "TITLE": build_lead_title(app, enrichment),
            "COMPANY_TITLE": enrichment.name or app.applicant_name,
            "COMMENTS": build_lead_comment(app, enrichment, existing_comments),
            self.config.lead_generation_field: self.config.lead_generation_value,
        }
        phone = self._phone_multifield(enrichment)
        if phone:
            fields["PHONE"] = phone
        if self.config.overwrite_assigned_by_on_update:
            assigned_by_id = self._configured_assigned_by_id()
            if assigned_by_id:
                fields["ASSIGNED_BY_ID"] = assigned_by_id
        return fields

    def _only_changed_fields(
        self,
        current: dict[str, Any],
        desired: dict[str, object],
    ) -> dict[str, object]:
        changed: dict[str, object] = {}
        for field_name, value in desired.items():
            current_value = current.get(field_name)
            if field_name == "PHONE":
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
            value_type = str(item.get("VALUE_TYPE") or "").strip().upper()
            if field_value:
                result.append((field_value, value_type))
        return sorted(result)

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
    def _phone_multifield(enrichment: CompanyEnrichment) -> list[dict[str, str]] | None:
        if not enrichment.phone:
            return None
        value = str(enrichment.phone).strip()
        if not value:
            return None
        return [{"VALUE": value, "VALUE_TYPE": "WORK"}]
