from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings
from fast_package_reconciler import process_package_fast
from sync_founder_packages import build_packages, is_founder_contact, load_snapshot, normalized_id, select_source_package


TRANSIENT_HTTP = {404, 408, 425, 429, 500, 502, 503, 504}
QUEUE_RETRY_DELAYS = (1, 2, 4)
QUEUE_HTTP_TIMEOUT = 12


def queue_call(url: str, key: str, action: str, **payload: Any) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(len(QUEUE_RETRY_DELAYS) + 1):
        try:
            response = requests.post(
                url,
                json={"key": key, "action": action, **payload},
                timeout=QUEUE_HTTP_TIMEOUT,
            )
            if response.status_code in TRANSIENT_HTTP and attempt < len(QUEUE_RETRY_DELAYS):
                time.sleep(QUEUE_RETRY_DELAYS[attempt])
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not data.get("ok"):
                error = str(data.get("error") or "") if isinstance(data, dict) else "invalid_response"
                if error in {"internal_error", "busy"} and attempt < len(QUEUE_RETRY_DELAYS):
                    time.sleep(QUEUE_RETRY_DELAYS[attempt])
                    continue
                raise RuntimeError(f"queue_{action}_rejected:{error or 'unknown'}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt >= len(QUEUE_RETRY_DELAYS):
                break
            time.sleep(QUEUE_RETRY_DELAYS[attempt])
    raise RuntimeError(f"queue_{action}_failed:{type(last_error).__name__}") from None


def best_effort_queue_call(url: str, key: str, action: str, **payload: Any) -> None:
    """Non-critical status/journal call that must never hold up CRM writes."""
    if not url or not key:
        return
    try:
        response = requests.post(
            url,
            json={"key": key, "action": action, **payload},
            timeout=5,
        )
        if not response.ok:
            print(f"[QUEUE-STATUS] {action} HTTP {response.status_code}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[QUEUE-STATUS] {action} skipped: {type(exc).__name__}", flush=True)


def reason(value: Any) -> str:
    raw = str(value or "sync_failed")
    return re.sub(r"[^0-9A-Za-z_-]+", "_", raw)[:120] or "sync_failed"


def _clone_client(client: BitrixClient) -> BitrixClient:
    return BitrixClient(
        client.webhook_url,
        timeout=client.timeout,
        retries=client.retries,
        polite_delay_seconds=0.0,
        verify_ssl=client.verify_ssl,
    )


def _result_payload(
    contact_id: int,
    version: int,
    success: bool,
    outcome: str,
    error: str,
    status: str = "",
    operation_id: str = "",
    retryable: bool = False,
) -> dict[str, Any]:
    if outcome == "ignored":
        return {
            "contact_id": contact_id,
            "version": version,
            "success": True,
            "error": "",
            "outcome": "ignored",
        }
    return {
        "contact_id": contact_id,
        "version": version,
        "success": success,
        "retryable": retryable,
        "error": error,
        "outcome": outcome,
        "status": status,
        "operation_id": operation_id,
    }


def process_claim(
    client: BitrixClient,
    output_dir: Path,
    claim: dict[str, Any],
    queue_url: str = "",
    queue_key: str = "",
) -> tuple[list[dict[str, Any]], int]:
    started = time.monotonic()
    items = claim.get("items") or []
    ids = [normalized_id(item.get("contact_id")) for item in items]
    if not items or any(item_id is None for item_id in ids):
        raise RuntimeError("queue_claim_contains_invalid_id")

    print(f"[PROCESSING] snapshot: start; queued={len(items)}", flush=True)
    snapshot = load_snapshot(client)
    packages, _ = build_packages(
        snapshot["companies"],
        snapshot["leads"],
        snapshot["contacts"],
        snapshot["requisites"],
    )
    print(
        "[PROCESSING] snapshot: ready; "
        f"companies={len(snapshot['companies'])}; leads={len(snapshot['leads'])}; "
        f"contacts={len(snapshot['contacts'])}; packages={len(packages)}; "
        f"elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )

    contacts_by_id = {
        contact_id: contact
        for contact in snapshot["contacts"]
        if (contact_id := normalized_id(contact.get("ID"))) is not None
    }
    ids_by_fio: dict[str, list[int]] = defaultdict(list)
    unresolved: set[int] = set()
    ignored: set[int] = set()
    missing: set[int] = set()
    packages_by_contact: dict[int, dict[str, Any]] = {}
    for contact_id in ids:
        assert contact_id is not None
        package = select_source_package(packages, contact_id)
        if package is None:
            contact = contacts_by_id.get(contact_id)
            if contact is None:
                missing.add(contact_id)
            elif not is_founder_contact(contact):
                ignored.add(contact_id)
            else:
                unresolved.add(contact_id)
        else:
            packages_by_contact[contact_id] = package
            ids_by_fio[str(package["fio"])].append(contact_id)
    conflicting = {contact_id for group in ids_by_fio.values() if len(group) > 1 for contact_id in group}

    results_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    journal_entries: list[dict[str, Any]] = []
    valid_jobs: list[tuple[dict[str, Any], int, int, int, str, dict[str, Any]]] = []

    for item, contact_id in zip(items, ids):
        assert contact_id is not None
        version = int(item["version"])
        attempts = int(item.get("attempts") or 1)
        operation_id = f"{claim['claim_id']}:{contact_id}:v{version}"
        if contact_id in ignored:
            results_by_key[(contact_id, version)] = _result_payload(contact_id, version, True, "ignored", "")
        elif contact_id in missing:
            results_by_key[(contact_id, version)] = _result_payload(
                contact_id, version, False, "failed", "source_contact_not_found", "MANUAL_REVIEW", operation_id
            )
        elif contact_id in unresolved:
            results_by_key[(contact_id, version)] = _result_payload(
                contact_id, version, False, "failed", "source_contact_package_not_resolved", "MANUAL_REVIEW", operation_id
            )
        elif contact_id in conflicting:
            results_by_key[(contact_id, version)] = _result_payload(
                contact_id, version, False, "failed", "multiple_contacts_for_same_package", "MANUAL_REVIEW", operation_id
            )
        else:
            valid_jobs.append((item, contact_id, version, attempts, operation_id, packages_by_contact[contact_id]))

    total = len(items)
    finished = len(results_by_key)
    workers = max(1, min(int(os.getenv("OWNER_SYNC_WORKERS", "8") or 8), 16, len(valid_jobs) or 1))
    progress_every = max(1, int(os.getenv("OWNER_SYNC_PROGRESS_EVERY", "5") or 5))
    print(
        f"[PROCESSING] packages: start; valid={len(valid_jobs)}; ignored={len(ignored)}; "
        f"errors={len(missing) + len(unresolved) + len(conflicting)}; workers={workers}",
        flush=True,
    )

    def run_job(job: tuple[dict[str, Any], int, int, int, str, dict[str, Any]]):
        _item, contact_id, version, attempts, operation_id, package = job
        local_client = _clone_client(client) if isinstance(client, BitrixClient) else client
        result = process_package_fast(
            local_client,
            output_dir / f"contact-{contact_id}-v{version}",
            package,
            contact_id,
            operation_id,
            str(claim["claim_id"]),
            attempts,
        )
        success = bool(result["success"])
        retryable = bool(result["retryable"])
        status = str(result["status"])
        outcome = "processed" if success else ("partial" if status == "PARTIAL" else "failed")
        error = "" if success else reason(result.get("reason"))
        payload = _result_payload(
            contact_id,
            version,
            success,
            outcome,
            error,
            status,
            operation_id,
            retryable,
        )
        return contact_id, version, payload, result.get("operation") or {}, result.get("items") or []

    if valid_jobs:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="owner-sync") as pool:
            future_map = {pool.submit(run_job, job): job for job in valid_jobs}
            for future in as_completed(future_map):
                job = future_map[future]
                _item, contact_id, version, _attempts, operation_id, _package = job
                try:
                    result_contact_id, result_version, payload, operation, operation_items = future.result()
                except Exception as exc:  # noqa: BLE001
                    payload = _result_payload(
                        contact_id,
                        version,
                        False,
                        "partial",
                        type(exc).__name__,
                        "PARTIAL",
                        operation_id,
                        True,
                    )
                    result_contact_id, result_version = contact_id, version
                    operation, operation_items = {}, []
                results_by_key[(result_contact_id, result_version)] = payload
                if operation:
                    journal_entries.append({"operation": operation, "items": operation_items})
                finished += 1
                elapsed = time.monotonic() - started
                print(
                    f"[PROCESSING] {finished}/{total}; contact={result_contact_id}; "
                    f"status={payload.get('status') or payload.get('outcome')}; elapsed={elapsed:.1f}s",
                    flush=True,
                )
                if finished % progress_every == 0 or finished == total:
                    best_effort_queue_call(
                        queue_url,
                        queue_key,
                        "progress",
                        claim_id=str(claim["claim_id"]),
                        completed=finished,
                        total=total,
                        active_contact_id=result_contact_id,
                        status=str(payload.get("status") or payload.get("outcome") or "PROCESSING"),
                    )

    if journal_entries:
        for start in range(0, len(journal_entries), 20):
            best_effort_queue_call(
                queue_url,
                queue_key,
                "log_operations",
                entries=journal_entries[start:start + 20],
            )

    results: list[dict[str, Any]] = []
    for item, contact_id in zip(items, ids):
        assert contact_id is not None
        version = int(item["version"])
        results.append(results_by_key[(contact_id, version)])

    failures = sum(not bool(result.get("success")) for result in results)
    print(
        f"[PROCESSING] claim complete; total={total}; failures={failures}; "
        f"elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )
    return results, failures


def process_queue(
    client: BitrixClient,
    queue_url: str,
    queue_key: str,
    output_dir: Path,
    max_batches: int,
) -> dict[str, Any]:
    worker_id = f"{os.getenv('GITHUB_RUN_ID', 'local')}-{uuid.uuid4().hex[:12]}"
    total_items = total_processed = total_ignored = total_partial = total_failures = batches = 0
    claim_limit = min(max(int(os.getenv("OWNER_SYNC_CLAIM_LIMIT", "100") or 100), 1), 500)
    for batch_no in range(1, max_batches + 1):
        print(f"[QUEUE] claim request batch={batch_no}; limit={claim_limit}", flush=True)
        claim = queue_call(queue_url, queue_key, "claim", worker_id=worker_id, limit=claim_limit)
        items = claim.get("items") or []
        if not items:
            print(f"[QUEUE] no work; busy={bool(claim.get('busy'))}", flush=True)
            break
        batches += 1
        print(f"[QUEUE] claimed={len(items)}; claim_id={claim.get('claim_id')}", flush=True)
        try:
            results, failures = process_claim(client, output_dir, claim, queue_url, queue_key)
        except Exception as exc:  # acknowledgement must survive processing failures
            print(f"[PROCESSING] fatal claim error: {type(exc).__name__}", flush=True)
            results = [
                {
                    "contact_id": int(item["contact_id"]),
                    "version": int(item["version"]),
                    "success": False,
                    "retryable": True,
                    "error": type(exc).__name__,
                    "outcome": "failed",
                    "status": "PARTIAL",
                }
                for item in items
            ]
            failures = len(results)
        completion = queue_call(queue_url, queue_key, "complete", claim_id=claim["claim_id"], results=results)
        print(
            f"[QUEUE] complete; done={completion.get('completed', 0)}; "
            f"pending={completion.get('pending', 0)}; retry={completion.get('retry', 0)}; "
            f"manual={completion.get('manual_review', 0)}",
            flush=True,
        )
        total_items += len(results)
        total_processed += sum(item.get("outcome") == "processed" for item in results)
        total_ignored += sum(item.get("outcome") == "ignored" for item in results)
        total_partial += sum(item.get("outcome") == "partial" for item in results)
        total_failures += failures

    summary = {
        "batches": batches,
        "items": total_items,
        "processed": total_processed,
        "ignored": total_ignored,
        "partial": total_partial,
        "failures": total_failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "queue_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[QUEUE] summary {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Drain queued founder packages with batch writes and live progress")
    parser.add_argument("--output-dir", default="output_event")
    parser.add_argument("--max-batches", type=int, default=20)
    args = parser.parse_args()
    queue_url = os.getenv("GOOGLE_QUEUE_URL", "").strip()
    queue_key = os.getenv("GOOGLE_QUEUE_KEY", "").strip()
    if not queue_url.startswith("https://script.google.com/macros/s/") or len(queue_key) < 32:
        parser.error("GOOGLE_QUEUE_URL or GOOGLE_QUEUE_KEY is not configured")
    settings = Settings.from_env()
    client = BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )
    summary = process_queue(client, queue_url, queue_key, Path(args.output_dir), args.max_batches)
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
