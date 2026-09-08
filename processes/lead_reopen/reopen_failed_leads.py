from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings


@dataclass(slots=True)
class Candidate:
    lead_id: str
    title: str
    assigned_by_id: str
    moved_by_id: str
    moved_time: str
    status_id: str
    action: str = "pending"
    note: str = ""


def _as_positive_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_cutoff(days: int, now: datetime | None = None) -> tuple[datetime, str]:
    if days <= 0:
        raise ValueError("days must be greater than zero")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    cutoff = current - timedelta(days=days)
    return cutoff, cutoff.isoformat(timespec="seconds")


def resolve_moved_by_user_id(client: BitrixClient, explicit_id: int | None) -> tuple[int, dict[str, Any] | None]:
    if explicit_id is not None:
        try:
            rows = client.list_all(
                "user.get",
                {
                    "filter": {"ID": explicit_id},
                    "select": ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "ACTIVE"],
                },
            )
            user = rows[0] if rows else None
        except Exception:
            user = None
        return explicit_id, user

    current = client.call("user.current")
    if not isinstance(current, dict):
        raise RuntimeError(
            "Не удалось определить пользователя вебхука. Укажите --moved-by-user-id "
            "или переменную BITRIX_REOPEN_MOVED_BY_ID."
        )
    user_id = _as_positive_id(current.get("ID"))
    if user_id is None:
        raise RuntimeError("user.current вернул некорректный ID пользователя")
    return user_id, current


def _status_map(client: BitrixClient) -> dict[str, dict[str, Any]]:
    rows = client.list_lead_statuses()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        status_id = str(row.get("STATUS_ID") or "").strip()
        if status_id:
            result[status_id] = row
    return result


def validate_statuses(client: BitrixClient, failed_status_id: str, target_status_id: str) -> None:
    statuses = _status_map(client)
    failed = statuses.get(failed_status_id)
    target = statuses.get(target_status_id)
    if failed is None:
        raise RuntimeError(f"Стадия провала {failed_status_id!r} не найдена в справочнике лидов")
    if target is None:
        raise RuntimeError(f"Целевая стадия {target_status_id!r} не найдена в справочнике лидов")

    failed_semantics = str(failed.get("SEMANTICS") or "").upper()
    target_semantics = str(target.get("SEMANTICS") or "").upper()
    if failed_semantics and failed_semantics != "F":
        raise RuntimeError(
            f"Стадия {failed_status_id!r} не является провальной: SEMANTICS={failed_semantics!r}"
        )
    if target_semantics == "F":
        raise RuntimeError(f"Целевая стадия {target_status_id!r} сама является провальной")


def build_candidate_filter(
    moved_by_user_id: int,
    failed_status_id: str,
    cutoff_iso: str,
) -> dict[str, Any]:
    return {
        "STATUS_ID": failed_status_id,
        "MOVED_BY_ID": moved_by_user_id,
        ">=MOVED_TIME": cutoff_iso,
    }


def load_candidates(
    client: BitrixClient,
    *,
    moved_by_user_id: int,
    failed_status_id: str,
    cutoff: datetime,
    cutoff_iso: str,
) -> list[Candidate]:
    rows = client.list_all(
        "crm.lead.list",
        {
            "order": {"MOVED_TIME": "ASC", "ID": "ASC"},
            "filter": build_candidate_filter(moved_by_user_id, failed_status_id, cutoff_iso),
            "select": [
                "ID",
                "TITLE",
                "STATUS_ID",
                "STATUS_SEMANTIC_ID",
                "ASSIGNED_BY_ID",
                "MOVED_BY_ID",
                "MOVED_TIME",
                "DATE_MODIFY",
            ],
        },
    )

    candidates: list[Candidate] = []
    for row in rows:
        lead_id = str(row.get("ID") or "").strip()
        if not lead_id:
            continue
        if str(row.get("STATUS_ID") or "").strip() != failed_status_id:
            continue
        if _as_positive_id(row.get("MOVED_BY_ID")) != moved_by_user_id:
            continue
        moved_time = _parse_datetime(row.get("MOVED_TIME"))
        if moved_time is not None and moved_time < cutoff:
            continue

        candidates.append(
            Candidate(
                lead_id=lead_id,
                title=str(row.get("TITLE") or "").strip(),
                assigned_by_id=str(row.get("ASSIGNED_BY_ID") or "").strip(),
                moved_by_id=str(row.get("MOVED_BY_ID") or "").strip(),
                moved_time=str(row.get("MOVED_TIME") or "").strip(),
                status_id=str(row.get("STATUS_ID") or "").strip(),
            )
        )
    return candidates


