from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.bitrix import BitrixClient, BitrixError


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"


def normalize_name(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_config() -> list[dict[str, Any]]:
    raw_path = os.environ.get("INPUT_FILE_PATH", "departments.json").strip()
    path = (BASE_DIR / raw_path).resolve()

    try:
        path.relative_to(BASE_DIR)
    except ValueError as exc:
        raise ValueError("INPUT_FILE_PATH должен находиться внутри processes/departments") from exc

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("departments.json должен содержать JSON-массив")

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for number, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Строка {number}: ожидается объект")

        name = " ".join(str(item.get("name") or "").split())
        parent = as_int(item.get("parent"))
        sort = as_int(item.get("sort")) or 500

        if not name:
            raise ValueError(f"Строка {number}: пустое name")
        if parent is None or parent <= 0:
            raise ValueError(f"Строка {number}: некорректный parent")

        key = (normalize_name(name), parent)
        if key in seen:
            raise ValueError(f"Дубль в конфигурации: {name!r}, parent={parent}")
        seen.add(key)

        rows.append({"name": name, "parent": parent, "sort": sort})

    return rows


def list_departments(client: BitrixClient) -> list[dict[str, Any]]:
    result = client.call("department.get", {})
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        for key in ("departments", "items", "result"):
            value = result.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def extract_created_id(result: Any) -> int | None:
    if isinstance(result, (int, str)):
        return as_int(result)
    if isinstance(result, dict):
        for key in ("ID", "id", "department", "result"):
            value = result.get(key)
            if isinstance(value, dict):
                nested = value.get("ID") or value.get("id")
                if nested is not None:
                    return as_int(nested)
            elif value is not None:
                parsed = as_int(value)
                if parsed is not None:
                    return parsed
    return None


def save_report(report: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "departments_result.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Отчет: {path}")


def main() -> int:
    mode = os.environ.get("INPUT_MODE", "dry_run").strip().casefold()
    if mode not in {"dry_run", "apply"}:
        print(f"ERROR: неизвестный INPUT_MODE={mode!r}", file=sys.stderr)
        return 2

    try:
        config = load_config()
    except Exception as exc:
        print(f"ERROR: не удалось прочитать конфигурацию: {exc}", file=sys.stderr)
        return 2

    client = BitrixClient.from_env()

    try:
        existing = list_departments(client)
    except Exception as exc:
        print(f"ERROR: не удалось получить оргструктуру Bitrix24: {exc}", file=sys.stderr)
        return 1

    existing_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    ids: set[int] = set()

    for dep in existing:
        dep_id = as_int(dep.get("ID"))
        parent = as_int(dep.get("PARENT"))
        name = dep.get("NAME")
        if dep_id is not None:
            ids.add(dep_id)
        if parent is not None and name:
            existing_by_key[(normalize_name(name), parent)] = dep

    parents = sorted({row["parent"] for row in config})
    missing_parents = [parent for parent in parents if parent not in ids]
    if missing_parents:
        print(
            "ERROR: в Bitrix24 не найдены родительские подразделения: "
            + ", ".join(map(str, missing_parents)),
            file=sys.stderr,
        )
        return 1

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "source_count": len(config),
        "created": [],
        "already_exists": [],
        "would_create": [],
        "errors": [],
    }

    print(f"Режим: {mode}")
    print(f"Подразделений в файле: {len(config)}")
    print(f"Родитель: {', '.join(map(str, parents))}")
    print("")

    for index, row in enumerate(config, start=1):
        key = (normalize_name(row["name"]), row["parent"])
        existing_dep = existing_by_key.get(key)

        if existing_dep is not None:
            dep_id = existing_dep.get("ID")
            print(f"[{index:02d}/{len(config)}] SKIP  ID={dep_id}: {row['name']}")
            report["already_exists"].append(
                {
                    "name": row["name"],
                    "parent": row["parent"],
                    "id": dep_id,
                }
            )
            continue

        if mode == "dry_run":
            print(f"[{index:02d}/{len(config)}] WOULD CREATE: {row['name']}")
            report["would_create"].append(dict(row))
            continue

        try:
            result = client.call(
                "department.add",
                {
                    "NAME": row["name"],
                    "PARENT": row["parent"],
                    "SORT": row["sort"],
                },
            )
            created_id = extract_created_id(result)
            print(f"[{index:02d}/{len(config)}] CREATED ID={created_id}: {row['name']}")

            created_row = {
                "name": row["name"],
                "parent": row["parent"],
                "sort": row["sort"],
                "id": created_id,
            }
            report["created"].append(created_row)

            existing_by_key[key] = {
                "ID": created_id,
                "NAME": row["name"],
                "PARENT": row["parent"],
            }

        except BitrixError as exc:
            message = f"{exc.code}: {exc.description}"
            print(f"[{index:02d}/{len(config)}] ERROR {row['name']}: {message}", file=sys.stderr)
            report["errors"].append(
                {
                    "name": row["name"],
                    "parent": row["parent"],
                    "error": message,
                }
            )
        except Exception as exc:
            message = str(exc)
            print(f"[{index:02d}/{len(config)}] ERROR {row['name']}: {message}", file=sys.stderr)
            report["errors"].append(
                {
                    "name": row["name"],
                    "parent": row["parent"],
                    "error": message,
                }
            )

    save_report(report)

    print("")
    print(
        "Итог: "
        f"создано={len(report['created'])}, "
        f"уже было={len(report['already_exists'])}, "
        f"к созданию={len(report['would_create'])}, "
        f"ошибок={len(report['errors'])}"
    )

    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
