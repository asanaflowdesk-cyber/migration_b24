from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import director_frozen_plan as frozen
import director_frozen_plan_hotfix as reliable
import enrich_missing_directors as base
import enrich_missing_directors_v2 as v2
import enrich_missing_directors_v3 as v3
import enrich_missing_directors_v4 as report
from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings


MANAGED_MARKERS = ("DIRECTOR_PLAN_ID:", "EQAZYNA_DIRECTOR:")


def _client() -> BitrixClient:
    settings = Settings.from_env()
    return BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )


def _director_values(contacts: list[dict[str, Any]]) -> set[str]:
    return {
        base.normalize_fio(base.contact_fio(item))
        for item in contacts
        if base.is_director_contact(item)
        and base.valid_fio(base.contact_fio(item))
    }


def _managed_director_contact(contacts: list[dict[str, Any]]) -> bool:
    for item in contacts:
        if not base.is_director_contact(item):
            continue
        comments = str(item.get("COMMENTS") or "")
        if any(marker in comments for marker in MANAGED_MARKERS):
            return True
    return False


def _secondary_directors_for_missing(
    client: BitrixClient,
    company_ids: list[int],
    workers: int,
) -> dict[int, list[dict[str, Any]]]:
    """Read secondary links only for companies with no known director."""
    result: dict[int, list[dict[str, Any]]] = {}

    def read(company_id: int) -> tuple[int, list[dict[str, Any]]]:
        local = base.clone_client(client)
        contacts = v2._linked_contacts_for_company(local, company_id)
        directors = [
            item
            for item in contacts
            if base.is_director_contact(item)
            and base.valid_fio(base.contact_fio(item))
        ]
        return company_id, directors

    with ThreadPoolExecutor(
        max_workers=max(1, min(workers, 8)),
        thread_name_prefix="director-secondary-recovery",
    ) as pool:
        futures = {pool.submit(read, company_id): company_id for company_id in company_ids}
        for future in as_completed(futures):
            company_id, contacts = future.result()
            if contacts:
                result[company_id] = contacts
    return result


def build_internal_repair_rows(
    client: BitrixClient,
    snapshot: dict[str, list[dict[str, Any]]],
    workers: int,
) -> tuple[list[dict[str, Any]], set[int], set[int]]:
    """Recover only partial work previously created by workflow 35.

    Historical/manual director data is left untouched. Any company that already
    has director information in Bitrix is excluded from public lookup. Only
    records carrying a workflow-35 marker are repaired automatically.
    """
    requisites_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    contacts_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    companies: dict[int, dict[str, Any]] = {}

    for row in snapshot.get("requisites", []):
        company_id = base.normalize_id(row.get("ENTITY_ID"))
        if company_id is not None:
            requisites_by_company[company_id].append(row)
    for row in snapshot.get("contacts", []):
        company_id = base.normalize_id(row.get("COMPANY_ID"))
        if company_id is not None:
            contacts_by_company[company_id].append(row)
    for row in snapshot.get("companies", []):
        company_id = base.normalize_id(row.get("ID"))
        if company_id is not None:
            companies[company_id] = row

    no_known_director: list[int] = []
    primary_directors: dict[int, list[dict[str, Any]]] = {}
    req_directors: dict[int, set[str]] = {}

    for company_id in companies:
        reqs = requisites_by_company.get(company_id, [])
        contacts = contacts_by_company.get(company_id, [])
        req_values = {
            base.normalize_fio(item.get("RQ_DIRECTOR"))
            for item in reqs
            if base.valid_fio(item.get("RQ_DIRECTOR"))
        }
        director_contacts = [
            item
            for item in contacts
            if base.is_director_contact(item)
            and base.valid_fio(base.contact_fio(item))
        ]
        req_directors[company_id] = req_values
        primary_directors[company_id] = director_contacts
        if not req_values and not director_contacts:
            no_known_director.append(company_id)

    secondary = _secondary_directors_for_missing(
        client,
        no_known_director,
        workers,
    )

    rows: list[dict[str, Any]] = []
    handled: set[int] = set()
    conflicts: set[int] = set()

    for company_id in sorted(companies):
        company = companies[company_id]
        reqs = requisites_by_company.get(company_id, [])
        contacts = primary_directors.get(company_id, []) + secondary.get(company_id, [])
        values = set(req_directors.get(company_id, set())) | _director_values(contacts)
        if not values:
            continue

        # Existing director data means this company is outside enrichment scope.
        handled.add(company_id)
        if len(values) != 1:
            # Existing historical conflict is not workflow 35's job. Exclude it
            # from public lookup and from the execution plan; do not block others.
            conflicts.add(company_id)
            continue

        # Repair only workflow-owned contacts. RQ-only and ordinary/manual
        # contacts are deliberately not expanded or rewritten.
        if not _managed_director_contact(contacts):
            continue

        director = next(iter(values))
        rows.append(
            {
                "company_id": company_id,
                "title": str(company.get("TITLE") or ""),
                "bin": base.current_bin(company, reqs),
                "director": director,
                "source": "bitrix",
                "confidence": "internal_repair",
                "status": "accepted",
                "evidence": "Bitrix workflow-35 managed director",
                "url": "",
                "adata_status": "not_checked",
                "adata_director": "",
                "adata_url": "",
                "kompra_status": "not_checked",
                "kompra_director": "",
                "kompra_url": "",
            }
        )

    return rows, handled, conflicts


