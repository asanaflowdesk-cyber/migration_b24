from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .bitrix_client import BitrixClient
from .egov_client import EgovClient
from .exporter import write_xlsx
from .lead_pipeline import LeadPipeline, LeadPipelineConfig
from .models import Application, CompanyEnrichment, ProcessResult
from .scraper import EqazynaScraper
from .settings import Settings, env_bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "e-Qazyna Minerals -> eGov -> Bitrix24. "
            "One BIN creates a complete CRM bundle: lead, company, requisite and director contact."
        )
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=int(os.getenv("EQAZYNA_PAGES", "1")),
        help="How many consecutive e-Qazyna pages to process",
    )
    parser.add_argument(
        "--page-start",
        type=int,
        default=int(os.getenv("EQAZYNA_PAGE_START", "1")),
        help="First e-Qazyna page",
    )
    parser.add_argument(
        "--page-list",
        default=os.getenv("EQAZYNA_PAGE_LIST") or None,
        help="Explicit pages/ranges, e.g. 16,22,30-35; overrides page-start/pages",
    )
    parser.add_argument(
        "--doc-type",
        default=os.getenv("EQAZYNA_DOC_TYPE", "Заявка на разведку ТПИ"),
    )
    parser.add_argument(
        "--statuses",
        default=os.getenv("EQAZYNA_STATUSES", "Отправлено на рассмотрение,Принято"),
    )
    parser.add_argument(
        "--min-created-date",
        default=os.getenv("EQAZYNA_MIN_CREATED_DATE") or None,
        help="Only applications created on/after YYYY-MM-DD",
    )
    parser.add_argument("--out", default=None, help="Output XLSX path")
    parser.add_argument("--json-out", default=None, help="Output JSON path")
    parser.add_argument("--no-egov", action="store_true", help="Disable eGov enrichment")
    parser.add_argument(
        "--push-bitrix",
        action="store_true",
        help="Read and create/update Bitrix24 leads",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Bitrix24 but do not create or update anything",
    )
    parser.add_argument(
        "--lead-status-id",
        default=os.getenv("BITRIX_LEAD_STATUS_ID", "NEW"),
        help="Initial status for newly created leads only",
    )
    parser.add_argument(
        "--assigned-by-id",
        default=os.getenv("BITRIX_ASSIGNED_BY_ID") or None,
        help="Optional responsible user ID for newly created leads",
    )
    parser.add_argument(
        "--overwrite-assigned-by-on-update",
        action="store_true",
        default=env_bool("BITRIX_OVERWRITE_ASSIGNED_BY_ON_UPDATE", False),
        help="Also overwrite responsible user on existing leads (off by default)",
    )
    parser.add_argument(
        "--lead-generation-field",
        default=os.getenv("BITRIX_LEAD_GENERATION_FIELD", "UF_CRM_1785917145255"),
    )
    parser.add_argument(
        "--lead-generation-value",
        default=os.getenv("BITRIX_LEAD_GENERATION_VALUE", "ГПО Недропользователя"),
    )
    parser.add_argument(
        "--originator-id",
        default=os.getenv("BITRIX_ORIGINATOR_ID", "EQAZYNA_LEAD"),
    )
    parser.add_argument(
        "--company-originator-id",
        default=os.getenv("BITRIX_COMPANY_ORIGINATOR_ID", "EQAZYNA"),
        help="Originator marker used by migrated and new e-Qazyna companies",
    )
    parser.add_argument(
        "--requisite-preset-id",
        default=os.getenv("BITRIX_REQUISITE_PRESET_ID", "1"),
        help="Company requisite preset ID in the target Bitrix24; 1 = preset used by migrated company requisites; a stale ID is replaced from existing requisites",
    )
    parser.add_argument(
        "--source-id",
        default=os.getenv("BITRIX_SOURCE_ID", "OTHER"),
    )
    parser.add_argument(
        "--source-description",
        default=os.getenv(
            "BITRIX_SOURCE_DESCRIPTION",
            "e-Qazyna Minerals. ГПО недропользователи",
        ),
    )
    parser.add_argument(
        "--skip-field-validation",
        action="store_true",
        help="Skip crm.lead.fields preflight check",
    )
    parser.add_argument(
        "--strict-page-errors",
        action="store_true",
        help="Stop immediately when any e-Qazyna page fails",
    )
    parser.add_argument(
        "--max-consecutive-page-errors",
        type=int,
        default=int(os.getenv("EQAZYNA_MAX_CONSECUTIVE_PAGE_ERRORS", "1")),
        help="Stop scraping after N consecutive page errors; 0 disables the limit",
    )
    return parser.parse_args()


