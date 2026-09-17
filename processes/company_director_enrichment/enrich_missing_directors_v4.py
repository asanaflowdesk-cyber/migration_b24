from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import xlsxwriter

import enrich_missing_directors as base
import enrich_missing_directors_v2 as v2
import enrich_missing_directors_v3 as v3
from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings

PLAN_READY = "READY"
PLAN_BLOCKED = "BLOCKED"
PLAN_NOT_APPLICABLE = "NOT_APPLICABLE"

def _csv(values: list[int] | set[int]) -> str:
    return ",".join(str(value) for value in sorted(set(values)))

def _group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("status") == "accepted" and base.valid_fio(row.get("director")):
            groups[base.fio_key(row["director"])].append(dict(row))
    return groups

def _company_leads_for_plan(client: BitrixClient, company_id: int) -> list[dict[str, Any]]:
    return client.list_all("crm.lead.list", {"order": {"ID": "ASC"}, "filter": {"COMPANY_ID": company_id}, "select": ["ID", "COMPANY_ID", "CONTACT_ID", "CONTACT_IDS", "ASSIGNED_BY_ID"]})

def _safe_lead_plan(client: BitrixClient, company_id: int, existing_contact_id: int | None) -> tuple[list[int], list[int], list[int], dict[int, int | None]]:
    all_leads: list[int] = []
    to_link: list[int] = []
    existing: list[int] = []
    lead_owners: dict[int, int | None] = {}
    for lead in _company_leads_for_plan(client, company_id):
        lead_id = base.normalize_id(lead.get("ID"))
        if lead_id is None:
            continue
        all_leads.append(lead_id)
        lead_owners[lead_id] = base.normalize_id(lead.get("ASSIGNED_BY_ID"))
        if existing_contact_id is None:
            to_link.append(lead_id)
            continue
        linked_ids = v2._binding_ids(v2._lead_contact_bindings(client, lead_id), "CONTACT_ID")
        if existing_contact_id in linked_ids:
            existing.append(lead_id)
        else:
            to_link.append(lead_id)
    return all_leads, to_link, existing, lead_owners

def _planned_requisite_ids(requisites: list[dict[str, Any]], bin_number: str, director: str) -> tuple[list[int], str]:
    update_ids: list[int] = []
    wanted = base.fio_key(director)
    for requisite in requisites:
        req_id = base.normalize_id(requisite.get("ID"))
        if req_id is None:
            continue
        current = requisite.get("RQ_DIRECTOR")
        if base.valid_fio(current):
            if base.fio_key(current) != wanted:
                return [], f"requisite_director_conflict:{req_id}"
            continue
        req_bin = base.normalize_bin(requisite.get("RQ_INN"))
        if req_bin and req_bin != bin_number:
            continue
        update_ids.append(req_id)
    return update_ids, ""

