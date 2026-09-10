from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from eqazyna_bitrix.bitrix_client import BitrixClient, BitrixError
from eqazyna_bitrix.scraper import EqazynaScraper
from eqazyna_bitrix.settings import Settings

DEFAULT_DOC_TYPE = "Заявка на разведку ТПИ"
DEFAULT_FAILURE_STAGE = "Провал"
DEFAULT_POTENTIAL_STAGE = "Потенциальные сделки"
DEFAULT_ORIGINATORS = ("EQAZYNA_LEAD", "EQAZYNA")
FAILURE_EXTERNAL_STATUSES = {"отклонено", "отозвано", "аннулировано"}
POTENTIAL_EXTERNAL_STATUSES = {"выдана лицензия"}

DOC_NUMBER_RE = re.compile(r"\b\d{3,}-[A-ZА-ЯЁ0-9]+\b", re.IGNORECASE)
COMPOSITE_ORIGIN_RE = re.compile(r"^eQazyna\|([^|]+)\|", re.IGNORECASE)


@dataclass(slots=True)
class SyncRow:
    lead_id: str
    doc_number: str | None
    eqazyna_status: str | None
    old_status_id: str | None
    old_status_name: str | None
    target_status_id: str | None
    target_status_name: str | None
    action: str
    error: str | None = None


@dataclass(slots=True)
class SyncSummary:
    mode: str
    leads_discovered: int = 0
    leads_with_application_number: int = 0
    applications_found: int = 0
    no_rule: int = 0
    already_in_target_stage: int = 0
    changes_planned: int = 0
    changes_applied: int = 0
    application_not_found: int = 0
    ambiguous_application_number: int = 0
    errors: int = 0
    failure_stage_id: str | None = None
    failure_stage_name: str | None = None
    potential_stage_id: str | None = None
    potential_stage_name: str | None = None


class StatusSyncError(RuntimeError):
    pass


def normalise(value: object) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text


def status_id(row: dict[str, Any]) -> str:
    return str(row.get("STATUS_ID") or row.get("ID") or "").strip()


def status_name(row: dict[str, Any]) -> str:
    return str(row.get("NAME") or "").strip()


def resolve_lead_stage(
    statuses: Iterable[dict[str, Any]],
    requested_name: str,
    *,
    aliases: Iterable[str] = (),
) -> tuple[str, str]:
    rows = [row for row in statuses if status_id(row)]
    wanted = [requested_name, *aliases]

    for candidate in wanted:
        key = normalise(candidate)
        if not key:
            continue
        matches = [row for row in rows if normalise(status_name(row)) == key]
        if len(matches) == 1:
            row = matches[0]
            return status_id(row), status_name(row)
        if len(matches) > 1:
            raise StatusSyncError(
                f"В Bitrix24 найдено несколько стадий с названием {candidate!r}: "
                + ", ".join(status_id(row) for row in matches)
            )

    available = ", ".join(
        f"{status_id(row)}={status_name(row)!r}" for row in rows
    )
    raise StatusSyncError(
        f"Не найдена стадия лида {requested_name!r}. Доступные стадии: {available}"
    )


def map_external_status(eqazyna_status: str) -> str | None:
    key = normalise(eqazyna_status)
    if key in FAILURE_EXTERNAL_STATUSES:
        return "failure"
    if key in POTENTIAL_EXTERNAL_STATUSES:
        return "potential"
    return None


def extract_application_numbers(lead: dict[str, Any]) -> list[str]:
    numbers: list[str] = []

    origin_id = str(lead.get("ORIGIN_ID") or "").strip()
    if origin_id:
        composite = COMPOSITE_ORIGIN_RE.match(origin_id)
        if composite:
            numbers.append(composite.group(1).strip())
        elif DOC_NUMBER_RE.fullmatch(origin_id):
            numbers.append(origin_id)

    for field in ("TITLE", "COMMENTS"):
        text = str(lead.get(field) or "")
        numbers.extend(match.group(0) for match in DOC_NUMBER_RE.finditer(text))

    result: list[str] = []
    seen: set[str] = set()
    for number in numbers:
        cleaned = number.strip().upper()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def list_eqazyna_leads(client: BitrixClient, originators: Iterable[str]) -> list[dict[str, Any]]:
    select = [
        "ID",
        "TITLE",
        "COMMENTS",
        "STATUS_ID",
        "STATUS_SEMANTIC_ID",
        "ORIGINATOR_ID",
        "ORIGIN_ID",
    ]
    by_id: dict[str, dict[str, Any]] = {}
    for originator in originators:
        originator = str(originator or "").strip()
        if not originator:
            continue
        rows = client.list_all(
            "crm.lead.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ORIGINATOR_ID": originator},
                "select": select,
            },
        )
        for row in rows:
            lead_id = str(row.get("ID") or "").strip()
            if lead_id:
                by_id[lead_id] = row
    return sorted(by_id.values(), key=lambda row: int(str(row.get("ID") or "0")))


