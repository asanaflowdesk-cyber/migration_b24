from __future__ import annotations

import os
import re
import sys

import recover_failed_leads


def main() -> int:
    mode = os.environ.get("INPUT_MODE", "").strip().casefold()
    raw_days = os.environ.get("INPUT_DAYS", "").strip()
    raw_moved_by = os.environ.get("INPUT_MOVED_BY_ID", "").strip()
    if mode not in {"dry_run", "apply"}:
        print(f"ERROR: invalid INPUT_MODE: {mode!r}", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d{1,2}", raw_days) or not 1 <= int(raw_days) <= 90:
        print(f"ERROR: INPUT_DAYS must be an integer from 1 to 90: {raw_days!r}", file=sys.stderr)
        return 2
    if raw_moved_by and (not re.fullmatch(r"\d+", raw_moved_by) or int(raw_moved_by) <= 0):
        print("ERROR: INPUT_MOVED_BY_ID must be empty or a positive integer", file=sys.stderr)
        return 2

    argv = ["recover_failed_leads.py", "--days", raw_days, "--output-dir", "output"]
    if raw_moved_by:
        argv.extend(["--moved-by-id", raw_moved_by])
    if mode == "apply":
        argv.append("--apply")
    previous = sys.argv
    try:
        sys.argv = argv
        return int(recover_failed_leads.main() or 0)
    finally:
        sys.argv = previous


if __name__ == "__main__":
    raise SystemExit(main())
