from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from common.bitrix import BitrixClient

LOG = logging.getLogger("lead_recovery")
DEFAULT_FAILURE_REASON_FIELD = os.getenv("BITRIX_LEAD_FAILURE_REASON_FIELD", "UF_CRM_1785508658316")
DEFAULT_TARGET_STATUS = "NEW"


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _utc_cutoff(days: int, now: datetime | None = None) -> str:
    if days < 1:
        raise ValueError("days must be >= 1")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat(timespec="seconds")


def resolve_moved_by_id(explicit_user_id: str | None, configured_user_id: str | None = None) -> int:
    """Resolve the user whose failed-stage moves must be reverted.

    Deliberately does NOT call user.current: an incoming webhook may have CRM scope only.
    Resolution order: workflow/CLI input -> repository variable LEAD_RECOVERY_MOVED_BY_ID.
    """
    raw = (explicit_user_id or "").strip() or (configured_user_id or "").strip()
    value = _as_int(raw)
    if not value or value <= 0:
        raise ValueError(
            "Bitrix user ID is required. Pass --moved-by-id or set LEAD_RECOVERY_MOVED_BY_ID repository variable."
        )
    return value


def find_failed_leads(
    client: BitrixClient,
    *,
    moved_by_id: int,
    cutoff_iso: str,
    failure_reason_field: str = DEFAULT_FAILURE_REASON_FIELD,
) -> list[dict[str, Any]]:
    """Return leads currently in a failed semantic stage, moved there by the chosen user after cutoff."""
    params = {
        "order": {"MOVED_TIME": "ASC", "ID": "ASC"},
        "filter": {
            "=STATUS_SEMANTIC_ID": "F",
            "=MOVED_BY_ID": moved_by_id,
            ">=MOVED_TIME": cutoff_iso,
        },
        "select": [
            "ID",
            "TITLE",
            "STATUS_ID",
            "STATUS_SEMANTIC_ID",
            "ASSIGNED_BY_ID",
            "MOVED_BY_ID",
            "MOVED_TIME",
            "DATE_MODIFY",
            failure_reason_field,
        ],
    }
    rows = client.list_all("crm.lead.list", params)
    return [dict(row) for row in rows]


def _is_empty_reason(value: Any) -> bool:
    return value in (None, "", [], (), False)


def _read_lead(client: BitrixClient, lead_id: str) -> dict[str, Any]:
    value = client.call("crm.lead.get", {"id": lead_id}) or {}
    return dict(value) if isinstance(value, dict) else {}