def _get_lead(client: BitrixClient, lead_id: str) -> dict[str, Any] | None:
    result = client.call("crm.lead.get", {"id": int(lead_id)})
    return result if isinstance(result, dict) else None


def _still_matches(
    lead: dict[str, Any] | None,
    *,
    moved_by_user_id: int,
    failed_status_id: str,
    cutoff: datetime,
) -> tuple[bool, str]:
    if not lead:
        return False, "lead_not_found"
    if str(lead.get("STATUS_ID") or "").strip() != failed_status_id:
        return False, "status_changed_before_apply"
    if _as_positive_id(lead.get("MOVED_BY_ID")) != moved_by_user_id:
        return False, "moved_by_changed_before_apply"
    moved_time = _parse_datetime(lead.get("MOVED_TIME"))
    if moved_time is not None and moved_time < cutoff:
        return False, "moved_time_outside_window"
    return True, ""


def apply_candidates(
    client: BitrixClient,
    candidates: Iterable[Candidate],
    *,
    moved_by_user_id: int,
    failed_status_id: str,
    target_status_id: str,
    cutoff: datetime,
    verify_delay_seconds: float = 3.0,
) -> list[Candidate]:
    rows = list(candidates)
    expected_assignees: dict[str, str] = {}

    for item in rows:
        current = _get_lead(client, item.lead_id)
        matches, reason = _still_matches(
            current,
            moved_by_user_id=moved_by_user_id,
            failed_status_id=failed_status_id,
            cutoff=cutoff,
        )
        if not matches:
            item.action = "skipped"
            item.note = reason
            continue

        # Snapshot the assignee immediately before the stage change. This is the
        # source of truth even if the dry-run report was generated earlier.
        current_assigned = str(current.get("ASSIGNED_BY_ID") or "").strip()
        item.assigned_by_id = current_assigned
        expected_assignees[item.lead_id] = current_assigned

        fields: dict[str, Any] = {"STATUS_ID": target_status_id}
        if _as_positive_id(current_assigned) is not None:
            # Explicitly resend the same assignee together with STATUS_ID so the
            # update itself never intentionally changes the responsible person.
            fields["ASSIGNED_BY_ID"] = int(current_assigned)

        client.update_lead(item.lead_id, fields)
        item.action = "moved_to_new"
        item.note = ""

    if expected_assignees and verify_delay_seconds > 0:
        time.sleep(verify_delay_seconds)

    # Stage robots can run asynchronously. Re-read every updated lead and repair
    # ASSIGNED_BY_ID if a robot changed it while entering NEW.
    for item in rows:
        if item.action != "moved_to_new":
            continue
        current = _get_lead(client, item.lead_id)
        if not current:
            item.action = "verification_failed"
            item.note = "lead_not_found_after_update"
            continue

        current_status = str(current.get("STATUS_ID") or "").strip()
        if current_status != target_status_id:
            item.action = "verification_failed"
            item.note = f"status_after_update={current_status or '<empty>'}"
            continue

        expected_assignee = expected_assignees.get(item.lead_id, "")
        actual_assignee = str(current.get("ASSIGNED_BY_ID") or "").strip()
        if expected_assignee and actual_assignee != expected_assignee:
            client.update_lead(
                item.lead_id,
                {
                    "ASSIGNED_BY_ID": int(expected_assignee),
                    "STATUS_ID": target_status_id,
                },
            )
            item.action = "moved_to_new_assignee_restored"
            item.note = f"assignee_restored_from={actual_assignee or '<empty>'}"

    return rows


def _user_display(user: dict[str, Any] | None, user_id: int) -> str:
    if not user:
        return str(user_id)
    parts = [
        str(user.get("LAST_NAME") or "").strip(),
        str(user.get("NAME") or "").strip(),
        str(user.get("SECOND_NAME") or "").strip(),
    ]
    name = " ".join(part for part in parts if part)
    return f"{name} (ID {user_id})" if name else str(user_id)


