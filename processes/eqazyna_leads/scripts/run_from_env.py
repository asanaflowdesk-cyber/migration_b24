from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def value(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else raw.strip()


def is_true(name: str) -> bool:
    return value(name).lower() in {"1", "true", "yes", "y", "on"}


def main() -> int:
    mode = value("INPUT_MODE", "dry_run").lower()
    pages = value("INPUT_PAGES", "1")
    page_start = value("INPUT_PAGE_START", "1")
    page_list = value("INPUT_PAGE_LIST")

    # Multiple pages are allowed only when the operator explicitly supplies the
    # exact list/range. This prevents an accidental long backfill from a typo.
    if not page_list and pages != "1":
        print(f"WARNING: PAGE_LIST is empty; PAGES changed from {pages} to 1")
        pages = "1"

    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    args = [
        sys.executable,
        "-m",
        "eqazyna_bitrix.main",
        "--pages",
        pages,
        "--page-start",
        page_start,
        "--doc-type",
        value("INPUT_DOC_TYPE", "Заявка на разведку ТПИ"),
        "--statuses",
        value("INPUT_STATUSES", "Отправлено на рассмотрение,Принято"),
        "--lead-status-id",
        "NEW",
        "--lead-generation-field",
        value("BITRIX_LEAD_GENERATION_FIELD", "UF_CRM_1785917145255"),
        "--lead-generation-value",
        value("BITRIX_LEAD_GENERATION_VALUE", "ГПО недропользователя"),
        "--originator-id",
        value("BITRIX_ORIGINATOR_ID", "EQAZYNA_LEAD"),
        "--source-id",
        value("BITRIX_SOURCE_ID", "OTHER"),
        "--source-description",
        value("BITRIX_SOURCE_DESCRIPTION", "e-Qazyna Minerals. ГПО недропользователи"),
        "--max-consecutive-page-errors",
        "1",
        "--push-bitrix",
        "--out",
        str(output_dir / "eqazyna_bitrix_leads.xlsx"),
        "--json-out",
        str(output_dir / "eqazyna_bitrix_leads.json"),
    ]

    if mode == "dry_run":
        args.append("--dry-run")
    elif mode != "apply":
        raise SystemExit(f"Unknown INPUT_MODE: {mode!r}")

    if is_true("INPUT_NO_EGOV"):
        args.append("--no-egov")
    if page_list:
        args.extend(["--page-list", page_list])

    min_created_date = value("INPUT_MIN_CREATED_DATE")
    if min_created_date:
        args.extend(["--min-created-date", min_created_date])

    assigned_by_id = value("BITRIX_ASSIGNED_BY_ID")
    if assigned_by_id:
        args.extend(["--assigned-by-id", assigned_by_id])

    print(
        "PARSER_RUN "
        f"mode={mode} pages={pages} page_start={page_start} "
        f"page_list={page_list or '-'} field={value('BITRIX_LEAD_GENERATION_FIELD')}"
    )
    completed = subprocess.run(args, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
