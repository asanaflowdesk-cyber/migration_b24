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
    """Read secondary company-contact links only where snapshot has no director.

    This avoids hundreds of unnecessary REST calls while still recovering a
    partially completed previous run where the director was attached as a
    secondary contact.
    """
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

    Existing historical/manual director data is treated as already managed and
    is never expanded into new contacts merely because RQ_DIRECTOR exists.
    Public sources are used only when Bitrix has no director at all.
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

        # Any existing director in Bitrix means the company is not an external
        # enrichment candidate. This preserves the original scope of workflow 35.
        handled.add(company_id)
        bin_number = base.current_bin(company, reqs)
        title = str(company.get("TITLE") or "")

        if len(values) != 1:
            conflicts.add(company_id)
            rows.append(
                {
                    "company_id": company_id,
                    "title": title,
                    "bin": bin_number,
                    "director": "",
                    "source": "bitrix",
                    "confidence": "internal_conflict",
                    "status": "source_conflict",
                    "evidence": "Bitrix=" + " | ".join(sorted(values)),
                    "url": "",
                    "adata_status": "not_checked",
                    "adata_director": "",
                    "adata_url": "",
                    "kompra_status": "not_checked",
                    "kompra_director": "",
                    "kompra_url": "",
                }
            )
            continue

        # Repair only records that workflow 35 itself created earlier. Without
        # our marker, existing RQ_DIRECTOR/contact data is left untouched.
        if not _managed_director_contact(contacts):
            continue

        director = next(iter(values))
        rows.append(
            {
                "company_id": company_id,
                "title": title,
                "bin": bin_number,
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

    seed_rows = internal_rows + external_rows
    seed_rows = v2._annotate_director_groups(seed_rows)
    print(
        f"[DIRECTOR] building live plan rows={len(seed_rows)} "
        f"repair={len(internal_rows)} external={len(external_rows)}",
        flush=True,
    )
    rows = frozen.build_exact_dry_run_plan(client, seed_rows, workers)

    run_id = str(os.getenv("GITHUB_RUN_ID") or "LOCAL")
    sha = str(os.getenv("GITHUB_SHA") or "LOCAL")
    execution_id = f"LIVE-{run_id}"
    plan = {
        "schema_version": 0,
        "plan_id": execution_id,
        "source_run_id": run_id,
        "source_sha": sha,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "skipped": skipped,
        "mode": "live_self_contained",
    }
    return plan, rows, skipped


def apply_live_plan_resilient(
    client: BitrixClient,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Apply all independent groups; one bad company no longer aborts the rest."""
    ok, preflight_errors, _ = frozen.preflight_plan(client, plan)
    rows = [dict(row) for row in plan.get("rows") or []]
    if not ok:
        for row in rows:
            if row.get("plan_status") == frozen.PLAN_READY:
                row["apply_status"] = "NOT_STARTED"
                row["verification_status"] = "PREFLIGHT_FAILED"
        return rows, preflight_errors, False

    groups = frozen._group_rows(
        [row for row in rows if row.get("plan_status") == frozen.PLAN_READY]
    )
    applied_by_company: dict[int, dict[str, Any]] = {}
    row_errors: list[str] = []

    for key in sorted(groups):
        try:
            results = reliable._apply_group_reliable(
                client,
                groups[key],
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

    # Persist the exact same-run plan for audit only. The workflow never needs a
    # previous artifact, run id, plan id or code SHA in order to execute.
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

    final_rows, row_errors, preflight_ok = apply_live_plan_resilient(client, plan)
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
    summary["preflight_ok"] = preflight_ok
    (output_dir / "company_director_run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)

    # A global preflight failure means nothing was written and is a real run
    # failure. Per-group errors are reported and retried from live Bitrix state
    # on the next apply; unrelated groups are not thrown away.
    return 0 if preflight_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Self-contained director enrichment. Every run reads fresh Bitrix "
            "state, repairs only workflow-35 partial work, uses Adata/Kompra "
            "only for truly unknown directors, and applies its same-run plan."
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
