from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings
from queue_founder_packages import process_queue


LOG = logging.getLogger("company_owner_sync_worker")


def build_client() -> BitrixClient:
    settings = Settings.from_env()
    return BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )


def run() -> None:
    queue_url = os.getenv("GOOGLE_QUEUE_URL", "").strip()
    queue_key = os.getenv("GOOGLE_QUEUE_KEY", "").strip()
    if not queue_url.startswith("https://script.google.com/macros/s/"):
        raise RuntimeError("GOOGLE_QUEUE_URL is not configured")
    if len(queue_key) < 32:
        raise RuntimeError("GOOGLE_QUEUE_KEY is not configured")
    if not os.getenv("TARGET_BITRIX_WEBHOOK_URL", "").strip():
        raise RuntimeError("TARGET_BITRIX_WEBHOOK_URL is not configured")

    poll_seconds = max(float(os.getenv("OWNER_SYNC_POLL_SECONDS", "5")), 1.0)
    error_sleep = max(float(os.getenv("OWNER_SYNC_ERROR_SLEEP_SECONDS", "10")), 2.0)
    max_batches = max(int(os.getenv("OWNER_SYNC_MAX_BATCHES", "20")), 1)
    output_dir = Path(
        os.getenv(
            "OWNER_SYNC_OUTPUT_DIR",
            str(Path.home() / ".company-owner-sync" / "output"),
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    client = build_client()

    print(
        f"31A persistent worker started; idle_poll={poll_seconds:.1f}s; "
        f"max_batches={max_batches}",
        flush=True,
    )

    while True:
        try:
            summary = process_queue(
                client,
                queue_url,
                queue_key,
                output_dir,
                max_batches,
            )

            if int(summary.get("items") or 0) == 0:
                time.sleep(poll_seconds)
            else:
                # Drain again almost immediately in case more owner-change
                # events arrived while the previous package was processing.
                time.sleep(0.25)

        except KeyboardInterrupt:
            print("31A persistent worker stopped.", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            LOG.exception("31A worker loop failed: %s", type(exc).__name__)
            time.sleep(error_sleep)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
