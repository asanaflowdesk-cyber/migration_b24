from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from processes.flowdesk_chatbot.config import (
    DETAIL_QUESTIONS,
    INSTRUCTIONS,
    PRODUCT_TREE,
    REQUEST_TREE,
    REQUEST_TYPES,
)

META_KEYS = {"__prompt__", "route", "fields"}

ACTION_BACK = "__back__"
ACTION_SKIP = "__skip__"
ACTION_CONFIRM = "__confirm__"
ACTION_NEW_REQUEST = "__new_request__"


def new_session(user_id: int, dialog_id: str) -> dict[str, Any]:
    return {
        "user_id": int(user_id),
        "dialog_id": str(dialog_id),
        "request_id": uuid4().hex,
        "current_screen": "type",
        "revision": 1,
        "history": [],
        "route": None,
        "detail_fields": [],
        "detail_index": 0,
        "instruction_path": None,
        "task_id": None,
        "active_message_id": None,
        "data": {
            "request_type": None,
            "target": None,
            "request": None,
            "request_detail": None,
            "details": {},
            "description": None,
            "insurance_type": None,
            "product": None,
            "subproduct": None,
            "document": None,
            "document_file_ids": [],
        },
    }


def _snapshot(session: dict[str, Any]) -> None:
    saved = deepcopy(session)
    saved["history"] = []
    session["history"].append(saved)


def go_back(session: dict[str, Any]) -> bool:
    if not session["history"]:
        return False

    # revision must be monotonic. It is deliberately NOT restored from history,
    # otherwise an old Bitrix keyboard could become valid again after Back.
    new_revision = int(session["revision"]) + 1
    previous = session["history"].pop()
    history = session["history"]

    session.clear()
    session.update(previous)
    session["history"] = history
    session["revision"] = new_revision
    return True


def _advance_revision(session: dict[str, Any]) -> None:
    session["revision"] = int(session["revision"]) + 1


def visible_options(node: dict[str, Any]) -> list[str]:
    return [key for key in node.keys() if key not in META_KEYS]


def request_node(data: dict[str, Any]) -> dict[str, Any]:
    node = REQUEST_TREE
    for key in ("target", "request", "request_detail"):
        value = data.get(key)
        if value:
            node = node[value]
    return node


def _apply_route(session: dict[str, Any], node: dict[str, Any]) -> None:
    route = node["route"]
    session["route"] = route

    if route == "details":
        session["detail_fields"] = list(node.get("fields", []))
        session["detail_index"] = 0
        session["current_screen"] = "details"
        return

    if route == "branch_3":
        session["current_screen"] = "description"
        return

    if route == "instruction":
        path = tuple(
            value
            for value in (
                session["data"]["target"],
                session["data"]["request"],
                session["data"]["request_detail"],
            )
            if value
        )
        session["instruction_path"] = list(path)
        session["current_screen"] = "instruction" if path in INSTRUCTIONS else "description"
        return

    raise ValueError(f"Неизвестный маршрут: {route}")


def _buttons(options: list[str]) -> list[dict[str, str]]:
    return [{"label": option, "action": option} for option in options]


