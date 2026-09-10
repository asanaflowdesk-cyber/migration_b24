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
DEFAULT_FAILURE_REASON_FIELD = "UF_CRM_1785508658316"

# The enum IDs below are the actual values of the Bitrix24 field
# UF_CRM_1785508658316 supplied for this portal.  The external e-Qazyna status
# determines both the CRM stage and the value written to this field.
STATUS_RULES: dict[str, dict[str, str]] = {
    "аннулировано": {
        "stage": "failure",
        "reason_id": "66",
        "reason_name": "Заявка аннулирована на сайте",
    },
    "отозвано": {
        "stage": "failure",
        "reason_id": "67",
        "reason_name": "Заявка отменена на сайте",
    },
    "отклонено": {
        "stage": "failure",
        "reason_id": "68",
        "reason_name": "Заявка отклонена на сайте",
    },
    "выдана лицензия": {
        "stage": "potential",
        "reason_id": "69",
        "reason_name": "По заявке уже выдана лицензия",
    },
}

TITLE_NUMBER_MARKER = "№ "


@dataclass(slots=True)
class SyncRow:
    lead_id: str
    doc_number: str | None
    eqazyna_status: str | None
    old_status_id: str | None
    old_status_name: str | None
    target_status_id: str | None
    target_status_name: str | None
    old_failure_reason: str | None
    target_failure_reason_id: str | None
    target_failure_reason_name: str | None
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
    leads_without_title_number: int = 0
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
    rule = STATUS_RULES.get(key)
    return rule["stage"] if rule else None


def extract_application_number(lead: dict[str, Any]) -> str | None:
    """Return the one e-Qazyna application number stored in the lead title.

    Business rule: one lead is always one application.  The application number
    is not reconstructed from ORIGIN_ID/comments and is not searched with a
    loose regex.  Everything after the literal ``№ `` marker in TITLE is the
    application number.
    """
    title = str(lead.get("TITLE") or "")
    marker_index = title.rfind(TITLE_NUMBER_MARKER)
    if marker_index < 0:
        return None
    number = title[marker_index + len(TITLE_NUMBER_MARKER) :].strip()
    return number or None


def list_eqazyna_leads(client: BitrixClient, originators: Iterable[str]) -> list[dict[str, Any]]:
    select = [
        "ID",
        "TITLE",
        "COMMENTS",
        "STATUS_ID",
        "STATUS_SEMANTIC_ID",
        "ORIGINATOR_ID",
        "ORIGIN_ID",
        DEFAULT_FAILURE_REASON_FIELD,
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
    # Also recover migrated/manual e-Qazyna cards by their title.  The title is
    # the source of truth for the application number, so ORIGINATOR_ID must not
    # be a hard dependency for status synchronisation.
    rows = client.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"%TITLE": "e-Qazyna"},
            "select": select,
        },
    )
    for row in rows:
        lead_id = str(row.get("ID") or "").strip()
        if lead_id and TITLE_NUMBER_MARKER in str(row.get("TITLE") or ""):
            by_id[lead_id] = row

    return sorted(by_id.values(), key=lambda row: int(str(row.get("ID") or "0")))


