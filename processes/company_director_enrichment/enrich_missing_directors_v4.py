from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import xlsxwriter

import director_frozen_plan as frozen
import enrich_missing_directors as base
import enrich_missing_directors_v2 as v2
import enrich_missing_directors_v3 as v3
from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings

PLAN_READY = frozen.PLAN_READY
PLAN_BLOCKED = frozen.PLAN_BLOCKED
PLAN_NOT_APPLICABLE = frozen.PLAN_NOT_APPLICABLE

# Public aliases kept for unit tests and callers inside this package.
build_dry_run_plan = frozen.build_exact_dry_run_plan


def _workbook_columns() -> list[tuple[str, str]]:
    return [
        ("company_id", "Компания ID"),
        ("title", "Компания"),
        ("bin", "БИН"),
        ("director", "Найденный руководитель"),
        ("source", "Источник"),
        ("confidence", "Подтверждение"),
        ("status", "Результат поиска"),
        ("plan_status", "План"),
        ("plan_block_reason", "Причина блокировки"),
        ("planned_contact_action", "Контакт: действие"),
        ("existing_contact_id", "Существующий контакт ID"),
        ("existing_contact_owner_id", "Ответственный существующего контакта"),
        ("planned_contact_id", "Плановый контакт ID"),
        ("planned_contact_owner_id", "Ответственный контакта после 35"),
        ("company_owner_id", "Текущий ответственный компании"),
        ("owner_mismatch", "Ответственный компании != руководителя"),
        ("workflow31_company_would_reassign", "31 сменит компанию"),
        ("workflow31_would_reassign", "31 что-то переназначит"),
        ("workflow31_target_owner_id", "Цель 31"),
        ("lead_owner_snapshot", "Ответственные лидов сейчас"),
        ("workflow31_leads_to_reassign", "Лиды, которым 31 сменит ответственного"),
        ("workflow31_leads_to_reassign_count", "Лидов к переназначению в 31"),
        ("director_group_size", "Компаний у руководителя"),
        ("director_group_company_ids", "Все компании руководителя"),
        ("existing_contact_company_ids", "Уже связанные компании контакта"),
        ("companies_to_link", "Компании к привязке"),
        ("company_contact_snapshot", "Контакты компании на dry-run"),
        ("company_link_action", "Связь контакт-компания"),
        ("requisites_to_update", "Реквизиты к обновлению"),
        ("requisites_to_update_count", "Реквизитов к обновлению"),
        ("lead_ids_all", "Все лиды компании"),
        ("leads_to_link", "Лиды к привязке"),
        ("leads_to_link_count", "Лидов к привязке"),
        ("existing_lead_links", "Лиды уже связаны"),
        ("existing_lead_links_count", "Лидов уже связано"),
        ("lead_contact_snapshot", "Контакты лидов на dry-run"),
        ("duplicate_contact_ids", "Совпавшие контакты / дубли"),
        ("adata_status", "Adata статус"),
        ("adata_director", "Adata руководитель"),
        ("adata_url", "Adata ссылка"),
        ("kompra_status", "Kompra статус"),
        ("kompra_director", "Kompra руководитель"),
        ("kompra_url", "Kompra ссылка"),
        ("evidence", "Совпавшие источники / конфликт"),
        ("url", "Основная ссылка"),
        ("apply_status", "Apply: состояние"),
        ("verification_status", "Apply: проверка"),
        ("contact_id", "Фактический контакт ID"),
        ("contact_owner_id", "Фактический ответственный контакта"),
        ("contact_company_link_added", "Связь с компанией добавлена"),
        ("requisites_updated", "Реквизиты обновлены фактически"),
        ("leads_linked", "Лиды связаны фактически"),
        ("lead_links_existing", "Связи лидов уже были"),
        ("lead_ids_verified", "Проверенные лиды"),
    ]


