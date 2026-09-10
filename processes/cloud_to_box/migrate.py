#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from common.bitrix import BitrixClient
from src.migration import MigrationProject

DEFAULT_SOURCE = "input/bitrix24_dump_20260805_072425.zip"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bitrix24 cloud-to-box migration")
    p.add_argument("command", choices=["plan", "map-users", "import", "verify"])
    p.add_argument("--source", default=DEFAULT_SOURCE)
    p.add_argument("--config", default="config/migration.json")
    p.add_argument("--users", default="config/users.csv")
    p.add_argument("--output", default="output")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-items", type=int, default=0)
    return p


def target_client() -> BitrixClient:
    return BitrixClient.from_env("TARGET_BITRIX_WEBHOOK_URL")


def source_client(required: bool) -> BitrixClient | None:
    url = os.environ.get("SOURCE_BITRIX_WEBHOOK_URL", "").strip()
    if not url:
        if required:
            raise RuntimeError(
                "SOURCE_BITRIX_WEBHOOK_URL is required for cloud preflight and child-content enrichment"
            )
        return None
    return BitrixClient.from_env("SOURCE_BITRIX_WEBHOOK_URL")


def _explained_skip(row: dict[str, object]) -> bool:
    """Return True only for intentionally lossy skips approved by config/policy."""
    message = str(row.get("message") or "").casefold()
    operation = str(row.get("operation") or "")
    target_id = str(row.get("target_id") or "").strip()
    if target_id and target_id not in {"0", "none"}:
        # Idempotent "already present" paths are not data loss.
        return True
    if operation == "map_user" and "unused source user" in message:
        return True
    if operation == "create_task" and "excluded by configured source users" in message:
        return True
    if operation == "link_requisite" and ("routed" in message or "not applicable" in message):
        return True
    return False


def _import_exit_code(project: MigrationProject) -> int:
    errors = [row for row in project.report.actions if row.get("status") in {"ERROR", "FATAL"}]
    skips = [row for row in project.report.actions if row.get("status") == "SKIP"]
    warnings = [row for row in project.report.actions if row.get("status") == "WARN"]
    unexplained_skips = [row for row in skips if not _explained_skip(row)]
    failed = bool(errors or unexplained_skips)
    project.report.extra["import_result"] = {
        "policy": "fail_on_error_or_unexplained_loss",
        "skipped": len(skips),
        "warnings": len(warnings),
        "errors": len(errors),
        "unexplained_skips": len(unexplained_skips),
        "workflow_failed": failed,
    }
    if failed:
        logging.error(
            "Import is incomplete: errors=%s unexplained_skips=%s warnings=%s",
            len(errors), len(unexplained_skips), len(warnings),
        )
        return 2
    logging.info(
        "Import completed: explained_skips=%s warnings=%s errors=0",
        len(skips), len(warnings),
    )
    return 0


def main() -> int:
    args = parser().parse_args()
    if args.max_items < 0:
        parser().error("--max-items must be 0 or a positive integer")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    target = target_client()
    source = source_client(required=args.command == "import")
    project = MigrationProject(
        Path(args.source),
        Path(args.config),
        Path(args.users),
        Path(args.output),
        target,
        source,
    )

    exit_code = 0
    try:
        if args.command == "plan":
            source_plan = project.source_plan()
            project.discover_target()
            target_validation = project.validate_target()
            user_map = project.build_user_map(strict=False)
            result = {
                "source": source_plan,
                "target": target_validation,
                "users": {
                    "mapped": len(user_map),
                    "source_total": source_plan["source_counts"]["Users"],
                },
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            exit_code = 0 if target_validation["ok"] else 2

        elif args.command == "map-users":
            project.discover_target()
            user_map = project.build_user_map(strict=False)
            diagnostics = project.report.extra.get("user_mapping", {})
            result = {
                "mapped": len(user_map),
                "source_total": diagnostics.get("source_users", 0),
                "unresolved": diagnostics.get("unresolved", []),
                "ambiguous": diagnostics.get("ambiguous", []),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            exit_code = 0 if not result["unresolved"] and not result["ambiguous"] else 2

        elif args.command == "import":
            project.import_all(dry_run=args.dry_run, max_items=args.max_items)
            exit_code = _import_exit_code(project)

        elif args.command == "verify":
            result = project.verify()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            exit_code = 0 if result.get("ok") else 2

    except Exception as exc:
        logging.exception("Migration command failed: %s", exc)
        project.report.add(args.command, "SYSTEM", "", "SYSTEM", "", "FATAL", str(exc))
        exit_code = 1
    finally:
        project.report.save()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