def scalar_value(value: Any) -> str | None:
    """Normalise Bitrix scalar/enum response shapes to one comparable value."""
    if value in (None, ""):
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            result = scalar_value(item)
            if result:
                return result
        return None
    if isinstance(value, dict):
        for key in ("ID", "id", "VALUE", "value"):
            if key in value:
                result = scalar_value(value.get(key))
                if result:
                    return result
        return None
    return str(value).strip() or None


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
        doc_number = extract_application_number(lead)
        old_failure_reason = scalar_value(lead.get(DEFAULT_FAILURE_REASON_FIELD))

        if not doc_number:
            summary.leads_without_title_number += 1
            rows.append(
                SyncRow(
                    lead_id=lead_id,
                    doc_number=None,
                    eqazyna_status=None,
                    old_status_id=old_id,
                    old_status_name=old_name,
                    target_status_id=None,
                    target_status_name=None,
                    old_failure_reason=old_failure_reason,
                    target_failure_reason_id=None,
                    target_failure_reason_name=None,
                    action="skipped_no_application_number",
                    error="В TITLE не найден номер после маркера '№ '.",
                )
            )
            continue

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
                    old_failure_reason=old_failure_reason,
                    target_failure_reason_id=None,
                    target_failure_reason_name=None,
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
                        old_failure_reason=old_failure_reason,
                        target_failure_reason_id=None,
                        target_failure_reason_name=None,
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
                    old_failure_reason=old_failure_reason,
                    target_failure_reason_id=None,
                    target_failure_reason_name=None,
                    action="application_not_found",
                )
            )
            continue

        summary.applications_found += 1
        rule_key = normalise(external_status)
        rule = STATUS_RULES.get(rule_key)
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
                    old_failure_reason=old_failure_reason,
                    target_failure_reason_id=None,
                    target_failure_reason_name=None,
                    action="no_change_for_external_status",
                )
            )
            continue

        if rule["stage"] == "failure":
            target_id, target_name = failure_id, failure_resolved_name
        else:
            target_id, target_name = potential_id, potential_resolved_name

        target_reason_id = rule["reason_id"]
        target_reason_name = rule["reason_name"]
        stage_matches = old_id == target_id
        reason_matches = old_failure_reason == target_reason_id

        if stage_matches and reason_matches:
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
                    old_failure_reason=old_failure_reason,
                    target_failure_reason_id=target_reason_id,
                    target_failure_reason_name=target_reason_name,
                    action="already_synced",
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
                    old_failure_reason=old_failure_reason,
                    target_failure_reason_id=target_reason_id,
                    target_failure_reason_name=target_reason_name,
                    action="would_update",
                )
            )
            continue

        try:
            client.update_lead(
                lead_id,
                {
                    "STATUS_ID": target_id,
                    DEFAULT_FAILURE_REASON_FIELD: target_reason_id,
                },
            )
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
                    old_failure_reason=old_failure_reason,
                    target_failure_reason_id=target_reason_id,
                    target_failure_reason_name=target_reason_name,
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
                old_failure_reason=old_failure_reason,
                target_failure_reason_id=target_reason_id,
                target_failure_reason_name=target_reason_name,
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
        f"failure reason field: {DEFAULT_FAILURE_REASON_FIELD}",
        f"failure stage: {summary.failure_stage_name} ({summary.failure_stage_id})",
        f"potential stage: {summary.potential_stage_name} ({summary.potential_stage_id})",
        f"leads discovered: {summary.leads_discovered}",
        f"leads with application number in TITLE: {summary.leads_with_application_number}",
        f"leads without '№ ' in TITLE: {summary.leads_without_title_number}",
        f"applications found: {summary.applications_found}",
        f"changes planned: {summary.changes_planned}",
        f"changes applied: {summary.changes_applied}",
        f"already synced: {summary.already_in_target_stage}",
        f"no rule: {summary.no_rule}",
        f"not found: {summary.application_not_found}",
        f"errors: {summary.errors}",
        "",
    ]
    for row in rows:
        journal_lines.append(
            f"lead={row.lead_id} | application={row.doc_number or '-'} | "
            f"e-Qazyna={row.eqazyna_status or '-'} | "
            f"Bitrix={row.old_status_name or row.old_status_id or '-'} -> "
            f"{row.target_status_name or row.target_status_id or '-'} | "
            f"reason={row.old_failure_reason or '-'} -> "
            f"{row.target_failure_reason_id or '-'}"
            + (
                f" ({row.target_failure_reason_name})"
                if row.target_failure_reason_name
                else ""
            )
            + " | "
            f"action={row.action}"
            + (f" | error={row.error}" if row.error else "")
        )
    (output_dir / "eqazyna_status_sync_journal.txt").write_text(
        "\n".join(journal_lines) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Сверяет существующие e-Qazyna лиды Bitrix24. Номер заявки берётся только "
            "из TITLE как всё после '№ '. "
            "Отклонено/Отозвано/Аннулировано -> Провал; "
            "Выдана лицензия -> Потенциальные сделки. Одновременно записывает "
            "соответствующую причину в UF_CRM_1785508658316."
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
