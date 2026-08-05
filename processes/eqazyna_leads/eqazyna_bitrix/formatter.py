from __future__ import annotations

from .models import Application, CompanyEnrichment, utc_now_iso
from .search_links import build_search_links


def build_lead_title(app: Application, enrichment: CompanyEnrichment) -> str:
    name = enrichment.name or app.applicant_name
    return f"e-Qazyna лид — {name}"[:250]


def build_lead_comment(
    app: Application,
    enrichment: CompanyEnrichment,
    previous_comments: str | None = None,
) -> str:
    if previous_comments and app.application_key in previous_comments:
        return previous_comments

    application_block = build_application_block(app)
    if previous_comments:
        return f"{previous_comments.rstrip()}\n\n---\n{application_block}"[:65000]

    summary = build_company_summary(app, enrichment)
    return f"{summary}\n\n---\n{application_block}"[:65000]


def build_company_summary(app: Application, enrichment: CompanyEnrichment) -> str:
    name = enrichment.name or app.applicant_name
    links = build_search_links(app.bin, name, enrichment.city or enrichment.region)
    return "\n".join(
        [
            "Источник лидогенерации: e-Qazyna Minerals",
            "Тип лидогенерации: ГПО Недропользователя",
            "",
            f"БИН: {app.bin}",
            f"Компания: {name}",
            f"Юридический адрес: {enrichment.legal_address or 'не найден'}",
            f"Регион: {enrichment.region or 'не определён'}",
            f"Город: {enrichment.city or 'не определён'}",
            f"Руководитель: {enrichment.director or 'не найден'}",
            f"Телефон из eGov: {enrichment.phone or 'не найден'}",
            f"ОКЭД / деятельность: {_activity_line(enrichment)}",
            f"Дата регистрации: {enrichment.registration_date or 'не найдена'}",
            "",
            "Ручной поиск контактов:",
            f"2GIS: {links['2gis']}",
            f"Google: {links['google']}",
            f"Yandex: {links['yandex']}",
            "",
            f"Обновлено интеграцией: {utc_now_iso()}",
        ]
    )


def build_application_block(app: Application) -> str:
    return "\n".join(
        [
            "Заявка e-Qazyna в пакете лида",
            f"Номер заявки: {app.doc_number}",
            f"Дата создания заявки: {app.created_at_raw}",
            f"Тип документа: {app.doc_type}",
            f"Статус заявки: {app.status}",
            f"БИН: {app.bin}",
            f"Ключ заявки: {app.application_key}",
            f"Источник: {app.source_url}",
        ]
    )


def build_timeline_comment(app: Application, enrichment: CompanyEnrichment) -> str:
    name = enrichment.name or app.applicant_name
    return "\n".join(
        [
            "Новая заявка e-Qazyna",
            "",
            f"Номер заявки: {app.doc_number}",
            f"Дата создания заявки: {app.created_at_raw}",
            f"Тип документа: {app.doc_type}",
            f"Статус заявки: {app.status}",
            f"БИН: {app.bin}",
            f"Компания: {name}",
            f"Ключ заявки: {app.application_key}",
            f"Источник: {app.source_url}",
        ]
    )


def _activity_line(enrichment: CompanyEnrichment) -> str:
    if enrichment.oked and enrichment.activity:
        return f"{enrichment.oked} — {enrichment.activity}"
    return enrichment.activity or enrichment.oked or "не найдено"
