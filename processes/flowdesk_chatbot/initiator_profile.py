"""Read the initiating employee and their department heads via Bitrix REST."""
from __future__ import annotations

from typing import Any, Callable


def _rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        for key in ("result", "items", "users", "departments"):
            if isinstance(result.get(key), list):
                return _rows(result[key])
    return []


def _positive_id(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _full_name(user: dict[str, Any]) -> str:
    return " ".join(str(user.get(key) or "").strip() for key in ("LAST_NAME", "NAME", "SECOND_NAME") if user.get(key))


def fetch_profile(call: Callable[..., Any], user_id: int) -> dict[str, str]:
    user = next((row for row in _rows(call("user.get", {"FILTER": {"ID": int(user_id)}})) if _positive_id(row.get("ID")) == int(user_id)), {})
    profile = {
        "name": _full_name(user), "department": "", "manager": "",
        "contact": "; ".join(str(user.get(key) or "").strip() for key in ("EMAIL", "WORK_PHONE", "UF_PHONE_INNER") if user.get(key)),
    }
    ids = user.get("UF_DEPARTMENT") or []
    if not isinstance(ids, (list, tuple)):
        ids = [ids]
    department_names: list[str] = []
    manager_ids: set[int] = set()
    for department_id in ids:
        current = _positive_id(department_id)
        seen: set[int] = set()
        first = True
        while current and current not in seen and len(seen) < 10:
            seen.add(current)
            try:
                department = next((row for row in _rows(call("department.get", {"ID": current})) if _positive_id(row.get("ID")) == current), {})
            except Exception:
                break  # Missing fields are collected from the employee once.
            if first and department.get("NAME"):
                department_names.append(str(department["NAME"]))
            first = False
            head_id = _positive_id(department.get("UF_HEAD"))
            if head_id and head_id != int(user_id):
                manager_ids.add(head_id)
                break
            current = _positive_id(department.get("PARENT"))
    profile["department"] = "; ".join(dict.fromkeys(department_names))
    # Multiple different heads are ambiguous: the employee supplies the manager.
    if len(manager_ids) == 1:
        manager_id = next(iter(manager_ids))
        try:
            manager = next((row for row in _rows(call("user.get", {"FILTER": {"ID": manager_id}})) if _positive_id(row.get("ID")) == manager_id), {})
            profile["manager"] = _full_name(manager)
        except Exception:
            pass
    return profile