def write_reports(
    output_dir: Path,
    *,
    candidates: list[Candidate],
    mode: str,
    moved_by_user_id: int,
    moved_by_user: dict[str, Any] | None,
    failed_status_id: str,
    target_status_id: str,
    cutoff_iso: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "reopen_failed_leads.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "lead_id",
                "title",
                "assigned_by_id",
                "moved_by_id",
                "moved_time",
                "status_id",
                "action",
                "note",
            ],
        )
        writer.writeheader()
        for item in candidates:
            writer.writerow(
                {
                    "lead_id": item.lead_id,
                    "title": item.title,
                    "assigned_by_id": item.assigned_by_id,
                    "moved_by_id": item.moved_by_id,
                    "moved_time": item.moved_time,
                    "status_id": item.status_id,
                    "action": item.action,
                    "note": item.note,
                }
            )

    counts: dict[str, int] = {}
    for item in candidates:
        counts[item.action] = counts.get(item.action, 0) + 1

    summary = {
        "mode": mode,
        "moved_by_user_id": moved_by_user_id,
        "moved_by_user": _user_display(moved_by_user, moved_by_user_id),
        "failed_status_id": failed_status_id,
        "target_status_id": target_status_id,
        "cutoff": cutoff_iso,
        "candidate_count": len(candidates),
        "actions": counts,
    }
    (output_dir / "reopen_failed_leads_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        f"Режим: {mode}",
        f"Кто перевёл в провал: {_user_display(moved_by_user, moved_by_user_id)}",
        f"Окно: начиная с {cutoff_iso}",
        f"Провал: {failed_status_id} -> Новые: {target_status_id}",
        f"Найдено лидов: {len(candidates)}",
    ]
    for key in sorted(counts):
        lines.append(f"{key}: {counts[key]}")
    (output_dir / "reopen_failed_leads_journal.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def build_client() -> BitrixClient:
    settings = Settings.from_env()
    if not settings.bitrix_webhook_url:
        raise RuntimeError("Не задан TARGET_BITRIX_WEBHOOK_URL")
    return BitrixClient(
        settings.bitrix_webhook_url,
        timeout=settings.bitrix_request_timeout,
        verify_ssl=settings.bitrix_tls_verify,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Вернуть в NEW лиды, которые указанный пользователь перевёл в провальную "
            "стадию за последние N дней, сохранив текущего ответственного."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Выполнить изменения; без флага только отчёт")
    parser.add_argument("--days", type=int, default=7, help="Глубина поиска в днях; по умолчанию 7")
    parser.add_argument(
        "--moved-by-user-id",
        type=int,
        default=None,
        help="ID пользователя, который перевёл лид в провал. Пусто = пользователь вебхука.",
    )
    parser.add_argument("--failed-status-id", default="JUNK", help="ID провальной стадии; по умолчанию JUNK")
    parser.add_argument("--target-status-id", default="NEW", help="ID целевой стадии; по умолчанию NEW")
    parser.add_argument("--verify-delay", type=float, default=3.0, help="Пауза перед проверкой ответственных после роботов")
    parser.add_argument("--output-dir", default="output", help="Каталог отчёта")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = build_client()

    explicit_user_id = args.moved_by_user_id
    if explicit_user_id is None:
        explicit_user_id = _as_positive_id(os.getenv("BITRIX_REOPEN_MOVED_BY_ID"))

    moved_by_user_id, moved_by_user = resolve_moved_by_user_id(client, explicit_user_id)
    failed_status_id = str(args.failed_status_id).strip()
    target_status_id = str(args.target_status_id).strip()
    validate_statuses(client, failed_status_id, target_status_id)

    cutoff, cutoff_iso = _iso_cutoff(args.days)
    candidates = load_candidates(
        client,
        moved_by_user_id=moved_by_user_id,
        failed_status_id=failed_status_id,
        cutoff=cutoff,
        cutoff_iso=cutoff_iso,
    )

    mode = "apply" if args.apply else "dry_run"
    if args.apply:
        candidates = apply_candidates(
            client,
            candidates,
            moved_by_user_id=moved_by_user_id,
            failed_status_id=failed_status_id,
            target_status_id=target_status_id,
            cutoff=cutoff,
            verify_delay_seconds=max(0.0, float(args.verify_delay)),
        )
    else:
        for item in candidates:
            item.action = "would_move_to_new"

    write_reports(
        Path(args.output_dir),
        candidates=candidates,
        mode=mode,
        moved_by_user_id=moved_by_user_id,
        moved_by_user=moved_by_user,
        failed_status_id=failed_status_id,
        target_status_id=target_status_id,
        cutoff_iso=cutoff_iso,
    )

    print(f"Пользователь перехода: {_user_display(moved_by_user, moved_by_user_id)}")
    print(f"Найдено: {len(candidates)}")
    if args.apply:
        changed = sum(1 for item in candidates if item.action.startswith("moved_to_new"))
        failed = sum(1 for item in candidates if item.action == "verification_failed")
        skipped = sum(1 for item in candidates if item.action == "skipped")
        print(f"Перенесено в {target_status_id}: {changed}; пропущено: {skipped}; ошибки проверки: {failed}")
        return 1 if failed else 0
    print("Dry-run: изменения не выполнялись")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
