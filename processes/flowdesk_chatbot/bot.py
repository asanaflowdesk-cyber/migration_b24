from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from processes.flowdesk_chatbot.config import (
    DETAIL_QUESTIONS,
    INSTRUCTIONS,
    PRODUCT_TREE,
    REQUEST_TREE,
    REQUEST_TYPES,
)


BACK = "__BACK__"
META_KEYS = {"__prompt__", "route", "fields"}


def new_session(user_id: int) -> dict[str, Any]:
    """Создать новую сессию обращения для одного пользователя."""
    return {
        "user_id": user_id,
        "current_screen": "type",
        "screen_id": 1,
        "history": [],
        "route": None,
        "detail_fields": [],
        "detail_index": 0,
        "instruction_path": None,
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
        },
    }


def snapshot(session: dict[str, Any]) -> None:
    """Сохранить состояние перед переходом на следующий экран."""
    saved = deepcopy(session)
    saved["history"] = []
    session["history"].append(saved)


def go_back(session: dict[str, Any]) -> bool:
    """Вернуться на предыдущий экран и восстановить прошлые данные."""
    if not session["history"]:
        return False

    previous = session["history"].pop()
    history = session["history"]

    session.clear()
    session.update(previous)
    session["history"] = history

    # Даже если вернулись на тот же логический экран,
    # это новый показ. Старые кнопки позже будут отклоняться по screen_id.
    session["screen_id"] += 1
    return True


def visible_options(node: dict[str, Any]) -> list[str]:
    """Получить только пользовательские варианты, без служебных ключей."""
    return [
        key
        for key in node.keys()
        if key not in META_KEYS
    ]


def choose(
    prompt: str,
    options: list[str],
    allow_back: bool = True,
) -> str:
    """Временный интерфейс для терминала. Позже его заменит клавиатура Bitrix."""
    while True:
        print(f"\n{prompt}")

        for index, option in enumerate(options, start=1):
            print(f"{index}. {option}")

        if allow_back:
            print("0. ← Назад")

        raw = input("> ").strip()

        if allow_back and raw == "0":
            return BACK

        if raw.isdigit():
            number = int(raw)

            if 1 <= number <= len(options):
                return options[number - 1]

        print("Выберите пункт из списка.")


def ask_text(
    prompt: str,
    allow_back: bool = True,
    optional: bool = False,
) -> str:
    """Временный текстовый ввод. Позже это будет сообщение пользователя в чате."""
    while True:
        suffix = " (Enter — пропустить)" if optional else ""
        print(f"\n{prompt}{suffix}")

        if allow_back:
            print("0 — ← Назад")

        value = input("> ").strip()

        if allow_back and value == "0":
            return BACK

        if value or optional:
            return value

        print("Поле не должно быть пустым.")


def request_node(data: dict[str, Any]) -> dict[str, Any]:
    """Найти текущий узел ветки 1 по уже сделанным выборам."""
    node = REQUEST_TREE

    for key in ("target", "request", "request_detail"):
        value = data.get(key)

        if value:
            node = node[value]

    return node


def apply_route(
    session: dict[str, Any],
    node: dict[str, Any],
) -> None:
    """Перевести пользователя в ветку 2, 3 или 4."""
    route = node["route"]
    session["route"] = route

    # Ветка 2: детализация.
    if route == "details":
        session["detail_fields"] = node.get("fields", [])
        session["detail_index"] = 0
        session["current_screen"] = "details"
        return

    # Ветка 3: общее обращение.
    if route == "branch_3":
        session["current_screen"] = "description"
        return

    # Ветка 4: инструкция.
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

        session["instruction_path"] = path

        if path in INSTRUCTIONS:
            session["current_screen"] = "instruction"
        else:
            # Пока инструкции нет — временно ведём по ветке 3.
            session["current_screen"] = "description"

        return

    raise ValueError(f"Неизвестный маршрут: {route}")


def handle_type(session: dict[str, Any]) -> None:
    choice = choose(
        "Мы вас слышим. Выберите тип обращения:",
        list(REQUEST_TYPES.keys()),
        allow_back=False,
    )

    snapshot(session)
    session["data"]["request_type"] = choice

    route = REQUEST_TYPES[choice]

    if route == "request_tree":
        session["current_screen"] = "target"
    elif route == "branch_3":
        session["current_screen"] = "description"
    else:
        raise ValueError(f"Неизвестный стартовый маршрут: {route}")

    session["screen_id"] += 1


def handle_target(session: dict[str, Any]) -> None:
    choice = choose(
        "К кому обращение?",
        visible_options(REQUEST_TREE),
    )

    if choice == BACK:
        go_back(session)
        return

    snapshot(session)
    session["data"]["target"] = choice

    node = REQUEST_TREE[choice]

    # Например Предконтроль сразу ведёт в ветку 2.
    if "route" in node:
        apply_route(session, node)
    else:
        session["current_screen"] = "request"

    session["screen_id"] += 1


def handle_request(session: dict[str, Any]) -> None:
    node = request_node(session["data"])

    prompt = node.get(
        "__prompt__",
        "Выберите запрос:",
    )

    choice = choose(
        prompt,
        visible_options(node),
    )

    if choice == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["request"] = choice
    selected = node[choice]

    if "route" in selected:
        apply_route(session, selected)
    else:
        # Есть ещё уровень "Запрос детализация".
        session["current_screen"] = "request_detail"

    session["screen_id"] += 1


def handle_request_detail(session: dict[str, Any]) -> None:
    node = request_node(session["data"])

    prompt = node.get(
        "__prompt__",
        "Уточните запрос:",
    )

    choice = choose(
        prompt,
        visible_options(node),
    )

    if choice == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["request_detail"] = choice
    selected = node[choice]

    apply_route(
        session,
        selected,
    )

    session["screen_id"] += 1