def recover_leads(
    client: BitrixClient,
    leads: list[dict[str, Any]],
    *,
    apply: bool,
    target_status: str = DEFAULT_TARGET_STATUS,
    failure_reason_field: str = DEFAULT_FAILURE_REASON_FIELD,
    stabilize_seconds: float = 12.0,
    repair_wait_seconds: float = 5.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Restore matching failed leads to NEW and preserve their original assignee.

    Apply mode is intentionally two-phase:
      1) move all selected leads to NEW, explicitly keeping ASSIGNED_BY_ID and clearing the failure reason;
      2) wait once for stage robots/automation to finish, then repair an assignee/reason changed by automation;
      3) wait once more after repairs and perform a final verification.

    This avoids claiming success before asynchronous Bitrix robots have had a chance to run.
    """
    report: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for lead in leads:
        lead_id = str(lead.get("ID") or "").strip()
        assignee = str(lead.get("ASSIGNED_BY_ID") or "").strip()
        row: dict[str, Any] = {
            "ID": lead_id,
            "TITLE": lead.get("TITLE") or "",
            "FROM_STATUS_ID": lead.get("STATUS_ID") or "",
            "TO_STATUS_ID": target_status,
            "ASSIGNED_BY_ID_BEFORE": assignee,
            "MOVED_BY_ID": str(lead.get("MOVED_BY_ID") or ""),
            "MOVED_TIME": lead.get("MOVED_TIME") or "",
            "FAILURE_REASON_FIELD": failure_reason_field,
            "FAILURE_REASON_BEFORE": lead.get(failure_reason_field),
            "FAILURE_REASON_AFTER": "",
            "automation_intervened": False,
            "repair_attempted": False,
            "action": "would_restore",
        }

        if not apply:
            report.append(row)
            continue

        if not lead_id:
            row["action"] = "error"
            row["error"] = "lead ID is empty"
            report.append(row)
            continue
        if not assignee or not _as_int(assignee):
            row["action"] = "error"
            row["error"] = "ASSIGNED_BY_ID is empty or invalid; lead was not changed"
            report.append(row)
            continue

        try:
            # Preserve the current executor explicitly in the same update. This protects against
            # synchronous/default assignment logic; asynchronous stage robots are handled below.
            update_fields = {
                "STATUS_ID": target_status,
                "ASSIGNED_BY_ID": assignee,
                failure_reason_field: "",
            }
            client.call("crm.lead.update", {"id": lead_id, "fields": update_fields})
            row["action"] = "updated_pending_verify"
            pending.append((lead, row))
        except Exception as exc:
            row["action"] = "error"
            row["error"] = str(exc)
        report.append(row)

    if not apply or not pending:
        return report

    if stabilize_seconds > 0:
        LOG.info("Waiting %.1f seconds for Bitrix stage automation before verification", stabilize_seconds)
        sleep_fn(stabilize_seconds)

    repaired_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for lead, row in pending:
        lead_id = str(row["ID"])
        expected_assignee = str(row["ASSIGNED_BY_ID_BEFORE"])
        try:
            check = _read_lead(client, lead_id)
            after_assignee = str(check.get("ASSIGNED_BY_ID") or "")
            after_status = str(check.get("STATUS_ID") or "")
            after_reason = check.get(failure_reason_field)
            row["ASSIGNED_BY_ID_AFTER_AUTOMATION"] = after_assignee
            row["STATUS_ID_AFTER_AUTOMATION"] = after_status
            row["FAILURE_REASON_AFTER_AUTOMATION"] = after_reason

            if after_status != target_status:
                row["action"] = "verify_failed"
                row["error"] = (
                    f"stage changed after Bitrix automation: {after_status!r}, expected {target_status!r}; "
                    "lead was not force-moved again"
                )
                continue

            repair_fields: dict[str, Any] = {}
            if after_assignee != expected_assignee:
                row["automation_intervened"] = True
                repair_fields["ASSIGNED_BY_ID"] = expected_assignee
            if not _is_empty_reason(after_reason):
                row["automation_intervened"] = True
                # null is used on the repair pass because some user-field types do not clear reliably with "".
                repair_fields[failure_reason_field] = None

            if repair_fields:
                row["repair_attempted"] = True
                client.call("crm.lead.update", {"id": lead_id, "fields": repair_fields})
                row["action"] = "repaired_pending_verify"
                repaired_rows.append((lead, row))
            else:
                row["action"] = "pending_final_verify"
        except Exception as exc:
            row["action"] = "error"
            row["error"] = str(exc)

    if repaired_rows and repair_wait_seconds > 0:
        LOG.info("Waiting %.1f seconds after repair before final verification", repair_wait_seconds)
        sleep_fn(repair_wait_seconds)

    # Final verification for every lead that survived the first verification. Even leads that did
    # not need repair are read again so delayed robots cannot be missed by an immediate check.
    for _lead, row in pending:
        if row.get("action") in {"error", "verify_failed"}:
            continue
        lead_id = str(row["ID"])
        expected_assignee = str(row["ASSIGNED_BY_ID_BEFORE"])
        try:
            check = _read_lead(client, lead_id)
            final_assignee = str(check.get("ASSIGNED_BY_ID") or "")
            final_status = str(check.get("STATUS_ID") or "")
            final_reason = check.get(failure_reason_field)
            row["ASSIGNED_BY_ID_AFTER"] = final_assignee
            row["STATUS_ID_AFTER"] = final_status
            row["FAILURE_REASON_AFTER"] = final_reason

            errors: list[str] = []
            if final_status != target_status:
                errors.append(f"stage={final_status!r}, expected {target_status!r}")
            if final_assignee != expected_assignee:
                errors.append(f"assignee={final_assignee!r}, expected {expected_assignee!r}")
            if not _is_empty_reason(final_reason):
                errors.append(f"failure reason not cleared: {final_reason!r}")

            if errors:
                row["action"] = "verify_failed"
                row["error"] = "; ".join(errors)
            else:
                row["action"] = "restored"
        except Exception as exc:
            row["action"] = "error"
            row["error"] = str(exc)

    return report


def write_outputs(output_dir: Path, *, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "lead_recovery_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "lead_recovery_rows.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"mode: {summary['mode']}",
        f"days: {summary['days']}",
        f"moved_by_id: {summary['moved_by_id']}",
        f"cutoff: {summary['cutoff']}",
        f"found: {summary['found']}",
        f"restored: {summary['restored']}",
        f"would_restore: {summary['would_restore']}",
        f"automation_intervened: {summary['automation_intervened']}",
        f"errors: {summary['errors']}",
        "",
    ]
    for row in rows:
        lines.append(
            f"#{row.get('ID')} | {row.get('TITLE')} | {row.get('FROM_STATUS_ID')} -> {row.get('TO_STATUS_ID')} | "
            f"assignee={row.get('ASSIGNED_BY_ID_BEFORE')} | moved={row.get('MOVED_TIME')} | {row.get('action')}"
            + (" | robot/automation changed fields; repaired" if row.get("automation_intervened") else "")
            + (f" | {row.get('error')}" if row.get("error") else "")
        )
    (output_dir / "lead_recovery_journal.txt").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore leads moved to a failed stage by one Bitrix user during the last N days."
    )
    parser.add_argument("--apply", action="store_true", help="Actually restore matching leads. Without this flag: dry-run.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7).")
    parser.add_argument(
        "--moved-by-id",
        default="",
        help="Bitrix user ID who moved leads to failure. If empty, LEAD_RECOVERY_MOVED_BY_ID must be set.",
    )
    parser.add_argument(
        "--failure-reason-field",
        default=DEFAULT_FAILURE_REASON_FIELD,
        help="Lead failure-reason user field to clear when restoring.",
    )
    parser.add_argument("--stabilize-seconds", type=float, default=12.0, help="Wait for stage robots before verification.")
    parser.add_argument("--repair-wait-seconds", type=float, default=5.0, help="Wait after repair before final verification.")
    parser.add_argument("--output-dir", default="output", help="Output directory.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.days < 1 or args.days > 90:
        parser.error("--days must be between 1 and 90")
    if args.stabilize_seconds < 0 or args.repair_wait_seconds < 0:
        parser.error("wait values must be >= 0")

    mover = resolve_moved_by_id(args.moved_by_id, os.getenv("LEAD_RECOVERY_MOVED_BY_ID"))
    client = BitrixClient.from_env("TARGET_BITRIX_WEBHOOK_URL")
    cutoff = _utc_cutoff(args.days)
    leads = find_failed_leads(
        client,
        moved_by_id=mover,
        cutoff_iso=cutoff,
        failure_reason_field=args.failure_reason_field,
    )
    rows = recover_leads(
        client,
        leads,
        apply=args.apply,
        target_status=DEFAULT_TARGET_STATUS,
        failure_reason_field=args.failure_reason_field,
        stabilize_seconds=args.stabilize_seconds,
        repair_wait_seconds=args.repair_wait_seconds,
    )

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "days": args.days,
        "moved_by_id": mover,
        "cutoff": cutoff,
        "target_status": DEFAULT_TARGET_STATUS,
        "failure_reason_field": args.failure_reason_field,
        "found": len(leads),
        "restored": sum(1 for row in rows if row.get("action") == "restored"),
        "would_restore": sum(1 for row in rows if row.get("action") == "would_restore"),
        "automation_intervened": sum(1 for row in rows if row.get("automation_intervened")),
        "errors": sum(1 for row in rows if row.get("action") in {"error", "verify_failed"}),
    }
    write_outputs(Path(args.output_dir), summary=summary, rows=rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