def _external_rows(
    client: BitrixClient,
    snapshot: dict[str, list[dict[str, Any]]],
    excluded_company_ids: set[int],
    workers: int,
    http_timeout: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates, skipped = base.build_candidates(snapshot)
    candidates = [
        row
        for row in candidates
        if int(row["company_id"]) not in excluded_company_ids
    ]
    skipped = [
        row
        for row in skipped
        if int(row.get("company_id") or 0) not in excluded_company_ids
    ]

    print(
        f"[DIRECTOR] external candidates={len(candidates)}; "
        f"known/excluded={len(excluded_company_ids)}",
        flush=True,
    )
    enriched = v3.enrich_candidates_two_sources(
        candidates,
        min(workers, 12),
        http_timeout,
    )
    return frozen.mark_source_unavailable(enriched), skipped


def build_live_plan(
    client: BitrixClient,
    *,
    workers: int,
    http_timeout: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    print("[DIRECTOR] loading Bitrix snapshot", flush=True)
    snapshot = base.load_snapshot(client)

    internal_rows, handled_ids, conflict_ids = build_internal_repair_rows(
        client,
        snapshot,
        workers,
    )
    external_rows, skipped = _external_rows(
        client,
        snapshot,
        handled_ids | conflict_ids,
        workers,
        http_timeout,
    )

    seed_rows = v2._annotate_director_groups(internal_rows + external_rows)
    print(
        f"[DIRECTOR] building live plan rows={len(seed_rows)} "
        f"repair={len(internal_rows)} external={len(external_rows)}",
        flush=True,
    )
    rows = frozen.build_exact_dry_run_plan(client, seed_rows, workers)

    run_id = str(os.getenv("GITHUB_RUN_ID") or "LOCAL")
    sha = str(os.getenv("GITHUB_SHA") or "LOCAL")
    plan = {
        "schema_version": 0,
        "plan_id": f"LIVE-{run_id}",
        "source_run_id": run_id,
        "source_sha": sha,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "skipped": skipped,
        "mode": "live_self_contained",
    }
    return plan, rows, skipped


def _mark_group_preflight_failed(
    applied_by_company: dict[int, dict[str, Any]],
    group_rows: list[dict[str, Any]],
) -> None:
    for row in group_rows:
        item = dict(row)
        item["apply_status"] = "SKIPPED"
        item["verification_status"] = "PREFLIGHT_FAILED"
        applied_by_company[int(item["company_id"])] = item


def apply_live_plan_resilient(
    client: BitrixClient,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Preflight and apply each independent director group separately.

    One changed/broken group is skipped and reported, while all unrelated groups
    continue. There is no cross-run or all-or-nothing plan dependency.
    """
    rows = [dict(row) for row in plan.get("rows") or []]
    groups = frozen._group_rows(
        [row for row in rows if row.get("plan_status") == frozen.PLAN_READY]
    )
    applied_by_company: dict[int, dict[str, Any]] = {}
    row_errors: list[str] = []

    for key in sorted(groups):
        group_rows = groups[key]
        group_plan = dict(plan)
        group_plan["rows"] = group_rows

        try:
            ok, preflight_errors, _ = frozen.preflight_plan(client, group_plan)
        except Exception as exc:  # noqa: BLE001
            ok = False
            preflight_errors = [f"{type(exc).__name__}:{exc}"]

        if not ok:
            _mark_group_preflight_failed(applied_by_company, group_rows)
            for error in preflight_errors:
                message = f"{key}:preflight:{error}"
                row_errors.append(message)
                print(f"::warning::{message}", flush=True)
            continue

        try:
            results = reliable._apply_group_reliable(
                client,
                group_rows,
                str(plan["plan_id"]),
            )
            for result in results:
                applied_by_company[int(result["company_id"])] = result
        except Exception as exc:  # noqa: BLE001
            message = f"{key}:apply_error:{type(exc).__name__}:{exc}"
            row_errors.append(message)
            print(f"::warning::{message}", flush=True)
            continue

    final_rows: list[dict[str, Any]] = []
    for row in rows:
        company_id = int(row["company_id"])
        final_rows.append(applied_by_company.get(company_id, row))
    return sorted(final_rows, key=lambda item: int(item["company_id"])), row_errors, True


def run(mode: str, output_dir: Path) -> int:
    client = _client()
    workers = max(1, int(os.getenv("DIRECTOR_ENRICH_WORKERS", "6") or 6))
    http_timeout = max(3, int(os.getenv("DIRECTOR_HTTP_TIMEOUT", "12") or 12))

    plan, rows, skipped = build_live_plan(
        client,
        workers=workers,
        http_timeout=http_timeout,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    # Audit copy only. Execution never depends on a previous artifact/run/plan.
    (output_dir / "company_director_live_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if mode == "dry_run":
        summary = report.write_report_v4(
            output_dir,
            rows,
            skipped,
            plan,
            apply=False,
        )
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0

    report.write_report_v4(
        output_dir / "before_apply",
        rows,
        skipped,
        plan,
        apply=False,
    )

    final_rows, row_errors, execution_ok = apply_live_plan_resilient(client, plan)
    summary = report.write_report_v4(
        output_dir,
        final_rows,
        skipped,
        plan,
        apply=True,
        errors=row_errors,
    )
    summary["execution_mode"] = "self_contained"
    summary["row_errors_nonfatal"] = len(row_errors)
    summary["preflight_ok"] = execution_ok
    (output_dir / "company_director_run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)

    # Row/group failures are visible in the report but never discard unrelated
    # successful work. Fatal infrastructure/Bitrix snapshot errors still raise.
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Self-contained director enrichment. Every run reads fresh Bitrix "
            "state, repairs workflow-35 partial work, uses Adata/Kompra only "
            "for truly unknown directors and applies the same-run plan."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("dry_run", "apply"),
        default="dry_run",
    )
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()
    return run(args.mode, Path(args.output_dir))


if __name__ == "__main__":
    raise SystemExit(main())