def _plan_group(client: BitrixClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted((dict(row) for row in rows), key=lambda item: int(item["company_id"]))
    director = base.normalize_fio(ordered[0]["director"])
    group_company_ids = [int(row["company_id"]) for row in ordered]
    matches = v2._global_contacts_for_director(client, director)
    canonical, duplicate_ids = v2._choose_canonical_contact(matches)
    existing_contact_id = base.normalize_id(canonical.get("ID")) if canonical else None
    existing_contact_owner_id = base.normalize_id(canonical.get("ASSIGNED_BY_ID")) if canonical else None
    fresh: dict[int, tuple[dict[str, Any] | None, list[dict[str, Any]], str, int | None]] = {}
    fresh_owners: set[int] = set()
    for row in ordered:
        company, requisites, _contacts, state = v2._fresh_row_state(client, row, director)
        company_owner_id = base.normalize_id(company.get("ASSIGNED_BY_ID")) if company else None
        fresh[int(row["company_id"])] = (company, requisites, state, company_owner_id)
        if company_owner_id is not None:
            fresh_owners.add(company_owner_id)
    group_error = ""
    planned_contact_action = "REUSE" if canonical else "CREATE"
    planned_contact_owner_id = existing_contact_owner_id
    if canonical:
        if existing_contact_id is None:
            group_error = "contact_invalid_id"
        elif planned_contact_owner_id is None:
            candidate_owners = {base.normalize_id(row.get("owner_id")) for row in ordered if base.normalize_id(row.get("owner_id")) is not None}
            if len(candidate_owners) != 1:
                group_error = "director_owner_conflict"
            else:
                planned_contact_owner_id = next(iter(candidate_owners))
    else:
        if any(fresh[company_id][0] is None for company_id in group_company_ids):
            group_error = "company_missing"
        elif any(fresh[company_id][3] is None for company_id in group_company_ids):
            group_error = "company_without_owner"
        elif len(fresh_owners) != 1:
            group_error = "director_owner_conflict"
        else:
            planned_contact_owner_id = next(iter(fresh_owners))
    existing_contact_company_ids: set[int] = set()
    if existing_contact_id is not None:
        existing_contact_company_ids = v2._binding_ids(v2._contact_company_bindings(client, existing_contact_id), "COMPANY_ID")
    group_companies_to_link = [company_id for company_id in group_company_ids if existing_contact_id is None or company_id not in existing_contact_company_ids]
    results: list[dict[str, Any]] = []
    primary_new_company_id = min(group_company_ids) if group_company_ids else None
    for row in ordered:
        result = dict(row)
        company_id = int(row["company_id"])
        company, requisites, row_state, company_owner_id = fresh[company_id]
        result.update({"plan_status": PLAN_READY, "plan_block_reason": "", "planned_contact_action": planned_contact_action, "existing_contact_id": existing_contact_id or "", "existing_contact_owner_id": existing_contact_owner_id or "", "planned_contact_id": existing_contact_id or "NEW", "planned_contact_owner_id": planned_contact_owner_id or "", "company_owner_id": company_owner_id or "", "owner_mismatch": "", "workflow31_company_would_reassign": "", "workflow31_would_reassign": "", "workflow31_target_owner_id": planned_contact_owner_id or "", "lead_owner_snapshot": "", "workflow31_leads_to_reassign": "", "workflow31_leads_to_reassign_count": 0, "director_group_company_ids": _csv(group_company_ids), "existing_contact_company_ids": _csv(existing_contact_company_ids), "companies_to_link": _csv(group_companies_to_link), "duplicate_contact_ids": _csv(duplicate_ids), "company_link_action": "", "requisites_to_update": "", "requisites_to_update_count": 0, "lead_ids_all": "", "leads_to_link": "", "leads_to_link_count": 0, "existing_lead_links": "", "existing_lead_links_count": 0})
        if group_error:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = group_error
            result["planned_contact_action"] = "BLOCKED"
            results.append(result)
            continue
        if row_state != "ok" or company is None:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = row_state
            results.append(result)
            continue
        if company_owner_id is None:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = "company_without_owner"
            results.append(result)
            continue
        mismatch = bool(planned_contact_owner_id and company_owner_id != planned_contact_owner_id)
        result["owner_mismatch"] = "YES" if mismatch else "NO"
        result["workflow31_company_would_reassign"] = "YES" if mismatch else "NO"
        if existing_contact_id is not None:
            result["company_link_action"] = "ALREADY_LINKED" if company_id in existing_contact_company_ids else "ADD_LINK"
        else:
            result["company_link_action"] = "CREATE_PRIMARY" if company_id == primary_new_company_id else "ADD_SECONDARY_AFTER_CREATE"
        requisite_ids, requisite_error = _planned_requisite_ids(requisites, row["bin"], director)
        if requisite_error:
            result["plan_status"] = PLAN_BLOCKED
            result["plan_block_reason"] = requisite_error
            results.append(result)
            continue
        result["requisites_to_update"] = _csv(requisite_ids)
        result["requisites_to_update_count"] = len(requisite_ids)
        all_leads, to_link, existing, lead_owners = _safe_lead_plan(client, company_id, existing_contact_id)
        result["lead_ids_all"] = _csv(all_leads)
        result["leads_to_link"] = _csv(to_link)
        result["leads_to_link_count"] = len(to_link)
        result["existing_lead_links"] = _csv(existing)
        result["existing_lead_links_count"] = len(existing)
        result["lead_owner_snapshot"] = ",".join(f"{lead_id}:{lead_owners[lead_id] or 'NONE'}" for lead_id in sorted(lead_owners))
        leads_to_reassign = [lead_id for lead_id, owner_id in lead_owners.items() if planned_contact_owner_id and owner_id != planned_contact_owner_id]
        result["workflow31_leads_to_reassign"] = _csv(leads_to_reassign)
        result["workflow31_leads_to_reassign_count"] = len(leads_to_reassign)
        result["workflow31_would_reassign"] = "YES" if mismatch or leads_to_reassign else "NO"
        results.append(result)
    return results

def build_dry_run_plan(client: BitrixClient, rows: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    untouched = []
    accepted_groups = _group_rows(rows)
    accepted_keys = set(accepted_groups)
    for row in rows:
        key = base.fio_key(row.get("director")) if base.valid_fio(row.get("director")) else ""
        if not (row.get("status") == "accepted" and key in accepted_keys):
            item = dict(row)
            item["plan_status"] = PLAN_NOT_APPLICABLE
            item["plan_block_reason"] = ""
            untouched.append(item)
    planned: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 6)), thread_name_prefix="director-plan") as pool:
        futures = {pool.submit(_plan_group, base.clone_client(client), group_rows): key for key, group_rows in accepted_groups.items()}
        done = 0
        for future in as_completed(futures):
            done += 1
            key = futures[future]
            try:
                planned.extend(future.result())
            except Exception as exc:
                for row in accepted_groups[key]:
                    item = dict(row)
                    item["plan_status"] = PLAN_BLOCKED
                    item["plan_block_reason"] = f"plan_error:{type(exc).__name__}"
                    item["planned_contact_action"] = "BLOCKED"
                    planned.append(item)
            print(f"[DIRECTOR] plan {done}/{len(accepted_groups)} director={key}", flush=True)
    return sorted(untouched + planned, key=lambda item: int(item["company_id"]))

