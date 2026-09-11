from __future__ import annotations

import os
import re
import sys

import recover_failed_leads


def main() -> int:
    mode = os.getenv("INPUT_MODE", "").strip().casefold()
    days = os.getenv("INPUT_DAYS", "").strip()
    moved_by_id = os.getenv("INPUT_MOVED_BY_ID", "").strip()

    if mode not in {"dry_run", "apply"}:
        raise SystemExit(f"invalid INPUT_MODE: {mode!r}")
    if not re.fullmatch(r"\d{1,2}", days) or not 1 <= int(days) <= 90:
        raise SystemExit("INPUT_DAYS must be an integer from 1 to 90")
    if moved_by_id and (not moved_by_id.isdigit() or int(moved_by_id) <= 0):
        raise SystemExit("INPUT_MOVED_BY_ID must be a positive integer")

    argv = ["recover_failed_leads.py", "--days", days, "--output-dir", "output"]
    if moved_by_id:
        argv.extend(["--moved-by-id", moved_by_id])
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
