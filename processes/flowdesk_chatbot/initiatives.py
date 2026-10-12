"""Initiative questionnaires from Опросник_инициативы(1).docx.

Only cursors are moved by navigation. Answers, including inactive answers from
the other scale, remain in the session and in the submitted task.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

SMALL = "Небольшая инициатива"
LARGE = "Крупная инициатива"
UNKNOWN = "Пока не знаю"
OTHER = "Другое — укажите"
SAVE = "__initiative_save__"
RESUME = "__initiative_resume__"
START_NEW = "__initiative_new__"
CONTINUE = "__initiative_continue__"
PROFILE_OK = "__initiative_profile_ok__"
PROFILE_EDIT = "__initiative_profile_edit__"
EDIT = "__initiative_edit__"
REVIEW = "__initiative_review__"
BACK = "__back__"
CONFIRM = "__confirm__"
NEW_REQUEST = "__new_request__"
PREFIX = "__initiative_value__:"
EDIT_PREFIX = "__initiative_edit_field__:"
PROFILE_PREFIX = "__initiative_profile_field__:"
ADD = "__initiative_files_add__"
REPLACE = "__initiative_files_replace__"
DELETE = "__initiative_files_delete__"

PROFILE_LABELS = {
    "name": "ФИО", "department": "Подразделение",
    "contact": "Рабочий контакт", "manager": "Руководитель",
}
META = {
    "scale": ("Какой масштаб изменения вы предполагаете?", [SMALL, LARGE, UNKNOWN]),
    "action": ("Что нужно сделать?", ["Создать новое", "Изменить существующее", "Заменить существующее", "Прекратить", OTHER]),
    "subject": ("С чем связана инициатива?", ["Процесс", "Продукт или услуга", "Система", "Отчёт или данные", "Документ или регламент", "Организация работы", OTHER]),
    "audience": ("Кого затронет инициатива?", ["Клиенты", "Сотрудники", "Партнёры", OTHER]),
    "scope": ("Где требуется изменение?", ["Во всей компании", "В нескольких подразделениях", "В одном подразделении", "В филиале или нескольких филиалах", "В отдельной роли или группе сотрудников", "В конкретной системе или канале", OTHER]),
}
META_LABELS = {
    "scale": "Масштаб", "action": "Что нужно сделать",
    "subject": "С чем связана инициатива", "audience": "Кого затронет",
    "scope": "Где требуется изменение",
}

def question(key: str, title: str, hint: str, kind: str = "text", options: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"key": key, "title": title, "hint": hint, "kind": kind, "options": options}

SMALL_QUESTIONS = (
    question("title", "Как называется инициатива?", "Дайте короткое название, из которого понятно, что предлагается улучшить."),
    question("reason", "Что сейчас мешает работе или какую возможность хотите использовать?", "Опишите текущую ситуацию и приведите один реальный пример. Можно указать, что стало поводом для предложения."),
    question("change", "Что предлагаете изменить?", "Опишите, как должна измениться работа. Если конкретный способ ещё не выбран, достаточно желаемого изменения."),
    question("affected", "Кого и что затронет изменение?", "Укажите известные подразделения, роли, клиентов, участок процесса, документы или системы, которых касается инициатива. Неизвестные участки можно отметить как требующие уточнения."),
    question("effect", "Какой результат ожидаете и как проверим, что он получен?", "Укажите пользу и 1–3 понятных признака результата: меньше времени, ошибок или ручных действий, больше продаж, лучше обслуживание. Цифры добавьте, если они есть. Денежный расчёт не требуется."),
    question("requirements", "Что важно учесть перед изменением?", "Есть ли обязательные условия, ограничения, зависимости или возможные отрицательные последствия? Укажите только известное.", "gate", ("Есть", "Нет известных условий", UNKNOWN)),
    question("deadline", "К какому сроку нужен результат и почему?", "Если срок важен, укажите дату или период и основание: обязательство, запуск, сезонность, риск или зависимость.", "gate", ("Есть срок", "Нет жёсткого срока")),
    question("materials", "Какие материалы подтверждают или поясняют инициативу?", "Приложите примеры, скриншоты, статистику, документы или ссылки. Здесь же укажите связанные обращения, задачи или инициативы, если они есть.", "materials"),
)
LARGE_QUESTIONS = (
    question("title", "Как называется инициатива?", "Укажите короткое название, которое отражает суть изменения."),
    question("reason", "Почему нужна инициатива и что будет, если ничего не менять?", "Опишите проблему или возможность, повод для предложения и последствия бездействия. Приведите факты или пример, если они есть. Неизвестные последствия можно отметить отдельно."),
    question("current", "Как устроена работа сейчас?", "Кто участвует, какие основные действия выполняет, где возникают ожидания, ошибки или ручная работа? Для нового продукта или процесса укажите, как потребность закрывается сейчас либо что пока отсутствует. Схему или пример можно приложить в конце."),
    question("change", "Как должна быть устроена работа после изменения?", "Опишите желаемый порядок работы и новые возможности. Если уже видны варианты решения, укажите их и предпочтительный вариант; если нет, достаточно целевого состояния."),
    question("bounds", "Что входит в инициативу и что остаётся за её пределами?", "Перечислите основные результаты и изменения. Отдельно укажите связанные темы, которые исключены или отложены. Используйте пункты «Входит» и «Не входит». Неясные границы отметьте как требующие уточнения."),
    question("affected", "Кого и что затронет инициатива?", "Укажите подразделения, роли, клиентов или партнёров, процессы, документы и системы. Для продукта или канала продаж добавьте целевую аудиторию и канал. Перечисляйте известное."),
    question("effect", "Какой эффект ожидается?", "Укажите пользу: продажи или доход, экономия времени, качество, клиентский опыт, контроль, снижение риска либо новые возможности. Если есть данные, добавьте текущий показатель, целевой результат или прогноз. Показатели и финансовый прогноз — при наличии."),
    question("success", "По каким признакам поймём, что инициатива реализована успешно?", "Укажите 2–5 проверяемых признаков результата. Например, нужный процесс работает, сотрудник выполняет действие без повторного ввода, клиент получает предусмотренный результат. Количественные значения — если известны."),
    question("owner", "Кто отвечает за результат инициативы и кто участвует в его проверке?", "Если известно, укажите владельца результата, заинтересованные подразделения и тех, кто сможет проверить изменение в работе. Достаточно имени или роли.", "text_choice", ("Нужно определить при рассмотрении",)),
    question("requirements", "Какие обязательные требования и ограничения нужно соблюдать?", "Укажите известные правила, договорные или нормативные требования, условия продукта или партнёра, необходимые согласования, ограничения бюджета и то, что нельзя менять.", "gate", ("Есть", "Нет известных требований", "Нужно уточнить")),
    question("resources", "От чего зависит реализация и какие ресурсы могут понадобиться?", "Укажите зависимость от людей, данных, решений, партнёров или других инициатив. Назовите известную потребность в участии сотрудников, обучении, закупках или расходах. Отметьте открытые вопросы. Суммы и трудозатраты — при наличии.", "text_choice", ("Нужно оценить при рассмотрении",)),
    question("risks", "Какие негативные последствия или риски возможны после изменения?", "Например, дополнительная нагрузка, новые расходы, сложности для клиента, необходимость переобучения или риск нарушения работы при переходе.", "gate", ("Есть", "Не выявлены", "Нужно оценить")),
    question("deadline", "К какому сроку нужен результат и почему?", "Укажите желаемую дату или период. Если срок обязательный, назовите основание и последствия задержки.", "gate", ("Есть срок", "Нет жёсткого срока")),
    question("materials", "Какие материалы подтверждают или поясняют инициативу?", "Приложите факты, статистику, схемы, действующие или предлагаемые документы, требования партнёра, известные согласования и ссылки. Добавьте связанные обращения, задачи или инициативы.", "materials"),
)
QUESTION_LABELS = {
    "title": "Название инициативы", "reason": "Проблема или возможность",
    "current": "Работа сейчас", "change": "Предлагаемое изменение",
    "bounds": "Границы инициативы", "affected": "Затронутые участники и объекты",
    "effect": "Ожидаемый эффект", "success": "Признаки успеха",
    "owner": "Владелец результата и проверка", "requirements": "Требования и условия",
    "resources": "Зависимости и ресурсы", "risks": "Риски",
    "deadline": "Срок и обоснование", "materials": "Материалы",
}
TASK_FIELDS = {
    **{f"UF_FLOWDESK_INIT_{key.upper()}": {"label": label, "sort": 310 + index * 10, "rows": 3}
       for index, (key, label) in enumerate(META_LABELS.items())},
    **{f"UF_FLOWDESK_INIT_{key.upper()}": {"label": label, "sort": 370 + index * 10, "rows": 8}
       for index, (key, label) in enumerate(QUESTION_LABELS.items()) if key != "materials"},
    **{f"UF_FLOWDESK_INIT_{key.upper()}": {"label": f"Инициатор: {label}", "sort": 520 + index * 10, "rows": 2}
       for index, (key, label) in enumerate(PROFILE_LABELS.items())},
    "UF_FLOWDESK_INIT_OPEN": {"label": "Сведения для уточнения", "sort": 570, "rows": 8},
}

def is_initiative(session: dict[str, Any]) -> bool:
    data = session.get("data")
    return isinstance(data, dict) and data.get("request_type") == "Предложение / Идея" and isinstance(data.get("initiative"), dict)

def state(session: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(session["data"].get("initiative"), dict):
        session["data"]["initiative"] = {}
    ini = session["data"]["initiative"]
    for key, default in {
        "version": 1, "profile": {}, "profile_loaded": False, "draft_checked": False,
        "profile_confirmed": False, "scale": None, "action": None, "subject": None,
        "audience": [], "scope": None, "other": {}, "answers": {}, "nav": [],
        "step": "scale", "materials_answered": False, "editing": False,
        "attachment_mode": "add", "profile_return": "flow",
    }.items():
        if key not in ini:
            ini[key] = deepcopy(default)
    for key in ("profile", "other", "answers"):
        if not isinstance(ini.get(key), dict):
            ini[key] = {}
    for key in ("nav", "audience"):
        if not isinstance(ini.get(key), list):
            ini[key] = []
    return ini

def begin(session: dict[str, Any]) -> None:
    if not is_initiative(session):
        session["data"]["initiative"] = {}
        if session["data"].get("description"):
            state(session)["answers"]["change"] = {"text": session["data"]["description"]}
    ini = state(session)
    ini["nav"] = [{"screen": "type", "step": "scale", "editing": False, "attachment_mode": "add", "profile_return": "flow"}]
    session["current_screen"] = "initiative_profile"
    session["edit_mode"] = False
    session["edit_root"] = None
    session["edit_backup"] = None
    session["edit_history"] = []
    session["route"] = "initiative"

def questions(session: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return LARGE_QUESTIONS if state(session)["scale"] == LARGE else SMALL_QUESTIONS

def specs(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in questions(session)}

def steps(session: dict[str, Any]) -> list[str]:
    return list(META) + [item["key"] for item in questions(session)]

def _bump(session: dict[str, Any]) -> None:
    session["revision"] = int(session.get("revision") or 0) + 1

def _cursor(session: dict[str, Any]) -> dict[str, Any]:
    ini = state(session)
    return {"screen": session["current_screen"], **{key: ini.get(key) for key in ("step", "profile_field", "profile_return", "other_field", "editing", "attachment_mode")}}

def _move(session: dict[str, Any], screen: str, step: str | None = None) -> None:
    ini = state(session)
    ini["nav"].append(_cursor(session))
    session["current_screen"] = screen
    if step is not None:
        ini["step"] = step

def _back(session: dict[str, Any]) -> bool:
    ini = state(session)
    if not ini["nav"]:
        return False
    cursor = ini["nav"].pop()
    session["current_screen"] = cursor.pop("screen")
    ini.update(cursor)
    return True

def profile_missing(session: dict[str, Any]) -> list[str]:
    profile = state(session)["profile"]
    return [key for key in PROFILE_LABELS if not str(profile.get(key) or "").strip()]

def answer_text(answer: dict[str, Any] | None) -> str:
    if not isinstance(answer, dict):
        return ""
    choice = str(answer.get("choice") or "").strip()
    text = str(answer.get("text") or "").strip()
    return "\n".join(value for value in (choice, text if not choice or choice in {"Есть", "Есть срок"} else "") if value)

def complete(session: dict[str, Any], key: str) -> bool:
    ini = state(session)
    if key in META:
        value = ini.get(key)
        if not value:
            return False
        if key == "audience":
            return OTHER not in value or bool(ini["other"].get(key))
        return value != OTHER or bool(ini["other"].get(key))
    if key == "materials":
        return bool(ini["materials_answered"])
    answer = ini["answers"].get(key) or {}
    spec = specs(session)[key]
    if spec["kind"] == "gate":
        choice = answer.get("choice")
        return bool(choice) and (choice not in {"Есть", "Есть срок"} or bool(answer.get("text")))
    return bool(answer.get("text") or answer.get("choice"))

def missing(session: dict[str, Any]) -> list[str]:
    return [key for key in steps(session) if not complete(session, key)]

def _advance(session: dict[str, Any]) -> None:
    remaining = missing(session)
    ini = state(session)
    if remaining:
        _move(session, "initiative_step", remaining[0])
    else:
        ini["editing"] = False
        _move(session, "initiative_review")

def _after_answer(session: dict[str, Any]) -> None:
    if state(session)["editing"]:
        state(session)["editing"] = False
        _move(session, "initiative_review")
    else:
        _advance(session)

def meta_value(session: dict[str, Any], key: str) -> str:
    ini = state(session)
    value = ini.get(key)
    values = value if isinstance(value, list) else [value]
    return "; ".join((f"Другое: {ini['other'].get(key, '')}" if item == OTHER else str(item)) for item in values if item)

def open_items(session: dict[str, Any]) -> list[str]:
    ini = state(session)
    result = ["Масштаб инициативы"] if ini["scale"] == UNKNOWN else []
    uncertain = {UNKNOWN, "Нужно уточнить", "Нужно оценить", "Нужно определить при рассмотрении", "Нужно оценить при рассмотрении"}
    uncertainty = re.compile(r"\b(?:неизвест\w*|не\s+зна\w*|нужно\s+(?:уточнить|определить|оценить|проработать)|требу\w*\s+уточнения)\b", re.IGNORECASE)
    for key, answer in ini["answers"].items():
        if answer.get("choice") in uncertain or uncertainty.search(answer_text(answer)):
            result.append(QUESTION_LABELS.get(key, key))
    for key, value in ini["other"].items():
        selected = OTHER in ini["audience"] if key == "audience" else ini.get(key) == OTHER
        if selected and uncertainty.search(str(value)):
            result.append(META_LABELS[key])
    for key, value in ini["profile"].items():
        if key in PROFILE_LABELS and uncertainty.search(str(value)):
            result.append(f"Данные инициатора — {PROFILE_LABELS[key]}")
    return list(dict.fromkeys(result))

def summary(session: dict[str, Any], markup: bool = True) -> str:
    ini = state(session)
    lines = ["Проверьте инициативу перед отправкой. Если всё верно, отправьте её на первичное рассмотрение.", ""] if markup else ["Инициатива на первичное рассмотрение", ""]
    def add(label: str, value: Any) -> None:
        if value not in (None, ""):
            lines.append(f"[b]{label}:[/b] {value}" if markup else f"{label}: {value}")
    for key, label in PROFILE_LABELS.items():
        add(f"Инициатор — {label}", ini["profile"].get(key))
    for key, label in META_LABELS.items():
        add(label, meta_value(session, key))
    lines.append("")
    active = [item["key"] for item in questions(session) if item["key"] != "materials"]
    for key in active:
        add(QUESTION_LABELS[key], answer_text(ini["answers"].get(key)))
    extra = [key for key in ini["answers"] if key not in active]
    if extra:
        lines.extend(["", "Дополнительные сведения, сохранённые при смене масштаба:"])
        for key in extra:
            add(QUESTION_LABELS.get(key, key), answer_text(ini["answers"][key]))
    data = session["data"]
    if data.get("document"):
        add("Материалы и связанные обращения", data["document"])
    if data.get("document_file_ids"):
        names = ", ".join(str(name) for name in data.get("document_file_names") or [] if name)
        add("Вложения", names or f"Файлов: {len(data['document_file_ids'])}")
    if data.get("document_comment"):
        add("Комментарий к материалам", data["document_comment"])
    if not data.get("document") and not data.get("document_file_ids"):
        add("Материалы", "Материалов пока нет")
    add("Сведения для уточнения", "; ".join(open_items(session)) or "Не отмечены")
    return "\n".join(lines)

def registry_fields(session: dict[str, Any]) -> dict[str, str]:
    ini = state(session)
    result = {f"UF_FLOWDESK_INIT_{key.upper()}": meta_value(session, key) for key in META}
    result.update({f"UF_FLOWDESK_INIT_{key.upper()}": value for key, answer in ini["answers"].items() if key in QUESTION_LABELS and (value := answer_text(answer))})
    result.update({f"UF_FLOWDESK_INIT_{key.upper()}": str(value) for key, value in ini["profile"].items() if key in PROFILE_LABELS and value})
    result["UF_FLOWDESK_INIT_OPEN"] = "; ".join(open_items(session))
    return {key: value for key, value in result.items() if value}

def _button(label: str, action: str, style: str = "secondary") -> dict[str, str]:
    return {"label": label, "action": action, "style": style}

def view(session: dict[str, Any]) -> dict[str, Any]:
    ini = state(session)
    screen = session["current_screen"]
    step = ini["step"]
    accepts_text = False
    accepts_files = False
    buttons: list[dict[str, str]] = []
    if screen == "initiative_profile":
        text = "[b]Проверьте данные инициатора.[/b]\n\n" + "\n".join(f"[b]{label}:[/b] {ini['profile'].get(key) or 'Не заполнено'}" for key, label in PROFILE_LABELS.items())
        if profile_missing(session):
            buttons.append(_button("Заполнить недостающие данные", PROFILE_OK, "primary"))
        else:
            buttons.append(_button("Данные верны", PROFILE_OK, "primary"))
        buttons.append(_button("Исправить данные", PROFILE_EDIT))
    elif screen == "initiative_profile_edit":
        text = "[b]Какие данные инициатора нужно исправить?[/b]"
        buttons = [_button(label, PROFILE_PREFIX + key) for key, label in PROFILE_LABELS.items()]
        buttons.append(_button("Готово", PROFILE_OK, "primary"))
    elif screen == "initiative_profile_field":
        key = ini["profile_field"]
        text = f"[b]Укажите: {PROFILE_LABELS[key]}.[/b]"
        if ini["profile"].get(key):
            text += f"\nТекущее значение: {ini['profile'][key]}"
        accepts_text = True
    elif screen == "initiative_other":
        text = f"[b]Укажите свой вариант: {META_LABELS[ini['other_field']]}.[/b]"
        accepts_text = True
    elif screen == "initiative_step" and step in META:
        title, options = META[step]
        text = f"[b]{title}[/b]"
        if step == "audience":
            text += "\nМожно выбрать несколько вариантов. После выбора нажмите «Продолжить»."
        if step == "scale":
            text += "\n[i]Если пока не знаете масштаб, начнём с короткой анкеты. Масштаб уточнят при рассмотрении.[/i]"
        for index, option in enumerate(options):
            selected = option in ini["audience"] if step == "audience" else ini.get(step) == option
            buttons.append(_button(("✓ " if selected else "") + option, PREFIX + str(index)))
        if step == "audience" and ini["audience"]:
            buttons.append(_button("Продолжить", CONTINUE, "primary"))
    elif screen in {"initiative_step", "initiative_detail"}:
        spec = specs(session)[step]
        number = next(index for index, item in enumerate(questions(session), 1) if item["key"] == step)
        text = f"[i]Вопрос {number} из {len(questions(session))}[/i]\n[b]{spec['title']}[/b]\n\n{spec['hint']}"
        if screen == "initiative_detail":
            text += "\n\n[b]Укажите дату или период и обоснование.[/b]" if step == "deadline" else "\n\n[b]Опишите известные условия.[/b]" if step == "requirements" else "\n\n[b]Опишите риск. Если известен способ его снижения, добавьте его.[/b]"
            accepts_text = True
        elif spec["kind"] == "materials":
            accepts_text = accepts_files = True
            text += "\n\n[i]Прикрепите файлы и/или отправьте ссылки одним сообщением.[/i]"
            has_materials = bool(session["data"].get("document") or session["data"].get("document_file_ids"))
            buttons.append(_button("Продолжить" if has_materials else "Материалов пока нет", CONTINUE, "primary" if has_materials else "secondary"))
        else:
            accepts_text = spec["kind"] in {"text", "text_choice"}
            buttons = [_button(option, PREFIX + str(index)) for index, option in enumerate(spec["options"])]
        saved = answer_text(ini["answers"].get(step))
        if saved:
            text += f"\n\n[i]Сохранённый ответ:[/i]\n{saved}"
    elif screen == "initiative_review":
        text = summary(session)
        buttons = [_button("Отправить", CONFIRM, "primary"), _button("Исправить ответ", EDIT)]
    elif screen == "initiative_edit":
        text = "[b]Какой ответ нужно исправить?[/b]"
        buttons = [_button("Данные инициатора", EDIT_PREFIX + "profile")]
        buttons += [_button(label, EDIT_PREFIX + key) for key, label in META_LABELS.items()]
        buttons += [_button(QUESTION_LABELS[item["key"]], EDIT_PREFIX + item["key"]) for item in questions(session)]
        buttons.append(_button("Вернуться к проверке", REVIEW, "primary"))
    elif screen == "initiative_attachments":
        text = "[b]Что сделать с материалами?[/b]"
        buttons = [_button("Добавить материалы", ADD), _button("Заменить материалы", REPLACE), _button("Удалить материалы", DELETE, "alert"), _button("Вернуться к проверке", REVIEW, "primary")]
    elif screen == "initiative_saved":
        text = "[b]Черновик инициативы сохранён.[/b]\nМожно продолжить сейчас или вернуться к нему позднее через «Предложение / Идея»."
        buttons = [_button("Продолжить заполнение", RESUME, "primary"), _button("В главное меню", NEW_REQUEST)]
    elif screen == "initiative_existing_draft":
        text = "[b]У вас есть сохранённый черновик инициативы.[/b]"
        buttons = [_button("Продолжить черновик", RESUME, "primary"), _button("Создать новую инициативу", START_NEW)]
    else:
        raise ValueError(f"Неизвестный экран инициативы: {screen}")
    if ini["nav"] and screen not in {"initiative_saved", "initiative_existing_draft", "initiative_review"}:
        buttons.append(_button("← Назад", BACK, "primary"))
    if screen not in {"initiative_saved", "initiative_existing_draft"}:
        buttons.append(_button("Сохранить черновик", SAVE))
    return {"screen": screen, "text": text, "buttons": buttons, "accepts_text": accepts_text, "accepts_files": accepts_files, "terminal": False, "revision": int(session["revision"])}

def submit_action(session: dict[str, Any], action: str) -> dict[str, Any]:
    ini = state(session)
    screen = session["current_screen"]
    if action not in {item["action"] for item in view(session)["buttons"]}:
        raise ValueError("Кнопка не относится к текущему экрану инициативы")
    status = "ok"
    if action == BACK:
        _back(session)
    elif action == SAVE:
        ini["resume_cursor"] = _cursor(session)
        session["current_screen"] = "initiative_saved"
        status = "draft_saved"
    elif action == RESUME:
        if screen == "initiative_existing_draft":
            return {"status": "draft_resume_requested"}
        cursor = ini.get("resume_cursor") or {"screen": "initiative_profile"}
        session["current_screen"] = cursor["screen"]
        ini.update({key: value for key, value in cursor.items() if key != "screen"})
    elif action == NEW_REQUEST:
        return {"status": "restart_requested"}
    elif action == START_NEW:
        _move(session, "initiative_profile")
    elif action == PROFILE_EDIT:
        ini["profile_confirmed"] = False
        _move(session, "initiative_profile_edit")
    elif action.startswith(PROFILE_PREFIX):
        ini["profile_confirmed"] = False
        ini["profile_field"] = action[len(PROFILE_PREFIX):]
        _move(session, "initiative_profile_field")
    elif action == PROFILE_OK:
        absent = profile_missing(session)
        if absent:
            ini["profile_field"] = absent[0]
            _move(session, "initiative_profile_field")
        elif screen == "initiative_profile_edit":
            _move(session, "initiative_profile")
        else:
            ini["profile_confirmed"] = True
            if ini["profile_return"] == "review":
                ini["profile_return"] = "flow"
                _move(session, "initiative_review")
            else:
                _advance(session)
    elif action == EDIT:
        _move(session, "initiative_edit")
    elif action == REVIEW:
        ini["editing"] = False
        _move(session, "initiative_review")
    elif action.startswith(EDIT_PREFIX):
        key = action[len(EDIT_PREFIX):]
        if key == "profile":
            ini["profile_return"] = "review"
            _move(session, "initiative_profile")
        elif key == "materials":
            _move(session, "initiative_attachments")
        else:
            ini["editing"] = key != "scale"
            _move(session, "initiative_step", key)
    elif action in {ADD, REPLACE}:
        ini["attachment_mode"] = "replace" if action == REPLACE else "add"
        ini["editing"] = True
        _move(session, "initiative_step", "materials")
    elif action == DELETE:
        for key, value in {"document": None, "document_file_ids": [], "document_file_names": [], "document_comment": None}.items():
            session["data"][key] = value
        ini["materials_answered"] = True
        _move(session, "initiative_review")
    elif action == CONFIRM:
        if profile_missing(session) or not ini["profile_confirmed"]:
            _move(session, "initiative_profile")
        elif missing(session):
            _advance(session)
        else:
            return {"status": "task_ready"}
    elif action == CONTINUE:
        step = ini["step"]
        if step == "audience" and OTHER in ini[step] and not ini["other"].get(step):
            ini["other_field"] = step
            _move(session, "initiative_other")
        elif step == "materials":
            ini["materials_answered"] = True
            _after_answer(session)
        else:
            _after_answer(session)
    elif action.startswith(PREFIX):
        index = int(action[len(PREFIX):])
        step = ini["step"]
        options = META[step][1] if step in META else specs(session)[step]["options"]
        value = options[index]
        if step == "audience":
            selected = ini[step]
            selected.remove(value) if value in selected else selected.append(value)
        elif step in META:
            ini[step] = value
            if value == OTHER:
                ini["other_field"] = step
                _move(session, "initiative_other")
            else:
                _after_answer(session)
        else:
            answer = ini["answers"].setdefault(step, {})
            answer["choice"] = value
            if value in {"Есть", "Есть срок"}:
                _move(session, "initiative_detail")
            else:
                _after_answer(session)
    _bump(session)
    return {"status": status}

def submit_text(session: dict[str, Any], text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    current = view(session)
    if not current["accepts_text"]:
        return {"status": "buttons_expected"}
    if not value:
        return {"status": "empty"}
    ini = state(session)
    screen = session["current_screen"]
    step = ini["step"]
    if screen == "initiative_profile_field":
        ini["profile"][ini["profile_field"]] = value
        absent = profile_missing(session)
        if absent:
            _move(session, "initiative_profile_field")
            ini["profile_field"] = absent[0]
        else:
            _move(session, "initiative_profile")
    elif screen == "initiative_other":
        key = ini["other_field"]
        ini["other"][key] = value
        _after_answer(session)
    elif step == "materials":
        return submit_materials(session, [], [], value)
    else:
        if step == "title" and len(value) > 200:
            return {"status": "validation_error", "message": "[b]Сократите только название инициативы до 200 символов.[/b]"}
        answer = ini["answers"].setdefault(step, {})
        answer["text"] = value
        if screen != "initiative_detail":
            answer["choice"] = ""
        _after_answer(session)
    _bump(session)
    return {"status": "ok"}

def submit_materials(session: dict[str, Any], file_ids: list[int], file_names: list[str], text: str = "") -> dict[str, Any]:
    if not view(session)["accepts_files"]:
        return {"status": "buttons_expected"}
    value = str(text or "").strip()
    ids = list(dict.fromkeys(int(item) for item in file_ids if str(item).isdigit() and int(item) > 0))
    if not ids and not value:
        return {"status": "empty"}
    ini = state(session)
    data = session["data"]
    if ini["attachment_mode"] == "replace":
        data.update(document=None, document_file_ids=[], document_file_names=[], document_comment=None)
    data["document_file_ids"] = list(dict.fromkeys((data.get("document_file_ids") or []) + ids))
    data["document_file_names"] = list(dict.fromkeys((data.get("document_file_names") or []) + [str(name) for name in file_names if name]))
    if value:
        key = "document_comment" if ids else "document"
        data[key] = "\n".join(item for item in (str(data.get(key) or "").strip(), value) if item)
    ini["materials_answered"] = True
    ini["attachment_mode"] = "add"
    _after_answer(session)
    _bump(session)
    return {"status": "ok"}