def _write_workbook_v4(output_dir: Path, rows: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> None:
    workbook = xlsxwriter.Workbook(output_dir / "company_director_enrichment.xlsx")
    header = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "text_wrap": True})
    ready_fmt = workbook.add_format({"bg_color": "#E2F0D9"})
    blocked_fmt = workbook.add_format({"bg_color": "#FCE4D6"})
    mismatch_fmt = workbook.add_format({"bg_color": "#FFF2CC"})
    sheet = workbook.add_worksheet("Обогащение")
    columns = [("company_id", "Компания ID"), ("title", "Компания"), ("bin", "БИН"), ("director", "Найденный руководитель"), ("source", "Источник"), ("confidence", "Подтверждение"), ("status", "Результат поиска"), ("plan_status", "План"), ("plan_block_reason", "Причина блокировки"), ("planned_contact_action", "Контакт: действие"), ("existing_contact_id", "Существующий контакт ID"), ("existing_contact_owner_id", "Ответственный существующего контакта"), ("planned_contact_id", "Плановый контакт ID"), ("planned_contact_owner_id", "Ответственный контакта после 35"), ("company_owner_id", "Текущий ответственный компании"), ("owner_mismatch", "Ответственный компании != руководителя"), ("workflow31_company_would_reassign", "31 сменит компанию"), ("workflow31_would_reassign", "31 что-то переназначит"), ("workflow31_target_owner_id", "Цель 31"), ("lead_owner_snapshot", "Ответственные лидов сейчас"), ("workflow31_leads_to_reassign", "Лиды, которым 31 сменит ответственного"), ("workflow31_leads_to_reassign_count", "Лидов к переназначению в 31"), ("director_group_size", "Компаний у руководителя"), ("director_group_company_ids", "Все компании руководителя"), ("existing_contact_company_ids", "Уже связанные компании контакта"), ("companies_to_link", "Компании к привязке"), ("company_link_action", "Связь контакт-компания"), ("requisites_to_update", "Реквизиты к обновлению"), ("requisites_to_update_count", "Реквизитов к обновлению"), ("lead_ids_all", "Все лиды компании"), ("leads_to_link", "Лиды к привязке"), ("leads_to_link_count", "Лидов к привязке"), ("existing_lead_links", "Лиды уже связаны"), ("existing_lead_links_count", "Лидов уже связано"), ("duplicate_contact_ids", "Дубли контакта"), ("adata_status", "Adata статус"), ("adata_director", "Adata руководитель"), ("adata_url", "Adata ссылка"), ("kompra_status", "Kompra статус"), ("kompra_director", "Kompra руководитель"), ("kompra_url", "Kompra ссылка"), ("evidence", "Совпавшие источники / конфликт"), ("url", "Основная ссылка"), ("error", "Ошибка apply"), ("contact_id", "Фактический контакт ID"), ("contact_owner_id", "Фактический ответственный контакта"), ("contact_company_link_added", "Связь с компанией добавлена"), ("requisites_updated", "Реквизиты обновлены фактически"), ("leads_linked", "Лиды связаны фактически"), ("lead_links_existing", "Связи лидов уже были"), ("lead_ids_verified", "Проверенные лиды")]
    for col, (_key, title) in enumerate(columns):
        sheet.write(0, col, title, header)
    for row_idx, row in enumerate(rows, 1):
        for col, (key, _title) in enumerate(columns):
            sheet.write(row_idx, col, row.get(key, ""))
        if row.get("plan_status") == PLAN_BLOCKED:
            sheet.set_row(row_idx, None, blocked_fmt)
        elif row.get("owner_mismatch") == "YES":
            sheet.set_row(row_idx, None, mismatch_fmt)
        elif row.get("plan_status") == PLAN_READY:
            sheet.set_row(row_idx, None, ready_fmt)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    for col in range(len(columns)):
        sheet.set_column(col, col, 18)
    sheet.set_column(1, 1, 38)
    sheet.set_column(3, 3, 34)
    sheet.set_column(8, 8, 30)
    sheet.set_column(19, 20, 34)
    sheet.set_column(23, 25, 28)
    sheet.set_column(29, 34, 28)
    sheet.set_column(37, 40, 42)
    skip_sheet = workbook.add_worksheet("Пропуски")
    skip_columns = [("company_id", "Компания ID"), ("title", "Компания"), ("status", "Причина"), ("bins", "БИН / конфликт БИН"), ("existing_director", "Существующий руководитель")]
    for col, (_key, title) in enumerate(skip_columns):
        skip_sheet.write(0, col, title, header)
    for row_idx, row in enumerate(skipped, 1):
        for col, (key, _title) in enumerate(skip_columns):
            skip_sheet.write(row_idx, col, row.get(key, ""))
    skip_sheet.freeze_panes(1, 0)
    skip_sheet.autofilter(0, 0, max(len(skipped), 1), len(skip_columns) - 1)
    skip_sheet.set_column(0, 0, 12)
    skip_sheet.set_column(1, 1, 42)
    skip_sheet.set_column(2, 4, 32)
    workbook.close()

