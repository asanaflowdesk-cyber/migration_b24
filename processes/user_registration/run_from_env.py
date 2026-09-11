from __future__ import annotations

import os
import sys

import register_users_from_excel


def main() -> int:
    mode = os.environ.get("INPUT_MODE", "").strip().casefold()

    if mode not in {"dry_run", "apply"}:
        print(f"ERROR: invalid INPUT_MODE: {mode!r}", file=sys.stderr)
        return 2

    previous = sys.argv
    try:
        sys.argv = [
            "register_users_from_excel.py",
            "--mode",
            mode,
            "--spreadsheet-id",
            os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip(),
            "--sheet",
            os.environ.get("GOOGLE_SHEETS_NEW_USERS_TAB", "new_users_add").strip(),
            "--user-list-sheet",
            os.environ.get("GOOGLE_SHEETS_USER_LIST_TAB", "user_list").strip(),
        ]
        return int(register_users_from_excel.main() or 0)
    finally:
        sys.argv = previous


if __name__ == "__main__":
    raise SystemExit(main())
