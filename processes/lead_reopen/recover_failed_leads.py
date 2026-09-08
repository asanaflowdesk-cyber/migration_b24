from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common.bitrix import BitrixClient

LOG = logging.getLogger("lead_recovery")
DEFAULT_FAILURE_REASON_FIELD = os.getenv("BITRIX_LEAD_FAILURE_REASON_FIELD", "UF_CRM_1785508658316")


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


def resolve_moved_by_id(client: BitrixClient, explicit_user_id: str | None) -> int:
    if explicit_user_id and explicit_user_id.strip():
        value = _as_int(explicit_user_id)
        if not value or value <= 0:
            raise ValueError("moved-by-id must be a positive Bitrix user ID")
        return value

    current = client.call("user.current") or {}
    if isinstance(current, list):
        current = current[0] if current else {}
    value = _as_int(current.get("ID") if isinstance(current, dict) else None)
    if not value:
        raise RuntimeError("Cannot resolve current Bitrix user ID from webhook; pass --moved-by-id explicitly")
    return value


def find_failed_leads(
    client: BitrixClient,
    *,
    moved_by_id: int,
    cutoff_iso: str,
    failure_reason_field: str = DEFAULT_FAILURE_REASON_FIELD,
) -> list[dict[str, Any]]:
    """Return leads that are CURRENTLY failed and were moved to their current stage by user after cutoff."""
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


def recover_leads(
    client: BitrixClient,
    leads: list[dict[str, Any]],
    *,
    apply: bool,
    target_status: str = "NEW",
    failure_reason_field: str = DEFAULT_FAILURE_REASON_FIELD,
) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for lead in leads:
        lead_id = str(lead.get("ID") or "").strip()
        assignee = str(lead.get("ASSIGNED_BY_ID") or "").strip()
        row = {
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

        try:
            # Change the stage and clear the failure reason. ASSIGNED_BY_ID is intentionally
            # not sent, so the existing executor/owner remains unchanged.
            update_fields = {"STATUS_ID": target_status, failure_reason_field: ""}
            client.call("crm.lead.update", {"id": lead_id, "fields": update_fields})
            check = client.call("crm.lead.get", {"id": lead_id}) or {}
            after_assignee = str(check.get("ASSIGNED_BY_ID") or "") if isinstance(check, dict) else ""
            after_status = str(check.get("STATUS_ID") or "") if isinstance(check, dict) else ""
            after_reason = check.get(failure_reason_field) if isinstance(check, dict) else None
            row["ASSIGNED_BY_ID_AFTER"] = after_assignee
            row["STATUS_ID_AFTER"] = after_status
            row["FAILURE_REASON_AFTER"] = after_reason
            if after_status != target_status:
                row["action"] = "verify_failed"
                row["error"] = f"stage after update is {after_status!r}, expected {target_status!r}"
            elif after_assignee != assignee:
                row["action"] = "verify_failed"
                row["error"] = f"assignee changed from {assignee!r} to {after_assignee!r}"
            elif after_reason not in (None, "", [], ()):
                row["action"] = "verify_failed"
                row["error"] = f"failure reason was not cleared: {after_reason!r}"
            else:
                row["action"] = "restored"
        except Exception as exc:  # keep processing remaining leads and report every failure
            row["action"] = "error"
            row["error"] = str(exc)
        report.append(row)
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
        f"errors: {summary['errors']}",
        "",
    ]
    for row in rows:
        lines.append(
            f"#{row.get('ID')} | {row.get('TITLE')} | {row.get('FROM_STATUS_ID')} -> {row.get('TO_STATUS_ID')} | "
            f"assignee={row.get('ASSIGNED_BY_ID_BEFORE')} | moved={row.get('MOVED_TIME')} | {row.get('action')}"
            + (f" | {row.get('error')}" if row.get("error") else "")
        )
    (output_dir / "lead_recovery_journal.txt").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore leads moved to a failed stage by one Bitrix user during the last N days."
    )
    parser.add_argument("--apply", action="store_true", help="Actually move matching leads to NEW. Without this flag: dry-run.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7).")
    parser.add_argument("--moved-by-id", default="", help="Bitrix user ID who moved leads to failure. Empty = webhook user.current.")
    parser.add_argument("--target-status", default="NEW", help="Target lead status (default: NEW).")
    parser.add_argument(
        "--failure-reason-field",
        default=DEFAULT_FAILURE_REASON_FIELD,
        help="Lead failure-reason user field to clear when restoring.",
    )
    parser.add_argument("--output-dir", default="output", help="Output directory.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.days < 1 or args.days > 90:
        parser.error("--days must be between 1 and 90")

    client = BitrixClient.from_env("TARGET_BITRIX_WEBHOOK_URL")
    mover = resolve_moved_by_id(client, args.moved_by_id)
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
        target_status=args.target_status,
        failure_reason_field=args.failure_reason_field,
    )

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "days": args.days,
        "moved_by_id": mover,
        "cutoff": cutoff,
        "target_status": args.target_status,
        "failure_reason_field": args.failure_reason_field,
        "found": len(leads),
        "restored": sum(1 for row in rows if row.get("action") == "restored"),
        "would_restore": sum(1 for row in rows if row.get("action") == "would_restore"),
        "errors": sum(1 for row in rows if row.get("action") in {"error", "verify_failed"}),
    }
    write_outputs(Path(args.output_dir), summary=summary, rows=rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