def write_report_v4(output_dir: Path, rows: list[dict[str, Any]], skipped: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    summary = v2._write_report_v2(output_dir, rows, skipped, apply)
    _write_workbook_v4(output_dir, rows, skipped)
    ready_rows = [row for row in rows if row.get("plan_status") == PLAN_READY]
    blocked_rows = [row for row in rows if row.get("plan_status") == PLAN_BLOCKED]
    create_groups = {base.fio_key(row.get("director")) for row in ready_rows if row.get("planned_contact_action") == "CREATE" and base.valid_fio(row.get("director"))}
    reuse_groups = {base.fio_key(row.get("director")) for row in ready_rows if row.get("planned_contact_action") == "REUSE" and base.valid_fio(row.get("director"))}
    duplicate_ids: set[int] = set()
    for row in rows:
        for token in str(row.get("duplicate_contact_ids") or "").split(","):
            if token.strip().isdigit():
                duplicate_ids.add(int(token.strip()))
    summary.update({"plan_ready": len(ready_rows), "plan_blocked": len(blocked_rows), "plan_contacts_to_create": len(create_groups), "plan_contacts_to_reuse": len(reuse_groups), "plan_owner_mismatches": sum(row.get("owner_mismatch") == "YES" for row in ready_rows), "plan_workflow31_company_reassignments": sum(row.get("workflow31_company_would_reassign") == "YES" for row in ready_rows), "plan_workflow31_lead_reassignments": sum(int(row.get("workflow31_leads_to_reassign_count") or 0) for row in ready_rows), "plan_company_links_to_add": sum(row.get("company_link_action") in {"CREATE_PRIMARY", "ADD_SECONDARY_AFTER_CREATE", "ADD_LINK"} for row in ready_rows), "plan_requisites_to_update": sum(int(row.get("requisites_to_update_count") or 0) for row in ready_rows), "plan_lead_links_to_add": sum(int(row.get("leads_to_link_count") or 0) for row in ready_rows), "plan_lead_links_existing": sum(int(row.get("existing_lead_links_count") or 0) for row in ready_rows), "plan_duplicate_contacts_detected": len(duplicate_ids), "plan_multi_company_director_groups": len({base.fio_key(row.get("director")) for row in ready_rows if int(row.get("director_group_size") or 0) > 1 and base.valid_fio(row.get("director"))})})
    (output_dir / "company_director_enrichment_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary

def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich missing directors from Adata OR Kompra and build a read-only execution plan")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-companies", type=int, default=0)
    parser.add_argument("--workers", type=int, default=int(os.getenv("DIRECTOR_ENRICH_WORKERS", "6") or 6))
    parser.add_argument("--http-timeout", type=int, default=int(os.getenv("DIRECTOR_HTTP_TIMEOUT", "12") or 12))
    args = parser.parse_args()
    if args.max_companies < 0 or args.workers <= 0 or args.http_timeout <= 0:
        parser.error("max-companies must be >= 0; workers/http-timeout must be > 0")
    settings = Settings.from_env()
    client = BitrixClient(settings.bitrix_webhook_url or "", timeout=settings.bitrix_request_timeout, polite_delay_seconds=settings.bitrix_polite_delay_seconds, verify_ssl=settings.bitrix_tls_verify)
    print("[DIRECTOR] loading Bitrix companies/requisites/contacts", flush=True)
    snapshot = base.load_snapshot(client)
    candidates, skipped = base.build_candidates(snapshot)
    candidates = v2._filter_secondary_directors(client, candidates, skipped, snapshot, args.workers)
    candidates.sort(key=lambda item: int(item["company_id"]))
    if args.max_companies:
        candidates = candidates[: args.max_companies]
    print(f"[DIRECTOR] candidates={len(candidates)} skipped={len(skipped)}", flush=True)
    rows = v3.enrich_candidates_two_sources(candidates, min(args.workers, 12), args.http_timeout)
    rows = v2._annotate_director_groups(rows)
    print("[DIRECTOR] building read-only execution plan", flush=True)
    rows = build_dry_run_plan(client, rows, args.workers)
    if args.apply:
        ready_rows = [row for row in rows if row.get("plan_status") == PLAN_READY]
        blocked_or_other = [row for row in rows if row.get("plan_status") != PLAN_READY]
        applied_rows = v2.apply_rows_v2(client, ready_rows, args.workers)
        rows = sorted(blocked_or_other + applied_rows, key=lambda item: int(item["company_id"]))
    summary = write_report_v4(Path(args.output_dir), rows, skipped, args.apply)
    if args.apply and summary.get("plan_blocked"):
        summary["errors"] = int(summary.get("errors") or 0) + int(summary["plan_blocked"])
        (Path(args.output_dir) / "company_director_enrichment_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if args.apply and summary["errors"] else 0

if __name__ == "__main__":
    raise SystemExit(main())
