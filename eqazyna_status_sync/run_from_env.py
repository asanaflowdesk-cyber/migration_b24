from __future__ import annotations

import os
import subprocess
import sys


def value(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else raw.strip()


def main() -> int:
    mode = value("INPUT_MODE", "dry_run").lower()
    if mode not in {"dry_run", "apply"}:
        raise SystemExit(f"Unknown INPUT_MODE: {mode!r}")

    max_items_raw = value("INPUT_MAX_ITEMS", "0")
    try:
        max_items = int(max_items_raw)
    except ValueError as exc:
        raise SystemExit("INPUT_MAX_ITEMS must be an integer") from exc
    if max_items < 0:
        raise SystemExit("INPUT_MAX_ITEMS must be >= 0")

    args = [
        sys.executable,
        "sync_eqazyna_statuses.py",
        "--mode",
        mode,
        "--doc-type",
        value("INPUT_DOC_TYPE", "Заявка на разведку ТПИ"),
        "--failure-stage-name",
        value("INPUT_FAILURE_STAGE_NAME", "Провал"),
        "--potential-stage-name",
        value("INPUT_POTENTIAL_STAGE_NAME", "Потенциальные сделки"),
        "--originators",
        value("BITRIX_EQAZYNA_ORIGINATORS", "EQAZYNA_LEAD,EQAZYNA"),
        "--max-items",
        str(max_items),
        "--output-dir",
        "output",
    ]
    completed = subprocess.run(args, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
