from __future__ import annotations

import os
import sys
from pathlib import Path

import register_users_from_excel


BASE_DIR = Path(__file__).resolve().parent


def _resolve_input_file(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise ValueError("INPUT_FILE_PATH is empty")
    if any(ord(char) < 32 for char in value):
        raise ValueError("INPUT_FILE_PATH contains control characters")

    path = Path(value)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (BASE_DIR / path).resolve()

    try:
        relative = resolved.relative_to(BASE_DIR)
    except ValueError as exc:
        raise ValueError("INPUT_FILE_PATH must point inside processes/user_registration") from exc

    return str(relative)


def main() -> int:
    mode = os.environ.get("INPUT_MODE", "").strip().casefold()
    raw_file_path = os.environ.get("INPUT_FILE_PATH", "")

    if mode not in {"dry_run", "apply"}:
        print(f"ERROR: invalid INPUT_MODE: {mode!r}", file=sys.stderr)
        return 2

    try:
        file_path = _resolve_input_file(raw_file_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    previous = sys.argv
    try:
        sys.argv = [
            "register_users_from_excel.py",
            "--file",
            file_path,
            "--mode",
            mode,
        ]
        return int(register_users_from_excel.main() or 0)
    finally:
        sys.argv = previous


if __name__ == "__main__":
    raise SystemExit(main())