def view(session: dict[str, Any]) -> dict[str, Any]:
    screen = session["current_screen"]
    data = session["data"]
    buttons: list[dict[str, str]] = []
    accepts_text = False
    terminal = False

    if screen == "type":
        text = "Мы вас слышим. Выберите тип обращения:"
        buttons = _buttons(list(REQUEST_TYPES.keys()))

    elif screen == "target":
        text = "К кому обращение?"
        buttons = _buttons(visible_options(REQUEST_TREE))

    elif screen == "request":
        node = request_node(data)
        text = node.get("__prompt__", "Выберите запрос:")
        buttons = _buttons(visible_options(node))

    elif screen == "request_detail":
        node = request_node(data)
        text = node.get("__prompt__", "Уточните запрос:")
        buttons = _buttons(visible_options(node))

    elif screen == "details":
        fields = session["detail_fields"]
        index = int(session["detail_index"])
        if index >= len(fields):
            raise RuntimeError("detail_index вышел за пределы detail_fields")
        field_name = fields[index]
        text = DETAIL_QUESTIONS[field_name]
        accepts_text = True

    elif screen == "description":
        text = "Опишите суть обращения одним сообщением:"
        accepts_text = True

    elif screen == "insurance_type":
        text = "Выберите вид страхования:"
        buttons = _buttons(list(PRODUCT_TREE.keys()) + ["Не применимо"])

    elif screen == "product":
        insurance_type = data["insurance_type"]
        products = PRODUCT_TREE.get(insurance_type, {})
        text = "Выберите продукт:"
        buttons = _buttons(list(products.keys()))

    elif screen == "subproduct":
        insurance_type = data["insurance_type"]
        product = data["product"]
        options = PRODUCT_TREE.get(insurance_type, {}).get(product, [])
        text = "Выберите подпродукт:"
        buttons = _buttons(list(options))

    elif screen == "document":
        text = "Пришлите документ или ссылку одним сообщением. Если документа нет — нажмите «Пропустить»."
        accepts_text = True
        buttons = [{"label": "Пропустить", "action": ACTION_SKIP}]

    elif screen == "confirm":
        text = summary_text(session)
        buttons = [{"label": "Создать задачу", "action": ACTION_CONFIRM}]

    elif screen == "instruction":
        path = tuple(session.get("instruction_path") or [])
        text = INSTRUCTIONS[path]
        terminal = True

    elif screen == "done":
        task_id = session.get("task_id")
        text = f"Задача создана: #{task_id}" if task_id else "Сценарий завершён."
        buttons = [{"label": "Создать новое обращение", "action": ACTION_NEW_REQUEST}]
        accepts_text = True
        terminal = True

    else:
        raise ValueError(f"Неизвестный экран: {screen}")

    if session["history"] and screen not in {"done", "instruction"}:
        buttons.append({"label": "← Назад", "action": ACTION_BACK})

    return {
        "screen": screen,
        "text": text,
        "buttons": buttons,
        "accepts_text": accepts_text,
        "terminal": terminal,
        "revision": int(session["revision"]),
    }


def submit_action(session: dict[str, Any], action: str) -> dict[str, Any]:
    screen = session["current_screen"]

    if action == ACTION_BACK:
        if not go_back(session):
            return {"status": "ignored"}
        return {"status": "ok"}

    current = view(session)
    allowed = {item["action"] for item in current["buttons"]}
    if action not in allowed:
        raise ValueError("Кнопка не относится к текущему экрану")

    if action == ACTION_CONFIRM:
        return {"status": "task_ready"}

    if action == ACTION_NEW_REQUEST:
        return {"status": "restart_requested"}

    _snapshot(session)

    if screen == "type":
        session["data"]["request_type"] = action
        route = REQUEST_TYPES[action]
        if route == "request_tree":
            session["current_screen"] = "target"
        elif route == "branch_3":
            session["current_screen"] = "description"
        else:
            raise ValueError(f"Неизвестный стартовый маршрут: {route}")

    elif screen == "target":
        session["data"]["target"] = action
        node = REQUEST_TREE[action]
        if "route" in node:
            _apply_route(session, node)
        else:
            session["current_screen"] = "request"

    elif screen == "request":
        node = request_node(session["data"])
        session["data"]["request"] = action
        selected = node[action]
        if "route" in selected:
            _apply_route(session, selected)
        else:
            session["current_screen"] = "request_detail"

    elif screen == "request_detail":
        node = request_node(session["data"])
        session["data"]["request_detail"] = action
        _apply_route(session, node[action])

    elif screen == "insurance_type":
        session["data"]["insurance_type"] = action
        if action == "Не применимо":
            session["data"]["product"] = None
            session["data"]["subproduct"] = None
            session["current_screen"] = "document"
        else:
            session["current_screen"] = "product"

    elif screen == "product":
        session["data"]["product"] = action
        options = PRODUCT_TREE[session["data"]["insurance_type"]][action]
        session["current_screen"] = "subproduct" if options else "document"

    elif screen == "subproduct":
        session["data"]["subproduct"] = action
        session["current_screen"] = "document"

    elif screen == "document" and action == ACTION_SKIP:
        session["data"]["document"] = None
        session["current_screen"] = "confirm"

    else:
        raise ValueError(f"На экране {screen!r} кнопка {action!r} не поддерживается")

    _advance_revision(session)
    return {"status": "ok"}


