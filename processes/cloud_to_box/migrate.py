#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from common.bitrix import BitrixClient
from src.migration import MigrationProject


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bitrix24 cloud to box CRM migration")
    p.add_argument("command", choices=["plan", "invite-users", "import", "verify"])
    p.add_argument("--source", default="input/bitrix24_export.xlsx")
    p.add_argument("--config", default="config/migration.json")
    p.add_argument("--users", default="config/users.csv")
    p.add_argument("--output", default="output")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--default-department-id", type=int, default=0)
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--allow-user-fallback", action="store_true")
    return p


def main() -> int:
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    client = BitrixClient.from_env()
    project = MigrationProject(args.source, args.config, args.users, args.output, client)
    exit_code = 0
    try:
        if args.command == "plan":
            source = project.source_plan()
            project.discover_target()
            target = project.validate_target()
            project.build_user_map(strict=False)
            print(json.dumps({"source": source, "target": target}, ensure_ascii=False, indent=2))
            exit_code = 0 if target["ok"] else 2
        elif args.command == "invite-users":
            project.invite_users(default_department_id=args.default_department_id, dry_run=args.dry_run)
        elif args.command == "import":
            project.import_crm(
                dry_run=args.dry_run,
                max_items=args.max_items,
                strict_users=not args.allow_user_fallback,
            )
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