def _stage_name_map(statuses: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {status_id(row): status_name(row) for row in statuses if status_id(row)}


def sync_statuses(
    *,
    client: BitrixClient,
    scraper: EqazynaScraper,
    mode: str,
    doc_type: str,
    failure_stage_name: str,
    potential_stage_name: str,
    originators: Iterable[str] = DEFAULT_ORIGINATORS,
    max_items: int = 0,
) -> tuple[SyncSummary, list[SyncRow]]:
    if mode not in {"dry_run", "apply"}:
        raise ValueError(f"unsupported mode: {mode}")

    statuses = client.list_lead_statuses()
    failure_id, failure_resolved_name = resolve_lead_stage(
        statuses,
        failure_stage_name,
    )
    potential_id, potential_resolved_name = resolve_lead_stage(
        statuses,
        potential_stage_name,
        aliases=("Потенциальная сделка", "Потенциальные сделки"),
    )
    names = _stage_name_map(statuses)

    summary = SyncSummary(
        mode=mode,
        failure_stage_id=failure_id,
        failure_stage_name=failure_resolved_name,
        potential_stage_id=potential_id,
        potential_stage_name=potential_resolved_name,
    )
    rows: list[SyncRow] = []
    leads = list_eqazyna_leads(client, originators)
    if max_items > 0:
        leads = leads[:max_items]
    summary.leads_discovered = len(leads)

    status_cache: dict[str, str | None] = {}
    fetch_error_cache: dict[str, str] = {}

    for index, lead in enumerate(leads, start=1):
        lead_id = str(lead.get("ID") or "")
        old_id = str(lead.get("STATUS_ID") or "") or None
        old_name = names.get(old_id or "") or None
        application_numbers = extract_application_numbers(lead)

        if not application_numbers:
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=None,
                    eqazyna_status=None,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=None,
                    target_status_name=None,
                    action="skipped_no_application_number",
                )
            )
            continue
        if len(application_numbers) != 1:
            summary.ambiguous_application_number += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=", ".join(application_numbers),
                    eqazyna_status=None,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=None,
                    target_status_name=None,
                    action="skipped_ambiguous_application_number",
                    error="У лида найдено несколько номеров заявок; автоматическое изменение запрещено.",
                )
            )
            continue

        doc_number = application_numbers[0]
        summary.leads_with_application_number += 1
        print(f"[{index}/{len(leads)}] lead={lead_id} application={doc_number}", flush=True)

        if doc_number in fetch_error_cache:
            summary.errors += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=doc_number,
                    eqazyna_status=None,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=None,
                    target_status_name=None,
                    action="error",
                    error=fetch_error_cache[doc_number],
                )
            )
            continue

        if doc_number not in status_cache:
            try:
                application = scraper.fetch_application_by_number(doc_number, doc_type)
                status_cache[doc_number] = application.status if application else None
            except Exception as exc:  # noqa: BLE001 - journal every failed lookup
                message = f"e-Qazyna lookup failed: {exc}"
                fetch_error_cache[doc_number] = message
                summary.errors += 1
                rows.append(
                    SyncRow(
                        lead_id=lead_id,
                        doc_number=doc_number,
                        eqazyna_status=None,
                        old_status_id=old_id,
                        old_status_name=old_name,
                        target_status_id=None,
                        target_status_name=None,
                        action="error",
                        error=message,
                    )
                )
                continue

        external_status = status_cache[doc_number]
        if not external_status:
            summary.application_not_found += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=doc_number,
                    eqazyna_status=None,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=None,
                    target_status_name=None,
                    action="application_not_found",
                )
            )
            continue

        summary.applications_found += 1
        rule = map_external_status(external_status)
        if rule is None:
            summary.no_rule += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=doc_number,
                    eqazyna_status=external_status,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=None,
                    target_status_name=None,
                    action="no_change_for_external_status",
                )
            )
            continue

        if rule == "failure":
            target_id, target_name = failure_id, failure_resolved_name
        else:
            target_id, target_name = potential_id, potential_resolved_name

        if old_id == target_id:
            summary.already_in_target_stage += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=doc_number,
                    eqazyna_status=external_status,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=target_id,
                    target_status_name=target_name,
                    action="already_in_target_stage",
                )
            )
            continue

        if mode == "dry_run":
            summary.changes_planned += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=doc_number,
                    eqazyna_status=external_status,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=target_id,
                    target_status_name=target_name,
                    action="would_update",
                )
            )
            continue

        try:
            client.update_lead(lead_id, {"STATUS_ID": target_id})
        except (BitrixError, OSError, ValueError) as exc:
            summary.errors += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=doc_number,
                    eqazyna_status=external_status,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=target_id,
                    target_status_name=target_name,
                    action="error",
                    error=f"Bitrix24 update failed: {exc}",
                )
            )
            continue

        summary.changes_applied += 1
        rows.append(
            SyncRow(
                lead_id=lead_id,
                doc_number=doc_number,
                eqazyna_status=external_status,
                old_status_id=old_id,
                old_status_name=old_name,
                target_status_id=target_id,
                target_status_name=target_name,
                action="updated",
            )
        )

    return summary, rows


