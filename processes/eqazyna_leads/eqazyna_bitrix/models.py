from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class Application:
    created_at_raw: str
    doc_number: str
    bin: str
    applicant_name: str
    doc_type: str
    status: str
    source_url: str

    @property
    def application_key(self) -> str:
        return f"eQazyna|{self.doc_number}|{self.bin}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CompanyEnrichment:
    bin: str
    name: str | None = None
    legal_address: str | None = None
    director: str | None = None
    activity: str | None = None
    oked: str | None = None
    registration_date: str | None = None
    phone: str | None = None
    match_name_score: int | None = None
    match_oked_tpi: bool | None = None
    match_reason: str | None = None
    region: str | None = None
    city: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProcessResult:
    app: Application
    enrichment: CompanyEnrichment
    action: str
    lead_id: str | None = None
    company_id: str | None = None
    contact_id: str | None = None
    requisite_id: str | None = None
    company_action: str | None = None
    contact_action: str | None = None
    requisite_action: str | None = None
    address_action: str | None = None
    assigned_by_id: int | None = None
    assignment_reason: str | None = None
    status_id: str | None = None
    status_reason: str | None = None
    failure_reason: str | None = None
    status_reference_lead_id: str | None = None
    warning: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at_raw": self.app.created_at_raw,
            "doc_number": self.app.doc_number,
            "bin": self.app.bin,
            "applicant_name": self.app.applicant_name,
            "doc_type": self.app.doc_type,
            "status": self.app.status,
            "application_key": self.app.application_key,
            "egov_name": self.enrichment.name,
            "legal_address": self.enrichment.legal_address,
            "director": self.enrichment.director,
            "activity": self.enrichment.activity,
            "oked": self.enrichment.oked,
            "registration_date": self.enrichment.registration_date,
            "phone": self.enrichment.phone,
            "egov_name_score": self.enrichment.match_name_score,
            "egov_oked_tpi": self.enrichment.match_oked_tpi,
            "egov_match_reason": self.enrichment.match_reason,
            "egov_error": self.enrichment.error,
            "egov_raw_preview": self._raw_preview(),
            "region": self.enrichment.region,
            "city": self.enrichment.city,
            "action": self.action,
            "lead_id": self.lead_id,
            "company_id": self.company_id,
            "contact_id": self.contact_id,
            "requisite_id": self.requisite_id,
            "company_action": self.company_action,
            "contact_action": self.contact_action,
            "requisite_action": self.requisite_action,
            "address_action": self.address_action,
            "assigned_by_id": self.assigned_by_id,
            "assignment_reason": self.assignment_reason,
            "status_id": self.status_id,
            "status_reason": self.status_reason,
            "failure_reason": self.failure_reason,
            "status_reference_lead_id": self.status_reference_lead_id,
            "warning": self.warning,
            "error": self.error,
            "source_url": self.app.source_url,
        }

    def _raw_preview(self) -> str | None:
        if not self.enrichment.raw:
            return None
        if self.enrichment.raw.get("raw_preview"):
            return str(self.enrichment.raw["raw_preview"])
        try:
            import json

            return json.dumps(
                self.enrichment.raw,
                ensure_ascii=False,
                default=str,
            )[:3000]
        except Exception:
            return str(self.enrichment.raw)[:3000]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