def _write_workbook_v4(
    output_dir: Path,
    rows: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    plan_meta: dict[str, Any] | None = None,
    apply_errors: list[str] | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(output_dir / "company_director_enrichment.xlsx")
    header = workbook.add_format(
        {"bold": True, "bg_color": "#D9EAF7", "border": 1, "text_wrap": True}
    )
    ready_fmt = workbook.add_format({"bg_color": "#E2F0D9"})
    blocked_fmt = workbook.add_format({"bg_color": "#FCE4D6"})
    mismatch_fmt = workbook.add_format({"bg_color": "#FFF2CC"})
    verified_fmt = workbook.add_format({"bg_color": "#E2F0D9"})

    passport = workbook.add_worksheet("Паспорт плана")
    passport.write_row(0, 0, ["Параметр", "Значение"], header)
    metadata = [
        ("Plan ID", (plan_meta or {}).get("plan_id", "")),
        ("Dry-run GitHub run ID", (plan_meta or {}).get("source_run_id", "")),
        ("Commit SHA", (plan_meta or {}).get("source_sha", "")),
        ("Создан UTC", (plan_meta or {}).get("created_at_utc", "")),
        ("Версия схемы", (plan_meta or {}).get("schema_version", "")),
        (
            "Режим",
            "APPLY утвержденного плана"
            if apply_errors is not None
            else "DRY RUN / утверждаемый план",
        ),
        (
            "Правило",
            "apply выполняет только этот plan_id; внешний поиск при apply запрещен",
        ),
    ]
    for row_idx, (key, value) in enumerate(metadata, 1):
        passport.write(row_idx, 0, key)
        passport.write(row_idx, 1, str(value or ""))
    passport.set_column(0, 0, 30)
    passport.set_column(1, 1, 90)

    sheet = workbook.add_worksheet("Обогащение")
    columns = _workbook_columns()
    for col, (_key, title) in enumerate(columns):
        sheet.write(0, col, title, header)
    for row_idx, row in enumerate(rows, 1):
        row_format = None
        if row.get("plan_status") == PLAN_BLOCKED:
            row_format = blocked_fmt
        elif row.get("verification_status") == "VERIFIED":
            row_format = verified_fmt
        elif row.get("owner_mismatch") == "YES" or row.get(
            "workflow31_would_reassign"
        ) == "YES":
            row_format = mismatch_fmt
        elif row.get("plan_status") == PLAN_READY:
            row_format = ready_fmt
        for col, (key, _title) in enumerate(columns):
            value = row.get(key, "")
            if isinstance(value, (list, tuple, set)):
                value = ",".join(str(item) for item in value)
            if isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            sheet.write(row_idx, col, value, row_format)

    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    for col in range(len(columns)):
        sheet.set_column(col, col, 18)
    sheet.set_column(1, 1, 38)
    sheet.set_column(3, 3, 34)
    sheet.set_column(8, 8, 34)
    sheet.set_column(19, 20, 38)
    sheet.set_column(23, 27, 30)
    sheet.set_column(30, 36, 30)
    sheet.set_column(39, 44, 42)

    skip_sheet = workbook.add_worksheet("Пропуски")
    skip_columns = [
        ("company_id", "Компания ID"),
        ("title", "Компания"),
        ("status", "Причина"),
        ("bins", "БИН / конфликт БИН"),
        ("existing_director", "Существующий руководитель"),
    ]
    for col, (_key, title) in enumerate(skip_columns):
        skip_sheet.write(0, col, title, header)
    for row_idx, row in enumerate(skipped, 1):
        for col, (key, _title) in enumerate(skip_columns):
            skip_sheet.write(row_idx, col, row.get(key, ""))
    skip_sheet.freeze_panes(1, 0)
    skip_sheet.autofilter(
        0, 0, max(len(skipped), 1), len(skip_columns) - 1
    )
    skip_sheet.set_column(0, 0, 12)
    skip_sheet.set_column(1, 1, 42)
    skip_sheet.set_column(2, 4, 32)

    if apply_errors:
        error_sheet = workbook.add_worksheet("Ошибки apply")
        error_sheet.write_row(0, 0, ["№", "Ошибка"], header)
        for idx, error in enumerate(apply_errors, 1):
            error_sheet.write(idx, 0, idx)
            error_sheet.write(idx, 1, error)
        error_sheet.set_column(0, 0, 8)
        error_sheet.set_column(1, 1, 120)

    workbook.close()


def _summary(
    rows: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    plan: dict[str, Any],
    apply: bool,
    errors: list[str],
) -> dict[str, Any]:
    ready_rows = [row for row in rows if row.get("plan_status") == PLAN_READY]
    blocked_rows = [row for row in rows if row.get("plan_status") == PLAN_BLOCKED]
    create_groups = {
        base.fio_key(row.get("director"))
        for row in ready_rows
        if row.get("planned_contact_action") == "CREATE"
        and base.valid_fio(row.get("director"))
    }
    reuse_groups = {
        base.fio_key(row.get("director"))
        for row in ready_rows
        if row.get("planned_contact_action") == "REUSE"
        and base.valid_fio(row.get("director"))
    }
    duplicate_ids: set[int] = set()
    for row in rows:
        duplicate_ids.update(frozen._ids(row.get("duplicate_contact_ids")))

    skip_counts = Counter(str(row.get("status") or "") for row in skipped)
    preflight_failed = any(
        row.get("verification_status") == "PREFLIGHT_FAILED" for row in rows
    )

    return {
        "apply": apply,
        "plan_id": plan.get("plan_id", ""),
        "plan_source_run_id": plan.get("source_run_id", ""),
        "plan_source_sha": plan.get("source_sha", ""),
        "plan_created_at_utc": plan.get("created_at_utc", ""),
        "candidates": len(rows),
        "accepted": sum(row.get("status") == "accepted" for row in rows),
        "source_unavailable": sum(
            row.get("status") == "source_unavailable" for row in rows
        ),
        "no_result": sum(row.get("status") == "no_result" for row in rows),
        "source_conflict": sum(
            row.get("status") == "source_conflict" for row in rows
        ),
        "plan_ready": len(ready_rows),
        "plan_blocked": len(blocked_rows),
        "plan_not_applicable": sum(
            row.get("plan_status") == PLAN_NOT_APPLICABLE for row in rows
        ),
        "plan_contacts_to_create": len(create_groups),
        "plan_contacts_to_reuse": len(reuse_groups),
        "plan_owner_mismatches": sum(
            row.get("owner_mismatch") == "YES" for row in ready_rows
        ),
        "plan_workflow31_company_reassignments": sum(
            row.get("workflow31_company_would_reassign") == "YES"
            for row in ready_rows
        ),
        "plan_workflow31_lead_reassignments": sum(
            int(row.get("workflow31_leads_to_reassign_count") or 0)
            for row in ready_rows
        ),
        "plan_company_links_to_add": sum(
            row.get("company_link_action")
            in {"CREATE_PRIMARY", "ADD_SECONDARY_AFTER_CREATE", "ADD_LINK"}
            for row in ready_rows
        ),
        "plan_requisites_to_update": sum(
            int(row.get("requisites_to_update_count") or 0)
            for row in ready_rows
        ),
        "plan_lead_links_to_add": sum(
            int(row.get("leads_to_link_count") or 0) for row in ready_rows
        ),
        "plan_duplicate_contacts_detected": len(duplicate_ids),
        "skipped": len(skipped),
        "skipped_by_status": dict(sorted(skip_counts.items())),
        "preflight_ok": not preflight_failed if apply else None,
        "apply_verified_rows": sum(
            row.get("verification_status") == "VERIFIED" for row in rows
        ),
        "errors": len(errors),
        "error_messages": errors,
    }


def write_report_v4(
    output_dir: Path,
    rows: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    plan: dict[str, Any],
    apply: bool,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    errors = list(errors or [])
    output_dir.mkdir(parents=True, exist_ok=True)
    details = {
        "apply": apply,
        "plan_id": plan.get("plan_id", ""),
        "rows": rows,
        "skipped": skipped,
        "errors": errors,
    }
    (output_dir / "company_director_enrichment.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if errors:
        (output_dir / "company_director_apply_errors.json").write_text(
            json.dumps(errors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    _write_workbook_v4(
        output_dir,
        rows,
        skipped,
        plan_meta=plan,
        apply_errors=errors if apply else None,
    )
    summary = _summary(rows, skipped, plan, apply, errors)
    (output_dir / "company_director_enrichment_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def _client() -> BitrixClient:
    settings = Settings.from_env()
    return BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )


def _run_dry(args: argparse.Namespace) -> int:
    client = _client()
    print("[DIRECTOR] loading Bitrix companies/requisites/contacts", flush=True)
    snapshot = base.load_snapshot(client)
    candidates, skipped = base.build_candidates(snapshot)
    candidates = v2._filter_secondary_directors(
        client, candidates, skipped, snapshot, args.workers
    )
    candidates.sort(key=lambda item: int(item["company_id"]))
    if args.max_companies:
        candidates = candidates[: args.max_companies]

    print(
        f"[DIRECTOR] candidates={len(candidates)} skipped={len(skipped)}",
        flush=True,
    )
    rows = v3.enrich_candidates_two_sources(
        candidates,
        min(args.workers, 12),
        args.http_timeout,
    )
    rows = frozen.mark_source_unavailable(rows)
    rows = v2._annotate_director_groups(rows)

    print("[DIRECTOR] building immutable execution plan", flush=True)
    rows = frozen.build_exact_dry_run_plan(client, rows, args.workers)

    source_sha = str(os.getenv("GITHUB_SHA") or "LOCAL")
    source_run_id = str(os.getenv("GITHUB_RUN_ID") or "LOCAL")
    plan = frozen.create_frozen_plan(
        rows,
        skipped,
        source_sha=source_sha,
        source_run_id=source_run_id,
    )
    frozen.save_frozen_plan(Path(args.output_dir), plan)
    summary = write_report_v4(
        Path(args.output_dir),
        rows,
        skipped,
        plan,
        apply=False,
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    print(
        f"[DIRECTOR] PLAN_ID={plan['plan_id']} RUN_ID={plan['source_run_id']}",
        flush=True,
    )
    return 0


def _run_apply(args: argparse.Namespace) -> int:
    if not args.expected_plan_id:
        raise SystemExit("--expected-plan-id is required for apply")
    current_sha = str(args.current_sha or os.getenv("GITHUB_SHA") or "")
    plan = frozen.load_frozen_plan(
        Path(args.apply_plan),
        expected_plan_id=args.expected_plan_id,
        current_sha=current_sha,
    )
    client = _client()
    print(
        f"[DIRECTOR] applying frozen plan {plan['plan_id']} "
        f"from run {plan.get('source_run_id', '')}",
        flush=True,
    )
    rows, errors = frozen.apply_frozen_plan(client, plan)
    summary = write_report_v4(
        Path(args.output_dir),
        rows,
        list(plan.get("skipped") or []),
        plan,
        apply=True,
        errors=errors,
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Director enrichment: dry-run creates an immutable plan; "
            "apply executes only that exact plan without external search"
        )
    )
    parser.add_argument(
        "--apply-plan",
        default="",
        help="Path to company_director_plan.json from an approved dry-run",
    )
    parser.add_argument(
        "--expected-plan-id",
        default="",
        help="Exact PLAN_ID shown by the approved dry-run",
    )
    parser.add_argument(
        "--current-sha",
        default=os.getenv("GITHUB_SHA", ""),
        help="Current code SHA; apply rejects plans created by different code",
    )
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-companies", type=int, default=0)
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("DIRECTOR_ENRICH_WORKERS", "6") or 6),
    )
    parser.add_argument(
        "--http-timeout",
        type=int,
        default=int(os.getenv("DIRECTOR_HTTP_TIMEOUT", "12") or 12),
    )
    args = parser.parse_args()

    if args.max_companies < 0 or args.workers <= 0 or args.http_timeout <= 0:
        parser.error(
            "max-companies must be >= 0; workers/http-timeout must be > 0"
        )
    if args.apply_plan:
        return _run_apply(args)
    return _run_dry(args)


if __name__ == "__main__":
    raise SystemExit(main())
