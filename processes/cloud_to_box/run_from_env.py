from __future__ import annotations

import os
import re
import sys

import migrate


def main() -> int:
    mode = os.environ.get("INPUT_MODE", "").strip().casefold()
    raw_max_items = os.environ.get("INPUT_MAX_ITEMS", "").strip()
    if mode not in {"dry_run", "apply"}:
        print(f"ERROR: invalid INPUT_MODE: {mode!r}", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d+", raw_max_items):
        print(f"ERROR: INPUT_MAX_ITEMS must be a non-negative integer: {raw_max_items!r}", file=sys.stderr)
        return 2
    argv = ["migrate.py", "import", "--max-items", raw_max_items]
    if mode == "dry_run":
        argv.append("--dry-run")
    previous = sys.argv
    try:
        sys.argv = argv
        return int(migrate.main() or 0)
    finally:
        sys.argv = previous


if __name__ == "__main__":
    raise SystemExit(main())
