from __future__ import annotations

import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

from processes.flowdesk_chatbot.config import (
    DETAIL_FIELDS,
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
ACTION_EDIT = "__edit__"
ACTION_EDIT_MORE = "__edit_more__"
ACTION_EDIT_REVIEW = "__edit_review__"
ACTION_EDIT_CANCEL = "__edit_cancel__"
ACTION_HISTORY_MORE = "__history_more__"
ACTION_ATTACHMENT_ADD = "__attachment_add__"
ACTION_ATTACHMENT_REPLACE = "__attachment_replace__"
ACTION_ATTACHMENT_DELETE = "__attachment_delete__"
EDIT_FIELD_PREFIX = "__edit_field__:"

EDIT_LABELS = {
    "request_type": "Тип обращения",
    "target": "Подразделение",
    "request": "Запрос",
    "request_detail": "Уточнение запроса",
    "description": "Суть обращения",
    "insurance_type": "Вид страхования",
    "product": "Класс страхования",
    "subproduct": "Продукт",
    "document": "Вложения / ссылка",
}


def parse_message_files(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract Drive file IDs from a Bitrix24 chat message event."""
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return []

    raw = (
        params.get("FILE_ID")
        or params.get("fileId")
        or params.get("file_id")
        or params.get("FILES")
        or params.get("files")
        or []
    )

    if isinstance(raw, dict):
        raw = list(raw.values())
    elif not isinstance(raw, (list, tuple, set)):
        raw = [raw] if raw not in (None, "", False) else []

    files: list[dict[str, Any]] = []
    seen: set[int] = set()

    for item in raw:
        file_id = 0
        name = ""

        if isinstance(item, dict):
            value = (
                item.get("id")
                or item.get("ID")
                or item.get("fileId")
                or item.get("FILE_ID")
                or item.get("value")
                or item.get("VALUE")
            )
            name = str(item.get("name") or item.get("NAME") or "").strip()
        else:
            value = item

        try:
            file_id = int(str(value).strip())
        except (TypeError, ValueError):
            file_id = 0

        if file_id <= 0 or file_id in seen:
            continue

        seen.add(file_id)
        files.append({"id": file_id, "name": name})

    return files


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
        "history_page": 0,
        "history_has_more": False,
        "edit_mode": False,
        "edit_root": None,
        "edit_backup": None,
        "edit_history": [],
        "last_edited_field": None,
        "attachment_edit_mode": None,
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
            "document_file_names": [],
            "document_comment": None,
        },
    }


def _snapshot(session: dict[str, Any]) -> None:
    saved = deepcopy(session)
    saved["history"] = []
    saved["edit_backup"] = None
    session["history"].append(saved)


def go_back(session: dict[str, Any]) -> bool:
    if not session["history"]:
        return False

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


def _button(label: str, action: str, style: str = "secondary") -> dict[str, str]:
    return {"label": label, "action": action, "style": style}


def _buttons(options: list[str], style: str = "secondary") -> list[dict[str, str]]:
    return [_button(option, option, style) for option in options]


def _detail_label(field_name: str) -> str:
    return str(DETAIL_FIELDS.get(field_name, {}).get("label") or field_name)


def _edit_label(field_key: str) -> str:
    if field_key.startswith("detail:"):
        return _detail_label(field_key.split(":", 1)[1])
    return EDIT_LABELS.get(field_key, field_key)


def _clear_edit_runtime(session: dict[str, Any]) -> None:
    """Drop transient editor state without touching the saved request data."""
    session["edit_mode"] = False
    session["edit_root"] = None
    session["edit_backup"] = None
    session["edit_history"] = []
    session["attachment_edit_mode"] = None


def _finish_edit(session: dict[str, Any], field_key: str | None = None) -> None:
    root = field_key or str(session.get("edit_root") or "")
    session["last_edited_field"] = _edit_label(root) if root else None
    session["current_screen"] = "edit_after"
    _clear_edit_runtime(session)


def _restore_edit_backup(session: dict[str, Any]) -> None:
    backup = session.get("edit_backup")
    if isinstance(backup, dict):
        session["data"] = deepcopy(backup.get("data") or session["data"])
        session["route"] = backup.get("route")
        session["detail_fields"] = list(backup.get("detail_fields") or [])
        session["detail_index"] = int(backup.get("detail_index") or 0)
        session["instruction_path"] = backup.get("instruction_path")
    _clear_edit_runtime(session)


def _edit_snapshot(session: dict[str, Any]) -> None:
    saved = {
        "data": deepcopy(session["data"]),
        "current_screen": session.get("current_screen"),
        "route": session.get("route"),
        "detail_fields": list(session.get("detail_fields") or []),
        "detail_index": int(session.get("detail_index") or 0),
        "instruction_path": deepcopy(session.get("instruction_path")),
        "attachment_edit_mode": session.get("attachment_edit_mode"),
    }
    session.setdefault("edit_history", []).append(saved)


def _edit_back(session: dict[str, Any]) -> bool:
    history = session.get("edit_history") or []
    if not history:
        return False

    previous = history.pop()
    session["data"] = deepcopy(previous["data"])
    session["current_screen"] = previous["current_screen"]
    session["route"] = previous["route"]
    session["detail_fields"] = list(previous["detail_fields"])
    session["detail_index"] = int(previous["detail_index"])
    session["instruction_path"] = deepcopy(previous["instruction_path"])
    session["attachment_edit_mode"] = previous["attachment_edit_mode"]
    session["edit_history"] = history
    return True


def _save_edit_backup(session: dict[str, Any]) -> None:
    session["edit_backup"] = {
        "data": deepcopy(session["data"]),
        "route": session.get("route"),
        "detail_fields": list(session.get("detail_fields") or []),
        "detail_index": int(session.get("detail_index") or 0),
        "instruction_path": deepcopy(session.get("instruction_path")),
    }


def _apply_route(session: dict[str, Any], node: dict[str, Any]) -> None:
    route = node["route"]
    session["route"] = route

    if route == "details":
        session["detail_fields"] = list(node.get("fields", []))
        session["detail_index"] = 0
        session["current_screen"] = "details"
        return

    if session.get("edit_mode"):
        _finish_edit(session)
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


def _start_edit(session: dict[str, Any], field_key: str) -> None:
    data = session["data"]
    _save_edit_backup(session)
    session["edit_mode"] = True
    session["edit_root"] = field_key
    session["edit_history"] = []
    session["last_edited_field"] = None

    if field_key == "request_type":
        data["request_type"] = None
        data["target"] = None
        data["request"] = None
        data["request_detail"] = None
        data["details"] = {}
        session["route"] = None
        session["current_screen"] = "type"
    elif field_key == "target":
        data["target"] = None
        data["request"] = None
        data["request_detail"] = None
        data["details"] = {}
        session["route"] = None
        session["current_screen"] = "target"
    elif field_key == "request":
        data["request"] = None
        data["request_detail"] = None
        data["details"] = {}
        session["route"] = None
        session["current_screen"] = "request"
    elif field_key == "request_detail":
        data["request_detail"] = None
        data["details"] = {}
        session["route"] = None
        session["current_screen"] = "request_detail"
    elif field_key.startswith("detail:"):
        field_name = field_key.split(":", 1)[1]
        session["detail_fields"] = [field_name]
        session["detail_index"] = 0
        session["current_screen"] = "details"
    elif field_key == "description":
        session["current_screen"] = "description"
    elif field_key == "insurance_type":
        data["insurance_type"] = None
        data["product"] = None
        data["subproduct"] = None
        session["current_screen"] = "insurance_type"
    elif field_key == "product":
        data["product"] = None
        data["subproduct"] = None
        session["current_screen"] = "product"
    elif field_key == "subproduct":
        data["subproduct"] = None
        session["current_screen"] = "subproduct"
    elif field_key == "document":
        session["current_screen"] = "edit_attachments"
    else:
        _restore_edit_backup(session)
        raise ValueError(f"Неизвестное поле для редактирования: {field_key}")


def _editable_fields(session: dict[str, Any]) -> list[tuple[str, str]]:
    data = session["data"]
    fields: list[tuple[str, str]] = []

    for key in ("request_type", "target", "request", "request_detail"):
        if data.get(key):
            fields.append((key, EDIT_LABELS[key]))

    for field_name, value in (data.get("details") or {}).items():
        if value not in (None, ""):
            fields.append((f"detail:{field_name}", _detail_label(field_name)))

    for key in ("description", "insurance_type", "product", "subproduct"):
        if data.get(key):
            fields.append((key, EDIT_LABELS[key]))

    # Keep attachments editable even when the user originally pressed
    # "Пропустить": the correction screen must allow adding them later.
    fields.append(("document", EDIT_LABELS["document"]))

    return fields


def _start_text() -> str:
    return (
        "[b]Привет! Я DeskFlow — твой помощник по обращениям 👋🙂[/b]\n"
        "Я помогу оформить обращение и передать его кураторам, "
        "если возник вопрос или сложность.\n\n"
        "[i]Выбирай подходящие варианты по шагам. Если ошибёшься — "
        "всегда можно вернуться кнопкой [b]← Назад[/b].[/i]\n\n"
        "[b]С чем я могу тебе помочь?[/b]"
    )


def view(session: dict[str, Any]) -> dict[str, Any]:
    screen = session["current_screen"]
    data = session["data"]
    buttons: list[dict[str, str]] = []
    accepts_text = False
    terminal = False

    if screen == "type":
        if session.get("edit_mode"):
            text = "✏️ [b]Выбери новый тип обращения.[/b]"
        else:
            text = _start_text()
        buttons = [
            _button("Запрос", "Запрос", "secondary"),
            _button("Предложение / Идея", "Предложение / Идея", "secondary"),
            _button("Вопрос / Уточнение", "Вопрос / Уточнение", "secondary"),
            _button("Горит контракт", "Горит контракт", "alert"),
            _button("Жалоба", "Жалоба", "alert"),
            _button("Другое", "Другое", "primary"),
        ]
        if not session.get("edit_mode") and session.get("history_has_more"):
            buttons.append(_button("Показать ещё", ACTION_HISTORY_MORE, "secondary"))

    elif screen == "target":
        text = (
            "[b]С каким подразделением связан вопрос?[/b]\n\n"
            "[i][b][u]Это не адресат обращения.[/u][/b] Здесь мы только уточняем, "
            "с каким подразделением возникла сложность. Само обращение будет "
            "передано кураторам.[/i]\n"
            "Так мне будет проще правильно собрать детали 🙂"
        )
        buttons = _buttons(visible_options(REQUEST_TREE))

    elif screen == "request":
        node = request_node(data)
        text = node.get("__prompt__", "💬 [b]Что именно нужно сделать?[/b]")
        buttons = _buttons(visible_options(node))

    elif screen == "request_detail":
        node = request_node(data)
        text = node.get("__prompt__", "💬 [b]Уточни, пожалуйста, запрос.[/b]")
        buttons = _buttons(visible_options(node))

    elif screen == "details":
        fields = session["detail_fields"]
        index = int(session["detail_index"])
        if index >= len(fields):
            raise RuntimeError("detail_index вышел за пределы detail_fields")
        field_name = fields[index]
        text = str(DETAIL_FIELDS[field_name]["prompt"])
        accepts_text = True

    elif screen == "description":
        if session.get("edit_mode"):
            text = (
                "✏️ [b]Напиши новую суть обращения.[/b]\n"
                "[i]Опиши ситуацию так подробно, как считаешь нужным.[/i]"
            )
        else:
            text = (
                "[b]Расскажи, пожалуйста, в чём нужна помощь ✍️[/b]\n"
                "[i]Опиши ситуацию так подробно, как считаешь нужным.[/i]"
            )
        accepts_text = True

    elif screen == "insurance_type":
        text = (
            "[b]К какому виду страхования относится обращение?[/b]\n"
            "[i]Если вопрос не связан с конкретным видом страхования, "
            "выбери [b]Не применимо[/b].[/i]"
        )
        buttons = _buttons(list(PRODUCT_TREE.keys()))
        buttons.append(_button("Не применимо", "Не применимо", "base"))

    elif screen == "product":
        insurance_type = data["insurance_type"]
        products = PRODUCT_TREE.get(insurance_type, {})
        text = "🛡️ [b]Выбери класс страхования.[/b]"
        buttons = _buttons(list(products.keys()))

    elif screen == "subproduct":
        insurance_type = data["insurance_type"]
        product = data["product"]
        options = PRODUCT_TREE.get(insurance_type, {}).get(product, [])
        text = (
            "📋 [b]Выбери продукт.[/b]\n"
            "[i]Если нужного варианта нет, вернись назад и проверь выбранный класс.[/i]"
        )
        buttons = _buttons(list(options))

    elif screen == "document":
        text = (
            "[b]Есть документ, фото или ссылка? 📎[/b]\n"
            "[i]Прикрепи всё необходимое одним сообщением. "
            "Если ничего добавлять не нужно — нажми [b]Пропустить[/b].[/i]"
        )
        accepts_text = True
        buttons = [_button("Пропустить", ACTION_SKIP, "base")]

    elif screen == "confirm":
        text = summary_text(session)
        buttons = [
            _button("Создать обращение", ACTION_CONFIRM, "primary"),
            _button("Исправить данные", ACTION_EDIT, "alert"),
        ]

    elif screen == "edit_menu":
        text = "✏️ [b]Что хочешь изменить?[/b]"
        for field_key, label in _editable_fields(session):
            buttons.append(
                _button(label, f"{EDIT_FIELD_PREFIX}{field_key}", "secondary")
            )
        buttons.append(_button("← Назад", ACTION_EDIT_REVIEW, "primary"))

    elif screen == "edit_after":
        label = session.get("last_edited_field")
        if label:
            text = f"✅ Готово, исправила [b]{label}[/b]. Что-нибудь ещё?"
        else:
            text = "✅ [b]Готово, исправила.[/b] Что-нибудь ещё?"
        buttons = [
            _button("Изменить ещё", ACTION_EDIT_MORE, "secondary"),
            _button("Вернуться к проверке", ACTION_EDIT_REVIEW, "primary"),
        ]

    elif screen == "edit_attachments":
        text = "📎 [b]Что сделать с вложениями?[/b]"
        buttons = [
            _button("Добавить файлы", ACTION_ATTACHMENT_ADD, "secondary"),
            _button("Заменить все вложения", ACTION_ATTACHMENT_REPLACE, "secondary"),
            _button("Удалить вложения", ACTION_ATTACHMENT_DELETE, "alert"),
            _button("← Назад", ACTION_EDIT_CANCEL, "primary"),
        ]

    elif screen == "edit_attachments_input":
        mode = session.get("attachment_edit_mode")
        if mode == "replace":
            text = (
                "📎 [b]Пришли новый пакет файлов, фото или ссылку.[/b]\n"
                "[i]Старые вложения будут заменены только после успешной отправки новых.[/i]"
            )
        else:
            text = "📎 [b]Пришли файлы, фото или ссылку, которые нужно добавить.[/b]"
        accepts_text = True
        buttons = [_button("← Назад", ACTION_EDIT_CANCEL, "primary")]

    elif screen == "instruction":
        path = tuple(session.get("instruction_path") or [])
        text = INSTRUCTIONS[path]
        terminal = True

    elif screen == "done":
        task_id = session.get("task_id")
        text = (
            "[b]Готово! Обращение создано ✅🙂[/b]\n"
            "[i]Кураторы уже увидят его в задачах.[/i]\n\n"
            f"[b]Номер обращения:[/b] #{task_id}"
            if task_id
            else "[b]Готово! Обращение создано ✅🙂[/b]"
        )
        buttons = [_button("Создать новое обращение", ACTION_NEW_REQUEST, "primary")]
        if session.get("history_has_more"):
            buttons.append(_button("Показать ещё", ACTION_HISTORY_MORE, "secondary"))
        terminal = True

    else:
        raise ValueError(f"Неизвестный экран: {screen}")

    if session.get("edit_mode") and screen not in {
        "edit_menu",
        "edit_after",
        "edit_attachments",
        "edit_attachments_input",
    }:
        buttons.append(_button("← Назад", ACTION_EDIT_CANCEL, "primary"))
    elif (
        session["history"]
        and screen not in {"done", "instruction", "edit_menu", "edit_after"}
        and not session.get("edit_mode")
    ):
        buttons.append(_button("← Назад", ACTION_BACK, "primary"))

    return {
        "screen": screen,
        "text": text,
        "buttons": buttons,
        "accepts_text": accepts_text,
        "terminal": terminal,
        "revision": int(session["revision"]),
    }


def _validate_detail(field_name: str, value: str) -> tuple[bool, str, str]:
    meta = DETAIL_FIELDS[field_name]

    if meta.get("validator") == "bin_iin":
        digits = "".join(re.findall(r"\d", value))
        if not digits:
            return (
                False,
                value,
                (
                    "[b]Не вижу здесь БИН / ИИН 👀[/b]\n"
                    "[i]Пришли номер из 12 цифр — остальное я обработаю сама.[/i]"
                ),
            )
        if len(digits) != 12:
            return (
                False,
                value,
                (
                    "[b]Хм, БИН / ИИН не распознался 🤔[/b]\n"
                    "[i]Нужны ровно 12 цифр. Напиши номер как удобно — "
                    "пробелы, тире и подписи я уберу сама.[/i]"
                ),
            )
        return True, digits, ""

    max_length = int(meta.get("max_length") or 0)
    if max_length and len(value) > max_length:
        return (
            False,
            value,
            (
                "[b]Ой, здесь получилось слишком много текста 🙈[/b]\n"
                f"[i]Для этого поля помещается до {max_length} символов. "
                "Сократи, пожалуйста, только этот ответ.[/i]"
            ),
        )

    return True, value, ""


def submit_action(session: dict[str, Any], action: str) -> dict[str, Any]:
    screen = session["current_screen"]

    if action == ACTION_BACK:
        if not go_back(session):
            return {"status": "ignored"}
        return {"status": "ok"}

    if action == ACTION_HISTORY_MORE:
        session["history_page"] = int(session.get("history_page") or 0) + 1
        _advance_revision(session)
        return {"status": "ok"}

    if action == ACTION_EDIT:
        # Entering the editor must always start clean. A stale transient flag
        # from an earlier cancelled edit must never leak into the new session.
        _clear_edit_runtime(session)
        session["current_screen"] = "edit_menu"
        _advance_revision(session)
        return {"status": "ok"}

    if action == ACTION_EDIT_MORE:
        _clear_edit_runtime(session)
        session["current_screen"] = "edit_menu"
        _advance_revision(session)
        return {"status": "ok"}

    if action == ACTION_EDIT_REVIEW:
        # "← Назад" from the correction menu is a pure no-op on request data:
        # close the editor and return to the exact same review screen.
        _clear_edit_runtime(session)
        session["current_screen"] = "confirm"
        session["history"] = []
        _advance_revision(session)
        return {"status": "ok"}

    if action == ACTION_EDIT_CANCEL:
        # Inside a multi-step replacement go back one editor step. If the user
        # has not changed anything yet, restore the original request atomically
        # and return to the correction menu.
        if not _edit_back(session):
            _restore_edit_backup(session)
            session["current_screen"] = "edit_menu"
        _advance_revision(session)
        return {"status": "ok"}

    if action.startswith(EDIT_FIELD_PREFIX):
        if screen != "edit_menu":
            raise ValueError("Редактирование поля доступно только из меню исправлений")
        field_key = action[len(EDIT_FIELD_PREFIX):]
        _start_edit(session, field_key)
        _advance_revision(session)
        return {"status": "ok"}

    if action == ACTION_ATTACHMENT_ADD:
        _edit_snapshot(session)
        session["attachment_edit_mode"] = "add"
        session["current_screen"] = "edit_attachments_input"
        _advance_revision(session)
        return {"status": "ok"}

    if action == ACTION_ATTACHMENT_REPLACE:
        _edit_snapshot(session)
        session["attachment_edit_mode"] = "replace"
        session["current_screen"] = "edit_attachments_input"
        _advance_revision(session)
        return {"status": "ok"}

    if action == ACTION_ATTACHMENT_DELETE:
        data = session["data"]
        data["document"] = None
        data["document_file_ids"] = []
        data["document_file_names"] = []
        data["document_comment"] = None
        _finish_edit(session, "document")
        _advance_revision(session)
        return {"status": "ok"}

    current = view(session)
    allowed = {item["action"] for item in current["buttons"]}
    if action not in allowed:
        raise ValueError("Кнопка не относится к текущему экрану")

    if action == ACTION_CONFIRM:
        return {"status": "task_ready"}

    if action == ACTION_NEW_REQUEST:
        return {"status": "restart_requested"}

    if session.get("edit_mode"):
        _edit_snapshot(session)
    else:
        _snapshot(session)

    data = session["data"]

    if screen == "type":
        data["request_type"] = action
        route = REQUEST_TYPES[action]
        if session.get("edit_mode"):
            if route == "request_tree":
                session["current_screen"] = "target"
            else:
                _finish_edit(session)
        elif route == "request_tree":
            session["current_screen"] = "target"
        elif route == "branch_3":
            session["current_screen"] = "description"
        else:
            raise ValueError(f"Неизвестный стартовый маршрут: {route}")

    elif screen == "target":
        data["target"] = action
        node = REQUEST_TREE[action]
        if "route" in node:
            _apply_route(session, node)
        else:
            session["current_screen"] = "request"

    elif screen == "request":
        node = request_node(data)
        data["request"] = action
        selected = node[action]
        if "route" in selected:
            _apply_route(session, selected)
        else:
            session["current_screen"] = "request_detail"

    elif screen == "request_detail":
        node = request_node(data)
        data["request_detail"] = action
        _apply_route(session, node[action])

    elif screen == "insurance_type":
        data["insurance_type"] = action
        if action == "Не применимо":
            data["product"] = None
            data["subproduct"] = None
            if session.get("edit_mode"):
                _finish_edit(session)
            else:
                session["current_screen"] = "document"
        else:
            session["current_screen"] = "product"

    elif screen == "product":
        data["product"] = action
        options = PRODUCT_TREE[data["insurance_type"]][action]
        if options:
            session["current_screen"] = "subproduct"
        elif session.get("edit_mode"):
            _finish_edit(session)
        else:
            session["current_screen"] = "document"

    elif screen == "subproduct":
        data["subproduct"] = action
        if session.get("edit_mode"):
            _finish_edit(session)
        else:
            session["current_screen"] = "document"

    elif screen == "document" and action == ACTION_SKIP:
        data["document"] = None
        data["document_file_ids"] = []
        data["document_file_names"] = []
        data["document_comment"] = None
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
    if screen == "edit_attachments_input":
        return submit_attachment_message(session, [], [], value)

    if screen == "details":
        fields = session["detail_fields"]
        index = int(session["detail_index"])
        field_name = fields[index]
        ok, normalized, error = _validate_detail(field_name, value)
        if not ok:
            return {"status": "validation_error", "message": error}

        if session.get("edit_mode"):
            _edit_snapshot(session)
        else:
            _snapshot(session)

        session["data"]["details"][field_name] = normalized
        session["detail_index"] = index + 1
        if session["detail_index"] >= len(fields):
            if session.get("edit_mode"):
                _finish_edit(session)
            else:
                session["current_screen"] = "description"

    elif screen == "description":
        if session.get("edit_mode"):
            _edit_snapshot(session)
        else:
            _snapshot(session)
        session["data"]["description"] = value
        if session.get("edit_mode"):
            _finish_edit(session, "description")
        else:
            session["current_screen"] = "insurance_type"

    elif screen == "document":
        if not session.get("edit_mode"):
            _snapshot(session)
        session["data"]["document"] = value
        session["data"]["document_file_ids"] = []
        session["data"]["document_file_names"] = []
        session["data"]["document_comment"] = None
        session["current_screen"] = "confirm"

    else:
        raise ValueError(f"Текстовый ввод не поддерживается на экране {screen!r}")

    _advance_revision(session)
    return {"status": "ok"}


def submit_attachment_message(
    session: dict[str, Any],
    file_ids: list[int],
    file_names: list[str],
    text: str = "",
) -> dict[str, Any]:
    screen = session["current_screen"]
    value = str(text or "").strip()
    ids = [int(x) for x in file_ids if int(x) > 0]
    names = [str(x).strip() for x in file_names if str(x).strip()]

    if not ids and not value:
        return {"status": "empty"}

    data = session["data"]

    if screen == "document":
        _snapshot(session)
        if ids:
            data["document_file_ids"] = list(dict.fromkeys(ids))
            data["document_file_names"] = names
            data["document_comment"] = value or None
            data["document"] = None
        else:
            data["document"] = value
            data["document_file_ids"] = []
            data["document_file_names"] = []
            data["document_comment"] = None
        session["current_screen"] = "confirm"

    elif screen == "edit_attachments_input":
        _edit_snapshot(session)
        mode = str(session.get("attachment_edit_mode") or "add")
        if mode == "replace":
            if ids:
                data["document_file_ids"] = list(dict.fromkeys(ids))
                data["document_file_names"] = names
                data["document_comment"] = value or None
                data["document"] = None
            else:
                data["document"] = value
                data["document_file_ids"] = []
                data["document_file_names"] = []
                data["document_comment"] = None
        else:
            existing_ids = [
                int(x) for x in (data.get("document_file_ids") or []) if int(x) > 0
            ]
            data["document_file_ids"] = list(dict.fromkeys(existing_ids + ids))
            existing_names = list(data.get("document_file_names") or [])
            data["document_file_names"] = list(dict.fromkeys(existing_names + names))
            if ids and value:
                previous = str(data.get("document_comment") or "").strip()
                data["document_comment"] = (
                    f"{previous}\n{value}".strip() if previous else value
                )
            elif value and not ids:
                previous = str(data.get("document") or "").strip()
                data["document"] = f"{previous}\n{value}".strip() if previous else value

        _finish_edit(session, "document")

    else:
        return {"status": "buttons_expected"}

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
    session["history_page"] = 0
    session["history_has_more"] = False
    _advance_revision(session)


def _file_count_text(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "файл"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        word = "файла"
    else:
        word = "файлов"
    return f"{count} {word}"


def summary_text(session: dict[str, Any]) -> str:
    data = session["data"]
    lines = [
        "[b]Почти готово — давай всё проверим 👀[/b]",
        "",
    ]

    pairs = [
        ("Тип обращения", data.get("request_type")),
        ("Подразделение", data.get("target")),
        ("Запрос", data.get("request")),
        ("Уточнение запроса", data.get("request_detail")),
    ]

    for label, value in pairs:
        if value:
            lines.append(f"[b]{label}:[/b] {value}")

    for field_name, value in (data.get("details") or {}).items():
        if value not in (None, ""):
            lines.append(f"[b]{_detail_label(field_name)}:[/b] {value}")

    for label, value in (
        ("Суть обращения", data.get("description")),
        ("Вид страхования", data.get("insurance_type")),
        ("Класс страхования", data.get("product")),
        ("Продукт", data.get("subproduct")),
        ("Документ / ссылка", data.get("document")),
    ):
        if value:
            lines.append(f"[b]{label}:[/b] {value}")

    file_ids = data.get("document_file_ids") or []
    if file_ids:
        names = [str(x) for x in (data.get("document_file_names") or []) if x]
        suffix = f" — {', '.join(names)}" if names else ""
        lines.append(f"[b]Вложения:[/b] {_file_count_text(len(file_ids))}{suffix}")

    if data.get("document_comment"):
        lines.append(
            f"[b]Комментарий к вложению:[/b] {data['document_comment']}"
        )

    lines.extend(
        [
            "",
            "[i]Если всё верно — создавай обращение. "
            "Если заметила ошибку, можно изменить только нужное поле 🙂[/i]",
        ]
    )
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
    return title[:250] if title else "DeskFlow обращение"


def _details_text(session: dict[str, Any]) -> str:
    details = session["data"].get("details") or {}
    lines = []
    for field_name, value in details.items():
        if value not in (None, ""):
            lines.append(f"{_detail_label(field_name)}: {value}")
    return "\n".join(lines)


def task_registry_fields(session: dict[str, Any]) -> dict[str, str]:
    data = session["data"]
    mapping = {
        "UF_FLOWDESK_REQUEST_TYPE": data.get("request_type"),
        "UF_FLOWDESK_TARGET": data.get("target"),
        "UF_FLOWDESK_REQUEST": data.get("request"),
        "UF_FLOWDESK_REQUEST_DETAIL": data.get("request_detail"),
        "UF_FLOWDESK_INSURANCE_TYPE": data.get("insurance_type"),
        "UF_FLOWDESK_INSURANCE_CLASS": data.get("product"),
        "UF_FLOWDESK_PRODUCT": data.get("subproduct"),
        "UF_FLOWDESK_DETAILS": _details_text(session),
        "UF_FLOWDESK_DESCRIPTION": data.get("description"),
        "UF_FLOWDESK_INITIATOR_ID": str(session.get("user_id") or ""),
    }
    return {
        key: str(value)
        for key, value in mapping.items()
        if value not in (None, "")
    }


def task_description(session: dict[str, Any]) -> str:
    data = session["data"]
    lines: list[str] = []

    for label, value in (
        ("Тип обращения", data.get("request_type")),
        ("Подразделение", data.get("target")),
        ("Запрос", data.get("request")),
        ("Уточнение запроса", data.get("request_detail")),
    ):
        if value:
            lines.append(f"{label}: {value}")

    details_text = _details_text(session)
    if details_text:
        lines.append("")
        lines.append("Детализация:")
        lines.append(details_text)

    for label, value in (
        ("Суть обращения", data.get("description")),
        ("Вид страхования", data.get("insurance_type")),
        ("Класс страхования", data.get("product")),
        ("Продукт", data.get("subproduct")),
        ("Документ / ссылка", data.get("document")),
    ):
        if value:
            lines.append(f"{label}: {value}")

    file_ids = data.get("document_file_ids") or []
    if file_ids:
        lines.append(f"Вложения: {_file_count_text(len(file_ids))}")

    return "\n".join(lines).strip()
