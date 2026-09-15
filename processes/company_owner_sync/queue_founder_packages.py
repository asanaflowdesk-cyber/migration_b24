from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings
from sync_founder_packages import (
    build_packages,
    is_founder_contact,
    load_snapshot,
    normalized_id,
    run,
    select_source_package,
)


def queue_call(url: str, key: str, action: str, **payload: Any) -> dict[str, Any]:
    response = requests.post(url, json={"key": key, "action": action, **payload}, timeout=45)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"queue_{action}_rejected")
    return data


def reason(summary: dict[str, Any]) -> str:
    value = str(summary.get("reason") or "")
    if value:
        return re.sub(r"[^0-9A-Za-z_-]+", "_", value)[:120]
    return "sync_failed"


def process_claim(
    client: BitrixClient,
    output_dir: Path,
    claim: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    items = claim.get("items") or []
    ids = [normalized_id(item.get("contact_id")) for item in items]
    if not items or any(item_id is None for item_id in ids):
        raise RuntimeError("queue_claim_contains_invalid_id")

    snapshot = load_snapshot(client)
    packages, _ = build_packages(
        snapshot["companies"], snapshot["leads"], snapshot["contacts"], snapshot["requisites"]
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

    results: list[dict[str, Any]] = []
    failures = 0
    for item, contact_id in zip(items, ids):
        if contact_id in ignored:
            summary = {"errors": 0, "reason": "not_founder_or_director"}
            outcome = "ignored"
        elif contact_id in missing:
            summary = {"errors": 1, "reason": "source_contact_not_found"}
            outcome = "failed"
        elif contact_id in unresolved:
            summary = {"errors": 1, "reason": "source_contact_package_not_resolved"}
            outcome = "failed"
        elif contact_id in conflicting:
            summary = {"errors": 1, "reason": "multiple_contacts_for_same_package"}
            outcome = "failed"
        else:
            summary = run(
                client,
                output_dir / f"contact-{contact_id}-v{item['version']}",
                True,
                source_contact_ids=[contact_id],
                snapshot=snapshot,
            )
            outcome = "processed" if int(summary.get("errors") or 0) == 0 else "failed"
        success = int(summary.get("errors") or 0) == 0
        failures += not success
        results.append({
            "contact_id": contact_id,
            "version": int(item["version"]),
            "success": success,
            "error": "" if success else reason(summary),
            "outcome": outcome,
        })
    return results, failures


def process_queue(client: BitrixClient, queue_url: str, queue_key: str, output_dir: Path, max_batches: int) -> dict[str, Any]:
    worker_id = f"{os.getenv('GITHUB_RUN_ID', 'local')}-{uuid.uuid4().hex[:12]}"
    total_items = total_processed = total_ignored = total_failures = batches = 0
    for _ in range(max_batches):
        claim = queue_call(queue_url, queue_key, "claim", worker_id=worker_id, limit=500)
        items = claim.get("items") or []
        if not items:
            break
        batches += 1
        try:
            results, failures = process_claim(client, output_dir, claim)
        except Exception as exc:  # queue acknowledgement must survive processing failures
            results = [
                {"contact_id": int(item["contact_id"]), "version": int(item["version"]), "success": False, "error": type(exc).__name__}
                for item in items
            ]
            failures = len(results)
        queue_call(queue_url, queue_key, "complete", claim_id=claim["claim_id"], results=results)
        total_items += len(results)
        total_processed += sum(item.get("outcome") == "processed" for item in results)
        total_ignored += sum(item.get("outcome") == "ignored" for item in results)
        total_failures += failures

    summary = {
        "batches": batches,
        "items": total_items,
        "processed": total_processed,
        "ignored": total_ignored,
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