def _enrichment_key(bin_number: str, name: str) -> tuple[str, str]:
    return ((bin_number or "").strip(), " ".join((name or "").lower().split()))


def _build_enrichment_map(
    applications: Iterable[Application],
    egov: EgovClient,
) -> dict[tuple[str, str], CompanyEnrichment]:
    unique: dict[tuple[str, str], tuple[str, str]] = {}
    application_count = 0
    for app in applications:
        application_count += 1
        key = _enrichment_key(app.bin, app.applicant_name)
        unique.setdefault(key, (app.bin, app.applicant_name))

    print(
        f"    unique eGov BIN+name pairs: {len(unique)} "
        f"from {application_count} applications"
    )
    result: dict[tuple[str, str], CompanyEnrichment] = {}
    for idx, (key, (bin_number, name)) in enumerate(unique.items(), start=1):
        print(f"    eGov {idx}/{len(unique)} {bin_number} {name[:70]}")
        result[key] = egov.get_company(bin_number, name)
    return result


def _parse_min_created_date(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit("--min-created-date must use YYYY-MM-DD") from exc


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    if args.push_bitrix and not args.no_egov and not settings.egov_api_key:
        raise SystemExit("EGOV_API_KEY is required for Bitrix24 write and dry-run modes")
    statuses = [status.strip() for status in args.statuses.split(",") if status.strip()]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = args.out or f"exports/eqazyna_bitrix_leads_{timestamp}.xlsx"
    json_path = args.json_out or f"exports/eqazyna_bitrix_leads_{timestamp}.json"
    min_created_date = _parse_min_created_date(args.min_created_date)

    eqazyna_max_retries = int(getattr(settings, "eqazyna_max_retries", 4))
    eqazyna_retry_base_sleep_seconds = float(
        getattr(settings, "eqazyna_retry_base_sleep_seconds", 3.0)
    )

    print(
        "[1/4] Scraping e-Qazyna: "
        f"page_start={args.page_start}, pages={args.pages}, page_list={args.page_list!r}, "
        f"timeout={settings.eqazyna_request_timeout}, "
        f"retries={eqazyna_max_retries}, "
        f"retry_base_sleep={eqazyna_retry_base_sleep_seconds}, "
        f"max_consecutive_page_errors={args.max_consecutive_page_errors}, "
        f"doc_type={args.doc_type!r}, statuses={statuses}, "
        f"min_created_date={min_created_date}"
    )
    scraper = EqazynaScraper(
        timeout=settings.eqazyna_request_timeout,
        polite_delay_seconds=settings.polite_delay_seconds,
        max_retries=eqazyna_max_retries,
        retry_base_sleep_seconds=eqazyna_retry_base_sleep_seconds,
        continue_on_page_error=not args.strict_page_errors,
        max_consecutive_page_errors=args.max_consecutive_page_errors,
    )
    applications = scraper.scrape(
        args.pages,
        args.doc_type,
        statuses,
        min_created_date=min_created_date,
        page_start=args.page_start,
        page_list=args.page_list,
    )
    print(f"Found applications after filter: {len(applications)}")
    if scraper.failed_pages:
        print(f"FAILED_PAGES={','.join(map(str, scraper.failed_pages))}")

    print("[2/4] Staged eGov enrichment")
    egov = EgovClient(
        None if args.no_egov else settings.egov_api_key,
        timeout=settings.egov_request_timeout,
        polite_delay_seconds=settings.egov_polite_delay_seconds,
    )
    enrichment_map = _build_enrichment_map(applications, egov)

    lead_pipeline: LeadPipeline | None = None
    if args.push_bitrix:
        if not settings.bitrix_webhook_url:
            raise SystemExit("BITRIX_WEBHOOK_URL is required when --push-bitrix is used")

        client = BitrixClient(
            settings.bitrix_webhook_url,
            timeout=settings.bitrix_request_timeout,
            polite_delay_seconds=settings.bitrix_polite_delay_seconds,
            verify_ssl=settings.bitrix_tls_verify,
        )
        lead_pipeline = LeadPipeline(
            client,
            LeadPipelineConfig(
                lead_status_id=args.lead_status_id,
                assigned_by_id=args.assigned_by_id,
                overwrite_assigned_by_on_update=args.overwrite_assigned_by_on_update,
                lead_generation_field=args.lead_generation_field,
                lead_generation_value=args.lead_generation_value,
                originator_id=args.originator_id,
                company_originator_id=args.company_originator_id,
                requisite_preset_id=args.requisite_preset_id,
                source_id=args.source_id,
                source_description=args.source_description,
                dry_run=args.dry_run,
                validate_custom_field=not args.skip_field_validation,
            ),
        )
        print(
            "    Bitrix lead mode: "
            f"field={args.lead_generation_field}, "
            f"value={args.lead_generation_value!r}, "
            f"originator={args.originator_id}, company_originator={args.company_originator_id}, "
            f"requisite_preset={args.requisite_preset_id}, dry_run={args.dry_run}"
        )
        lead_pipeline.validate()
        resolved_preset = getattr(lead_pipeline, "requisite_preset_id", None)
        if resolved_preset is not None:
            print(f"    Resolved company requisite preset: {resolved_preset}")
        for warning in getattr(lead_pipeline, "validation_warnings", []):
            print(f"    WARNING: {warning}")

    print("[3/4] Processing Bitrix24 leads")
    results: list[ProcessResult] = []
    for idx, app in enumerate(applications, start=1):
        print(f"  Lead {idx}/{len(applications)} {app.doc_number} {app.bin} {app.applicant_name[:60]}")
        enrichment = enrichment_map.get(_enrichment_key(app.bin, app.applicant_name))
        if enrichment is None:
            enrichment = CompanyEnrichment(bin=app.bin, error="enrichment_missing")

        if lead_pipeline:
            result = lead_pipeline.process(app, enrichment)
        else:
            result = ProcessResult(app, enrichment, action="excel_only")

        if result.error:
            print(f"    ERROR: {result.error}")
        else:
            print(
                f"    {result.action}: lead={result.lead_id} company={result.company_id} "
                f"contact={result.contact_id} requisite={result.requisite_id}"
            )
            if result.warning:
                print(f"    WARNING: {result.warning}")
        results.append(result)

    print("[4/4] Writing logs")
    xlsx = write_xlsx(results, xlsx_path)
    result_path = Path(json_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps([result.as_dict() for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    pages_path = result_path.with_name(result_path.stem + "_pages.json")
    pages_payload = {
        "page_start": args.page_start,
        "pages_requested": args.pages,
        "page_list": args.page_list,
        "failed_pages": scraper.failed_pages,
        "page_logs": [page.as_dict() for page in scraper.page_logs],
        "applications_collected": len(applications),
        "results_written": len(results),
        "lead_generation_field": args.lead_generation_field,
        "lead_generation_value": args.lead_generation_value,
        "dry_run": args.dry_run,
    }
    pages_path.write_text(
        json.dumps(pages_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    errors = sum(1 for result in results if result.error)
    warnings = sum(1 for result in results if result.warning)
    print(f"SUMMARY applications={len(results)} errors={errors} warnings={warnings}")
    print(f"XLSX: {xlsx}")
    print(f"PAGES JSON: {pages_path}")
    print(f"JSON: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
