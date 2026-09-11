from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate e-Qazyna parser output")
    parser.add_argument("--results", required=True)
    parser.add_argument("--pages", required=True)
    return parser.parse_args()


def _load_json(path: Path):
    if not path.exists():
        raise RuntimeError(f"File was not created: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Cannot read JSON {path}: {exc}") from exc


def main() -> int:
    args = parse_args()
    result_data = _load_json(Path(args.results))
    page_data = _load_json(Path(args.pages))

    rows = result_data if isinstance(result_data, list) else result_data.get("results", [])
    page_logs = page_data.get("page_logs", []) if isinstance(page_data, dict) else []

    failures: list[str] = []
    if not page_logs:
        failures.append("No e-Qazyna page logs found")

    for page in page_logs:
        if not isinstance(page, dict):
            continue
        status = str(page.get("status") or "").lower()
        if status in {"failed", "empty"}:
            failures.append(
                f"e-Qazyna page {page.get('page', '?')} status={status}; "
                f"error={page.get('error', '')}"
            )

    row_errors = [
        row
        for row in rows
        if isinstance(row, dict)
        and (row.get("error") or str(row.get("action") or "").lower() == "error")
    ]
    row_warnings = [row for row in rows if isinstance(row, dict) and row.get("warning")]

    print(
        f"RESULT_SUMMARY rows={len(rows)} errors={len(row_errors)} "
        f"warnings={len(row_warnings)}"
    )
    for row in row_errors[:10]:
        print("ERROR_ROW", json.dumps(row, ensure_ascii=False))
    for row in row_warnings[:10]:
        print("WARNING_ROW", json.dumps(row, ensure_ascii=False))

    if row_errors:
        failures.append(f"Bitrix/eGov processing errors: {len(row_errors)}")

    if failures:
        print("RUN_FAILED")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("RUN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
