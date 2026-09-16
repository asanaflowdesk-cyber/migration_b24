from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings
from package_reconciler import process_package
from sync_founder_packages import build_packages, is_founder_contact, load_snapshot, normalized_id, select_source_package


TRANSIENT_HTTP = {404, 408, 425, 429, 500, 502, 503, 504}
QUEUE_RETRY_DELAYS = (10, 30, 60)


def queue_call(url: str, key: str, action: str, **payload: Any) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(len(QUEUE_RETRY_DELAYS) + 1):
        try:
            response = requests.post(url, json={"key": key, "action": action, **payload}, timeout=45)
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


def reason(value: Any) -> str:
    raw = str(value or "sync_failed")
    return re.sub(r"[^0-9A-Za-z_-]+", "_", raw)[:120] or "sync_failed"


def operation_logger(queue_url: str, queue_key: str):
    def log(operation: dict[str, Any], items: list[dict[str, Any]]) -> None:
        try:
            queue_call(queue_url, queue_key, "log_operation", operation=operation, items=items)
        except Exception as exc:  # journal failure must not interrupt CRM recovery
            print(json.dumps({"journal_warning": type(exc).__name__, "operation_id": operation.get("operation_id")}, ensure_ascii=False))
    return log


def process_claim(
    client: BitrixClient,
    output_dir: Path,
    claim: dict[str, Any],
    queue_url: str,
    queue_key: str,
) -> tuple[list[dict[str, Any]], int]:
    items = claim.get("items") or []
    ids = [normalized_id(item.get("contact_id")) for item in items]
    if not items or any(item_id is None for item_id in ids):
        raise RuntimeError("queue_claim_contains_invalid_id")

    snapshot = load_snapshot(client)
    packages, _ = build_packages(snapshot["companies"], snapshot["leads"], snapshot["contacts"], snapshot["requisites"])
    contacts_by_id = {
        contact_id: contact
        for contact in snapshot["contacts"]
        if (contact_id := normalized_id(contact.get("ID"))) is not None
    }
    ids_by_fio: dict[str, list[int]] = defaultdict(list)
    unresolved: set[int] = set()
    ignored: set[int] = set()
    missing: set[int] = set()
    for contact_id in ids:
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
            ids_by_fio[str(package["fio"])].append(contact_id)
    conflicting = {contact_id for group in ids_by_fio.values() if len(group) > 1 for contact_id in group}

    progress = operation_logger(queue_url, queue_key)
    results: list[dict[str, Any]] = []
    failures = 0
    for item, contact_id in zip(items, ids):
        version = int(item["version"])
        attempts = int(item.get("attempts") or 1)
        operation_id = f"{claim['claim_id']}:{contact_id}:v{version}"
        retryable = False

        if contact_id in ignored:
            success = True
            outcome = "ignored"
            error = ""
            status = "IGNORED"
        elif contact_id in missing:
            success = False
            outcome = "failed"
            error = "source_contact_not_found"
            status = "MANUAL_REVIEW"
        elif contact_id in unresolved:
            success = False
            outcome = "failed"
            error = "source_contact_package_not_resolved"
            status = "MANUAL_REVIEW"
        elif contact_id in conflicting:
            success = False
            outcome = "failed"
            error = "multiple_contacts_for_same_package"
            status = "MANUAL_REVIEW"
        else:
            package = select_source_package(packages, contact_id)
            assert package is not None
            result = process_package(
                client,
                output_dir / f"contact-{contact_id}-v{version}",
                package,
                contact_id,
                operation_id,
                str(claim["claim_id"]),
                attempts,
                progress=progress,
                max_reconcile_rounds=3,
            )
            success = bool(result["success"])
            retryable = bool(result["retryable"])
            status = str(result["status"])
            outcome = "processed" if success else ("partial" if status == "PARTIAL" else "failed")
            error = "" if success else reason(result.get("reason"))

        failures += not success
        results.append({
            "contact_id": contact_id,
            "version": version,
            "success": success,
            "retryable": retryable,
            "error": error,
            "outcome": outcome,
            "status": status,
            "operation_id": operation_id,
        })
    return results, failures


def process_queue(client: BitrixClient, queue_url: str, queue_key: str, output_dir: Path, max_batches: int) -> dict[str, Any]:
    worker_id = f"{os.getenv('GITHUB_RUN_ID', 'local')}-{uuid.uuid4().hex[:12]}"
    total_items = total_processed = total_ignored = total_partial = total_failures = batches = 0
    for _ in range(max_batches):
        claim = queue_call(queue_url, queue_key, "claim", worker_id=worker_id, limit=500)
        items = claim.get("items") or []
        if not items:
            break
        batches += 1
        try:
            results, failures = process_claim(client, output_dir, claim, queue_url, queue_key)
        except Exception as exc:  # acknowledgement must survive processing failures
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
        queue_call(queue_url, queue_key, "complete", claim_id=claim["claim_id"], results=results)
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
    (output_dir / "queue_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Drain queued founder packages in one worker")
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
