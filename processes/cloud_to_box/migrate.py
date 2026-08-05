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
                "SOURCE_BITRIX_WEBHOOK_URL is required for tasks, comments, checklists and files"
            )
        return None
    return BitrixClient.from_env("SOURCE_BITRIX_WEBHOOK_URL")


def main() -> int:
    args = parser().parse_args()
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
            blocking = [
                row for row in project.report.actions
                if row.get("status") in {"ERROR", "FATAL"}
            ]
            if blocking:
                logging.error("Import produced %s blocking errors; see errors.csv", len(blocking))
                exit_code = 4

        elif args.command == "verify":
            result = project.verify()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            exit_code = 0 if result["ok"] else 3

    except Exception as exc:
        logging.exception("Migration command failed: %s", exc)
        project.report.add(args.command, "SYSTEM", "", "SYSTEM", "", "FATAL", str(exc))
        exit_code = 1
    finally:
        project.report.save()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