def write_outputs(output_dir: Path, summary: SyncSummary, rows: list[SyncRow]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "eqazyna_status_sync_summary.json").write_text(
        json.dumps(asdict(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "eqazyna_status_sync_results.json").write_text(
        json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fieldnames = list(SyncRow.__dataclass_fields__)
    with (output_dir / "eqazyna_status_sync_results.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)

    journal_lines = [
        "e-Qazyna -> Bitrix24 status sync",
        f"mode: {summary.mode}",
        f"failure stage: {summary.failure_stage_name} ({summary.failure_stage_id})",
        f"potential stage: {summary.potential_stage_name} ({summary.potential_stage_id})",
        f"leads discovered: {summary.leads_discovered}",
        f"applications found: {summary.applications_found}",
        f"changes planned: {summary.changes_planned}",
        f"changes applied: {summary.changes_applied}",
        f"already target: {summary.already_in_target_stage}",
        f"no rule: {summary.no_rule}",
        f"not found: {summary.application_not_found}",
        f"ambiguous: {summary.ambiguous_application_number}",
        f"errors: {summary.errors}",
        "",
    ]
    for row in rows:
        journal_lines.append(
            f"lead={row.lead_id} | application={row.doc_number or '-'} | "
            f"e-Qazyna={row.eqazyna_status or '-'} | "
            f"Bitrix={row.old_status_name or row.old_status_id or '-'} -> "
            f"{row.target_status_name or row.target_status_id or '-'} | "
            f"action={row.action}"
            + (f" | error={row.error}" if row.error else "")
        )
    (output_dir / "eqazyna_status_sync_journal.txt").write_text(
        "\n".join(journal_lines) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Сверяет существующие e-Qazyna лиды Bitrix24 по точному номеру заявки: "
            "Отклонено/Отозвано/Аннулировано -> Провал; "
            "Выдана лицензия -> Потенциальные сделки."
        )
    )
    parser.add_argument("--mode", choices=("dry_run", "apply"), default="dry_run")
    parser.add_argument("--doc-type", default=DEFAULT_DOC_TYPE)
    parser.add_argument("--failure-stage-name", default=DEFAULT_FAILURE_STAGE)
    parser.add_argument("--potential-stage-name", default=DEFAULT_POTENTIAL_STAGE)
    parser.add_argument("--originators", default=",".join(DEFAULT_ORIGINATORS))
    parser.add_argument("--max-items", type=int, default=0, help="0 = все найденные e-Qazyna лиды")
    parser.add_argument("--output-dir", default="output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_items < 0:
        raise SystemExit("--max-items must be >= 0")

    settings = Settings.from_env()
    if not settings.bitrix_webhook_url:
        raise SystemExit("TARGET_BITRIX_WEBHOOK_URL is required")

    client = BitrixClient(
        webhook_url=settings.bitrix_webhook_url,
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )
    scraper = EqazynaScraper(
        timeout=settings.eqazyna_request_timeout,
        polite_delay_seconds=settings.polite_delay_seconds,
        max_retries=settings.eqazyna_max_retries,
        retry_base_sleep_seconds=settings.eqazyna_retry_base_sleep_seconds,
    )
    originators = [value.strip() for value in args.originators.split(",") if value.strip()]

    try:
        summary, rows = sync_statuses(
            client=client,
            scraper=scraper,
            mode=args.mode,
            doc_type=args.doc_type,
            failure_stage_name=args.failure_stage_name,
            potential_stage_name=args.potential_stage_name,
            originators=originators,
            max_items=args.max_items,
        )
    except (StatusSyncError, BitrixError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    write_outputs(Path(args.output_dir), summary, rows)
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2), flush=True)
    if summary.errors:
        print(
            f"ERROR: status sync incomplete; per-lead errors={summary.errors}. See output journal.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
