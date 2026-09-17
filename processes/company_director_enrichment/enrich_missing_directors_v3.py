from __future__ import annotations

import argparse
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import enrich_missing_directors as base
import enrich_missing_directors_v2 as v2
from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings


ACTIVE_SOURCES = ("adata", "kompra")


def enrich_candidates_two_sources(
    candidates: list[dict[str, Any]],
    workers: int,
    timeout: int,
) -> list[dict[str, Any]]:
    """Enrich by Adata OR Kompra only.

    One valid source is enough. If both sources return the same director, the
    result is confirmed. If both return different valid directors, the row is
    blocked as source_conflict. No ba.prg.kz lookup is performed.
    """
    by_id = {item["company_id"]: dict(item) for item in candidates}
    source_map: dict[int, list[dict[str, Any]]] = defaultdict(list)

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="director-web") as pool:
        futures = {
            pool.submit(base.base_source_results, item["bin"], timeout): item["company_id"]
            for item in candidates
        }
        done = 0
        for future in as_completed(futures):
            company_id = futures[future]
            done += 1
            try:
                source_map[company_id].extend(future.result())
            except Exception as exc:  # noqa: BLE001
                source_map[company_id].append(
                    {
                        "source": "web",
                        "url": "",
                        "director": "",
                        "status": type(exc).__name__,
                        "http": 0,
                    }
                )
            print(f"[DIRECTOR] web {done}/{len(candidates)} company={company_id}", flush=True)

    rows: list[dict[str, Any]] = []
    for company_id, item in by_id.items():
        decision = base.choose_director(source_map[company_id])
        item.update(decision)
        for source in ACTIVE_SOURCES:
            result = next(
                (row for row in source_map[company_id] if row.get("source") == source),
                None,
            )
            item[f"{source}_status"] = result.get("status", "not_checked") if result else "not_checked"
            item[f"{source}_director"] = result.get("director", "") if result else ""
            item[f"{source}_url"] = result.get("url", "") if result else ""
        rows.append(item)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich missing company directors from Adata OR Kompra, then reuse one "
            "canonical contact per person and link it to all companies/leads"
        )
    )
    parser.add_argument("--apply", action="store_true", help="Write director/requisite/company/lead links; default is dry-run")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-companies", type=int, default=0, help="0 = all candidates")
    parser.add_argument("--workers", type=int, default=int(os.getenv("DIRECTOR_ENRICH_WORKERS", "6") or 6))
    parser.add_argument("--http-timeout", type=int, default=int(os.getenv("DIRECTOR_HTTP_TIMEOUT", "12") or 12))
    args = parser.parse_args()
    if args.max_companies < 0 or args.workers <= 0 or args.http_timeout <= 0:
        parser.error("max-companies must be >= 0; workers/http-timeout must be > 0")

    settings = Settings.from_env()
    client = BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )

    print("[DIRECTOR] loading Bitrix companies/requisites/contacts", flush=True)
    snapshot = base.load_snapshot(client)
    candidates, skipped = base.build_candidates(snapshot)
    candidates = v2._filter_secondary_directors(client, candidates, skipped, snapshot, args.workers)
    candidates.sort(key=lambda item: int(item["company_id"]))
    if args.max_companies:
        candidates = candidates[: args.max_companies]

    print(f"[DIRECTOR] candidates={len(candidates)} skipped={len(skipped)}", flush=True)
    rows = enrich_candidates_two_sources(candidates, min(args.workers, 12), args.http_timeout)
    rows = v2._annotate_director_groups(rows)
    if args.apply:
        rows = v2.apply_rows_v2(client, rows, args.workers)

    summary = v2._write_report_v2(Path(args.output_dir), rows, skipped, args.apply)
    return 1 if args.apply and summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