def handle_details(session: dict[str, Any]) -> None:
    fields = session["detail_fields"]
    index = session["detail_index"]

    if index >= len(fields):
        session["current_screen"] = "description"
        return

    field_name = fields[index]
    prompt = DETAIL_QUESTIONS[field_name]

    value = ask_text(prompt)

    if value == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["details"][field_name] = value
    session["detail_index"] += 1
    session["screen_id"] += 1

    # После последнего вопроса ветки 2 автоматически переходим в ветку 3.
    if session["detail_index"] >= len(fields):
        session["current_screen"] = "description"


def handle_description(session: dict[str, Any]) -> None:
    value = ask_text(
        "Опишите суть обращения:"
    )

    if value == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["description"] = value

    # Ветка 3 теперь идёт не сразу к продукту,
    # а сначала к виду страхования.
    session["current_screen"] = "insurance_type"

    session["screen_id"] += 1


def handle_insurance_type(session: dict[str, Any]) -> None:
    value = choose(
        "Выберите вид страхования:",
        list(PRODUCT_TREE.keys()),
    )

    if value == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["insurance_type"] = value
    session["current_screen"] = "product"

    session["screen_id"] += 1


def handle_product(session: dict[str, Any]) -> None:
    insurance_type = session["data"]["insurance_type"]
    products = PRODUCT_TREE[insurance_type]

    # Пока реальные продукты ещё не заполнены,
    # разрешаем тестировать сценарий текстовым вводом.
    if products:
        value = choose(
            "Выберите продукт:",
            list(products.keys()),
        )
    else:
        value = ask_text(
            f"Введите продукт для вида страхования «{insurance_type}»:"
        )

    if value == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["product"] = value

    # Если продукт взят из PRODUCT_TREE,
    # его подпродукты уже известны.
    subproducts = products.get(value, []) if products else []

    if subproducts:
        session["current_screen"] = "subproduct"
    elif products:
        # Для продукта явно задан пустой список подпродуктов.
        session["current_screen"] = "document"
    else:
        # Пока PRODUCT_TREE не заполнен, дадим ввести подпродукт вручную.
        session["current_screen"] = "subproduct"

    session["screen_id"] += 1


def handle_subproduct(session: dict[str, Any]) -> None:
    insurance_type = session["data"]["insurance_type"]
    product = session["data"]["product"]

    products = PRODUCT_TREE[insurance_type]
    options = products.get(product, []) if products else []

    if options:
        value = choose(
            "Выберите подпродукт:",
            options,
        )
    else:
        value = ask_text(
            "Введите подпродукт:",
            optional=True,
        )

    if value == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["subproduct"] = value or None
    session["current_screen"] = "document"

    session["screen_id"] += 1


def handle_document(session: dict[str, Any]) -> None:
    value = ask_text(
        "Укажите документ или ссылку на него:",
        optional=True,
    )

    if value == BACK:
        go_back(session)
        return

    snapshot(session)

    session["data"]["document"] = value or None
    session["current_screen"] = "confirm"

    session["screen_id"] += 1


def build_task_data(session: dict[str, Any]) -> dict[str, Any]:
    """Собрать единый объект, который позже уйдёт в поля задачи Bitrix."""
    data = session["data"]

    return {
        "customer_id": session["user_id"],
        "created_by": session["user_id"],
        "request_type": data["request_type"],
        "target": data["target"],
        "request": data["request"],
        "request_detail": data["request_detail"],
        "details": deepcopy(data["details"]),
        "description": data["description"],
        "insurance_type": data["insurance_type"],
        "product": data["product"],
        "subproduct": data["subproduct"],
        "document": data["document"],
        "created_at": datetime.now().isoformat(
            timespec="seconds"
        ),
    }


def print_summary(session: dict[str, Any]) -> None:
    task = build_task_data(session)

    print("\nПроверьте обращение:")

    for key, value in task.items():
        if value not in (None, "", {}, []):
            print(f"- {key}: {value}")


def handle_confirm(session: dict[str, Any]) -> None:
    print_summary(session)

    choice = choose(
        "Всё верно?",
        ["Создать задачу"],
    )

    if choice == BACK:
        go_back(session)
        return

    snapshot(session)

    task_data = build_task_data(session)

    print("\nЗадача подготовлена:")
    print(task_data)

    print(
        "\nСледующим этапом здесь будет "
        "tasks.task.add и ссылка на созданную задачу."
    )

    # После создания задачи возврат назад запрещён,
    # чтобы не плодить дубли.
    session["current_screen"] = "done"
    session["screen_id"] += 1


def handle_instruction(session: dict[str, Any]) -> None:
    path = session["instruction_path"]

    print("\nИнструкция:")
    print(INSTRUCTIONS[path])

    session["current_screen"] = "done"
    session["screen_id"] += 1


def run_bot() -> None:
    # Пока один тестовый пользователь.
    # В Bitrix здесь будет ID автора сообщения.
    session = new_session(
        user_id=153
    )

    handlers = {
        "type": handle_type,
        "target": handle_target,
        "request": handle_request,
        "request_detail": handle_request_detail,
        "details": handle_details,
        "description": handle_description,
        "insurance_type": handle_insurance_type,
        "product": handle_product,
        "subproduct": handle_subproduct,
        "document": handle_document,
        "confirm": handle_confirm,
        "instruction": handle_instruction,
    }

    while session["current_screen"] != "done":
        screen = session["current_screen"]
        handler = handlers[screen]
        handler(session)

    print("\nСценарий завершён.")


if __name__ == "__main__":
    run_bot()