def submit_text(session: dict[str, Any], text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    current = view(session)
    if not current["accepts_text"]:
        return {"status": "buttons_expected"}
    if not value:
        return {"status": "empty"}

    screen = session["current_screen"]
    _snapshot(session)

    if screen == "details":
        fields = session["detail_fields"]
        index = int(session["detail_index"])
        field_name = fields[index]
        session["data"]["details"][field_name] = value
        session["detail_index"] = index + 1
        if session["detail_index"] >= len(fields):
            session["current_screen"] = "description"

    elif screen == "description":
        session["data"]["description"] = value
        session["current_screen"] = "insurance_type"

    elif screen == "document":
        session["data"]["document"] = value
        session["current_screen"] = "confirm"

    else:
        raise ValueError(f"Текстовый ввод не поддерживается на экране {screen!r}")

    _advance_revision(session)
    return {"status": "ok"}


def finish_instruction(session: dict[str, Any]) -> None:
    if session["current_screen"] != "instruction":
        return
    session["current_screen"] = "done"
    _advance_revision(session)


def mark_task_created(session: dict[str, Any], task_id: int | str) -> None:
    session["task_id"] = str(task_id)
    session["current_screen"] = "done"
    session["history"] = []
    _advance_revision(session)


def summary_text(session: dict[str, Any]) -> str:
    data = session["data"]
    lines = ["Проверьте обращение:"]

    pairs = [
        ("Тип", data.get("request_type")),
        ("Кому", data.get("target")),
        ("Запрос", data.get("request")),
        ("Уточнение", data.get("request_detail")),
    ]

    for label, value in pairs:
        if value:
            lines.append(f"• {label}: {value}")

    for field_name, value in (data.get("details") or {}).items():
        if value:
            label = DETAIL_QUESTIONS.get(field_name, field_name).rstrip(":")
            lines.append(f"• {label}: {value}")

    for label, value in (
        ("Суть", data.get("description")),
        ("Вид страхования", data.get("insurance_type")),
        ("Продукт", data.get("product")),
        ("Подпродукт", data.get("subproduct")),
        ("Документ", data.get("document")),
    ):
        if value:
            lines.append(f"• {label}: {value}")

    return "\n".join(lines)


def task_title(session: dict[str, Any]) -> str:
    data = session["data"]
    parts = [
        data.get("request_type"),
        data.get("target"),
        data.get("request"),
        data.get("product"),
    ]
    compact = [str(x).strip() for x in parts if x]
    title = " | ".join(compact)
    return title[:250] if title else "FlowDesk обращение"


def task_description(session: dict[str, Any]) -> str:
    data = session["data"]
    lines: list[str] = []

    for label, value in (
        ("Тип обращения", data.get("request_type")),
        ("Кому", data.get("target")),
        ("Запрос", data.get("request")),
        ("Уточнение запроса", data.get("request_detail")),
    ):
        if value:
            lines.append(f"{label}: {value}")

    details = data.get("details") or {}
    if details:
        lines.append("")
        lines.append("Детализация:")
        for field_name, value in details.items():
            if value:
                label = DETAIL_QUESTIONS.get(field_name, field_name).rstrip(":")
                lines.append(f"{label}: {value}")

    for label, value in (
        ("Суть обращения", data.get("description")),
        ("Вид страхования", data.get("insurance_type")),
        ("Продукт", data.get("product")),
        ("Подпродукт", data.get("subproduct")),
        ("Документ / ссылка", data.get("document")),
    ):
        if value:
            lines.append(f"{label}: {value}")

    return "\n".join(lines).strip()
