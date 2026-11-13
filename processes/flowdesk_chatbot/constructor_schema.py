"""Editable, versioned graph for all DeskFlow conversations.

The seed imports the current catalogue; after that the database is authoritative.
Node/option IDs and answer bindings survive renaming and reordering.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from math import isfinite
import hashlib
import re
from typing import Any

from processes.flowdesk_chatbot.field_templates import (
    PROFILE_FIELDS, PROTECTED_FIELDS, SYSTEM_FIELDS, mapping_errors, template_errors, validated_link,
)

ACTIONS = {"choice", "link", "download", "back", "create_task", "save_draft", "finish", "cancel"}
KINDS = {"menu", "single", "multi", "text", "number", "date", "files", "info", "review", "end"}
OPS = {"eq", "ne", "contains", "not_contains", "in", "exists", "not_exists", "gt", "gte", "lt", "lte"}
MODES = {"buttons", "text", "both"}
STYLES = {"primary", "secondary", "alert", "base"}


def stable_id(value: str) -> str:
    return "n_" + hashlib.sha256(value.encode()).hexdigest()[:16]


def condition(field: str, value: Any, op: str = "eq") -> dict[str, Any]:
    return {"all": [{"field": field, "op": op, "value": value}]}


def seed_document() -> dict[str, Any]:
    from processes.flowdesk_chatbot import config, initiatives
    from processes.flowdesk_chatbot.engine import _start_text
    nodes: list[dict[str, Any]] = []

    def add(key, title, kind="text", bind="", next_id=None, section="Общие шаги", **extra):
        node = {
            "id": key, "label": title, "title": title, "hint": "", "kind": kind,
            "bind": bind, "next": next_id, "section": section, "enabled": True,
            "required": kind not in {"menu", "info", "end", "review", "files"},
            "input_mode": "buttons" if kind in {"menu", "single", "multi", "info", "review", "end"} else "both",
            "options": [], "show_if": {}, "limits": {"max_length": 10000},
            "allow_back": True, "allow_save": True, "allow_cancel": True,
        }
        node.update(extra)
        nodes.append(node)
        return node

    def option(text, next_id=None, **extra):
        result = {"id": stable_id("option:" + text + ":" + str(next_id) + ":" + str(extra.get("show_if", ""))), "text": text,
                  "value": text, "next": next_id, "style": "",
                  "display": "LINE", "enabled": True, "action": "choice"}
        result.update(extra)
        return result

    start = add("start", _start_text(), "menu", "request_type", section="Главное меню",
                label="Приветствие и главное меню", allow_back=False, allow_save=False, registry_field="UF_FLOWDESK_REQUEST_TYPE")
    description = add("description", "Расскажите, в чём нужна помощь", bind="description", next_id="insurance",
                      registry_field="UF_FLOWDESK_DESCRIPTION")
    add("insurance", "К какому виду страхования относится обращение?", "single", "insurance_type", "product",
        hint="Если вопрос не связан со страхованием, выберите «Не применимо».",
        registry_field="UF_FLOWDESK_INSURANCE_TYPE")
    product = add("product", "Выберите класс страхования", "single", "product", "subproduct",
                  registry_field="UF_FLOWDESK_INSURANCE_CLASS")
    subproduct = add("subproduct", "Выберите продукт", "single", "subproduct", "documents",
                     registry_field="UF_FLOWDESK_PRODUCT")
    insurance = next(n for n in nodes if n["id"] == "insurance")
    for group, products in config.PRODUCT_TREE.items():
        insurance["options"].append(option(group))
        for cls, products_list in products.items():
            product["options"].append(option(cls, show_if=condition("insurance_type", group)))
            for item in products_list:
                subproduct["options"].append(option(item, show_if={"all": [
                    {"field": "insurance_type", "op": "eq", "value": group},
                    {"field": "product", "op": "eq", "value": cls},
                ]}))
    insurance["options"].append(option("Не применимо", "documents", style="base"))
    add("documents", "Есть документ, фото или ссылка?", "files", "document", "review",
        hint="Прикрепите материалы. Если их нет, нажмите «Пропустить».",
        limits={"max_files": 20, "max_file_mb": 50, "extensions": [], "max_length": 10000})
    add("review", "Проверьте обращение перед отправкой", "review", next_id="done",
        label="Проверка и отправка", allow_save=True)
    add("done", "Обращение создано", "end", label="Успешная отправка",
        allow_save=False, allow_cancel=False, allow_back=False)

    def request_tree(tree, path=(), level=0):
        if "route" in tree:
            route = tree["route"]
            if route == "instruction" and path in config.INSTRUCTIONS:
                key = stable_id("instruction:" + "/".join(path))
                add(key, config.INSTRUCTIONS[path], "info", section="Инструкции",
                    label=" / ".join(path), allow_save=False)
                return key
            fields = tree.get("fields", []) if route == "details" else []
            dest = "description"
            for field in reversed(fields):
                key = stable_id("detail:" + "/".join(path) + ":" + field)
                spec = config.DETAIL_FIELDS[field]
                add(key, spec["prompt"], bind="details." + field, next_id=dest,
                    section="Запросы", label=" / ".join(path) + " · " + spec["label"],
                    limits={k: v for k, v in spec.items() if k in {"max_length", "validator"}})
                dest = key
            return dest
        key = stable_id("request:" + "/".join(path)) if path else "request_target"
        binds = ["target", "request", "request_detail"]
        bind = binds[min(level, 2)]
        node = add(key, tree.get("__prompt__") or ("С каким подразделением связан вопрос?" if not path else "Что требуется сделать?"),
                   "single", bind, section="Запросы", label=" / ".join(path) or "Подразделение",
                   registry_field={"target": "UF_FLOWDESK_TARGET", "request": "UF_FLOWDESK_REQUEST",
                                   "request_detail": "UF_FLOWDESK_REQUEST_DETAIL"}[bind])
        for text, child in tree.items():
            if text in {"route", "fields", "__prompt__"}:
                continue
            node["options"].append(option(text, request_tree(child, path + (text,), level + 1)))
        return key

    request_root = request_tree(config.REQUEST_TREE)

    # A profile snapshot is prefilled by the worker; employees can correct it in review.
    profile_keys = list(initiatives.PROFILE_LABELS)
    for index, field in enumerate(profile_keys):
        add("profile_" + field, "Укажите: " + initiatives.PROFILE_LABELS[field], bind="profile." + field,
            next_id="profile_" + profile_keys[index + 1] if index + 1 < len(profile_keys) else "initiative_profile_confirm",
            section="Инициативы · общие", required=field != "manager",
            show_if=condition("profile." + field, None, "not_exists"),
            registry_field="UF_FLOWDESK_INIT_" + field.upper())
    add("initiative_profile_confirm", "Проверьте данные инициатора", "info", next_id="initiative_scale",
        section="Инициативы · общие", hint="{{profile.name}}\n{{profile.department}}\n{{profile.manager}}\n{{profile.contact}}")
    meta_keys = list(initiatives.META)
    for index, field in enumerate(meta_keys):
        title, options = initiatives.META[field]
        node = add("initiative_" + field, title, "multi" if field == "audience" else "single", "initiative." + field,
                   "initiative_" + meta_keys[index + 1] if index + 1 < len(meta_keys) else "initiative_branch",
                   section="Инициативы · общие", registry_field="UF_FLOWDESK_INIT_" + field.upper())
        node["options"] = [option(text, other=text == initiatives.OTHER) for text in options]
    branch = add("initiative_branch", "Выбор анкеты", "info", next_id="small_title",
                 section="Инициативы · общие", hidden_router=True)
    branch["routes"] = [{"when": condition("initiative.scale", initiatives.LARGE), "next": "large_title"}]
    for prefix, questions in (("small", initiatives.SMALL_QUESTIONS), ("large", initiatives.LARGE_QUESTIONS)):
        for index, spec in enumerate(questions):
            dest = prefix + "_" + questions[index + 1]["key"] if index + 1 < len(questions) else "review"
            kind = "files" if spec["kind"] == "materials" else ("single" if spec["kind"] == "gate" else "text")
            key = prefix + "_" + spec["key"]
            binding = "initiative." + spec["key"]
            node = add(key, spec["title"], kind, binding, dest, hint=spec["hint"],
                       section="Инициативы · " + ("небольшая" if prefix == "small" else "крупная"),
                       label=initiatives.QUESTION_LABELS[spec["key"]],
                       registry_field="UF_FLOWDESK_INIT_" + spec["key"].upper() if kind != "files" else "",
                       required=kind != "files", limits={"max_length": 200 if spec["key"] == "title" else 10000,
                                                        "max_files": 20, "max_file_mb": 50, "extensions": []})
            if spec["kind"] == "gate":
                node["options"] = [option(text, key + "_explain" if pos == 0 else dest)
                                   for pos, text in enumerate(spec["options"])]
                add(key + "_explain", "Уточните ответ: " + spec["title"], bind=binding + "_explanation",
                    next_id=dest, section=node["section"], hint=spec["hint"], label=node["label"] + " · пояснение")
            elif spec["kind"] == "text_choice":
                node["options"] = [option(text) for text in spec["options"]]
    for text, route in config.REQUEST_TYPES.items():
        if route == "initiative":
            dest = "profile_" + profile_keys[0]
        elif route == "request_tree":
            dest = request_root
        elif route == "instruction":
            dest = stable_id("maininstruction:" + text)
            old = {"Калькулятор ОГПО ВТС": ("Калькулятор ОГПО ВТС",),
                   "Сообщить о сбое": ("Сообщить о сбое",)}.get(text, (text,))
            instruction_text = config.INSTRUCTIONS.get(old)
            if not instruction_text:
                raise ValueError("В текущем боте отсутствует инструкция: " + text)
            add(dest, instruction_text, "info", section="Инструкции", label=text, allow_save=False)
        else:
            dest = description["id"]
        start["options"].append(option(text, dest, style="alert" if text in {"Горит контракт", "Жалоба"} else "secondary"))
    return {"format": 1, "name": "FlowDesk", "start": "start", "nodes": nodes,
            "settings": {"idle_hours": 24, "max_drafts": 10, "show_history": True, "labels": {
                "back": "← Назад", "save": "Сохранить черновик", "cancel": "Отменить",
                "skip": "Пропустить", "continue": "Продолжить", "submit": "Создать обращение",
                "edit": "Исправить данные", "restart": "Новое обращение",
                "resume": "Продолжить", "review": "К проверке", "files_replace": "Заменить вложения",
                "files_clear": "Удалить вложения", "retry": "Повторить передачу",
                "check_task": "Проверить наличие задачи", "history_more": "Показать ещё",
                "launcher_open": "Открыть служебный чат", "launcher_ready": "Перейти в служебный чат",
            }, "styles": {"choice": "secondary", "back": "primary", "submit": "primary", "cancel": "alert"},
                "messages": {"saved": "Черновик сохранён. Можно продолжить позже.",
                             "cancelled": "Заполнение отменено.", "error": "Не удалось передать обращение. Ответы сохранены.",
                             "other_prompt": "Уточните свой вариант", "edit_prompt": "Что изменить?",
                             "confirmed": "Обращение подтверждено. Ответы сохранены.",
                             "launcher": "[b]DeskFlow готов к работе 🙂[/b]\nНажми кнопку ниже — я проверю служебный чат и подготовлю его к работе.",
                             "launcher_ready": "[b]Готово 🙂[/b]\nСлужебный чат проверен и готов к работе."}}}


def validate_document(doc: dict[str, Any]) -> list[str]:
    try:
        return _validate_document(doc)
    except (TypeError, ValueError, AttributeError, KeyError, OverflowError, RecursionError):
        return ["Неверный тип значения в сценарии"]


def conditional(value):
    return any(value.get(group) for group in ("all", "any"))


def _validate_document(doc):
    errors: list[str] = []
    if not isinstance(doc, dict) or doc.get("format") != 1 or not isinstance(doc.get("nodes"), list):
        return ["Неверная структура конструктора"]
    if len(doc["nodes"]) > 3000:
        return ["Слишком много шагов (максимум 3000)"]
    if not doc["nodes"] or not isinstance(doc.get("settings"), dict):
        return ["Укажите окна и общие настройки"]
    nodes = {n.get("id"): n for n in doc["nodes"] if isinstance(n, dict)}
    if len(nodes) != len(doc["nodes"]) or None in nodes or "" in nodes:
        errors.append("У каждого шага должен быть уникальный ID")
    if doc.get("start") not in nodes:
        errors.append("Не указан существующий стартовый шаг")
    bindings = {n.get("bind") for n in nodes.values() if n.get("bind")}
    bindings |= PROFILE_FIELDS | SYSTEM_FIELDS
    bindings |= {o["bind"] + ".other" for o in nodes.values() if o.get("bind") and any(
        button.get("other") for button in o.get("options", []))}

    def check_template(value, label):
        errors.extend(f"{label} — {error}" for error in template_errors(value, bindings))
    edges = {key: [] for key in nodes}

    def check_condition(value, label):
        if not isinstance(value, dict):
            errors.append(label + ": неверное условие"); return
        for group in ("all", "any"):
            for rule in value.get(group, []):
                if rule.get("field") not in bindings or rule.get("op") not in OPS:
                    errors.append(label + ": условие ссылается на отсутствующий ответ или неизвестную операцию")
                elif rule["op"] == "in" and not isinstance(rule.get("value"), list):
                    errors.append(label + ": условию «Один из» нужен список значений")

    for key, n in nodes.items():
        label = str(n.get("label") or key)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,100}", key):
            errors.append(label + ": недопустимый ID шага")
        if not all(isinstance(n.get(k, ""), str) for k in ("title", "label", "hint")):
            errors.append(label + ": текст и описание должны быть строками")
        if not (n.get("title", "").strip() or n.get("label", "").strip()):
            errors.append(label + ": укажите сообщение или название окна")
        for prop in ("title", "hint"):
            check_template(n.get(prop, ""), label)
        if any(type(n.get(key, False)) is not bool for key in ("enabled", "required", "allow_back", "allow_save", "allow_cancel", "hidden_router")):
            errors.append(label + ": неверный переключатель окна")
        if n.get("bind", "").startswith("system.") or n.get("registry_field") in PROTECTED_FIELDS:
            errors.append(label + ": системное поле заполняется автоматически")
        if (not n.get("enabled", True) or conditional(n.get("show_if", {}))) and not (n.get("skip_next") or n.get("next")):
            errors.append(label + ": отключённому шагу нужен следующий шаг")
        if n.get("kind") not in KINDS or n.get("input_mode") not in MODES:
            errors.append(label + ": неизвестный тип шага или режим ввода")
        if n.get("bind") and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,100}", n["bind"]):
            errors.append(label + ": недопустимое имя ответа")
        if n.get("kind") in {"single", "multi", "text", "number", "date", "files"} and not n.get("bind"):
            errors.append(label + ": укажите имя для сохранения ответа")
        if n.get("kind") in {"review", "end", "info"} and n.get("input_mode") != "buttons":
            errors.append(label + ": у служебного шага должен быть режим «Только кнопки»")
        if n.get("hidden_router") and not n.get("next"):
            errors.append(label + ": у развилки нужен переход «Иначе»")
        if n.get("kind") == "review" and not any(o.get("action") == "create_task" for o in n.get("options", [])):
            ending = nodes.get(n.get("next"), {})
            if ending.get("kind") != "end" or not ending.get("enabled", True) or conditional(ending.get("show_if", {})):
                errors.append(label + ": после подтверждения требуется доступное окно завершения")
        if n.get("registry_field") and not re.fullmatch(r"UF_[A-Z0-9_]{1,80}", n["registry_field"]):
            errors.append(label + ": поле результата должно иметь вид UF_...")
        check_condition(n.get("show_if", {}), label)
        limits = n.get("limits", {})
        for numeric in ("min_length", "max_length", "min_choices", "max_choices", "max_files"):
            if numeric in limits and (type(limits[numeric]) is not int or not 0 <= limits[numeric] <= 20000):
                errors.append(label + ": предел должен быть целым числом от 0 до 20 000")
        for numeric in ("min", "max", "max_file_mb"):
            if numeric in limits and (type(limits[numeric]) not in (int, float) or not isfinite(limits[numeric])):
                errors.append(label + ": неверный числовой предел")
        if limits.get("max_file_mb", 1) <= 0 or limits.get("max_files", 1) > 30:
            errors.append(label + ": допустимо до 30 файлов с положительным ограничением размера")
        extensions = limits.get("extensions", [])
        if not isinstance(extensions, list) or any(not re.fullmatch(r"\.?[A-Za-z0-9]{1,20}", ext) for ext in extensions):
            errors.append(label + ": неверное расширение файла")
        dates = [date.fromisoformat(limits[prop]) for prop in ("min_date", "max_date") if limits.get(prop)]
        if len(dates) == 2 and dates[0] > dates[1]:
            errors.append(label + ": минимальная дата позже максимальной")
        for lo, hi in (("min_length", "max_length"), ("min", "max"), ("min_choices", "max_choices")):
            if limits.get(lo) is not None and limits.get(hi) is not None:
                try:
                    if float(limits[lo]) > float(limits[hi]): errors.append(label + ": минимум больше максимума")
                except (ValueError, TypeError): errors.append(label + ": неверный числовой предел")
        if limits.get("pattern"):
            # Never execute administrator-supplied regular expressions on employee text.
            errors.append(label + ": произвольные регулярные выражения не поддерживаются; выберите готовую проверку")
        option_ids = set()
        values = set()
        if len(n.get("options", [])) > 300 or len(n.get("routes", [])) > 100:
            errors.append(label + ": допустимо до 300 кнопок и 100 развилок")
        for opt in n.get("options", []):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,101}", opt.get("id", "")) or opt["id"] in option_ids:
                errors.append(label + ": ID кнопок должны быть уникальны внутри шага")
            option_ids.add(opt.get("id"))
            check_template(opt.get("text"), label)
            if not opt["text"].strip() or len(opt["text"]) > 250:
                errors.append(label + ": кнопке нужна надпись до 250 символов")
            if type(opt.get("enabled", True)) is not bool or any(type(opt.get(flag, False)) is not bool for flag in ("other", "exclusive")):
                errors.append(label + ": неверный переключатель кнопки")
            check_condition(opt.get("show_if", {}), label + " / " + str(opt.get("text")))
            if opt.get("style", "") not in STYLES | {""}:
                errors.append(label + ": неизвестный стиль кнопки")
            if opt.get("display", "LINE") not in {"LINE", "BLOCK"}:
                errors.append(label + ": неизвестное расположение кнопки")
            if opt.get("width") is not None and (type(opt["width"]) is not int or not 20 <= opt["width"] <= 1000):
                errors.append(label + ": ширина кнопки должна быть от 20 до 1000 пикселей")
            if n.get("kind") == "multi" and opt.get("action", "choice") == "choice" and opt.get("next") and opt["next"] != n.get("next"):
                errors.append(label + ": для нескольких ответов используйте общий переход и условия следующего шага")
            for color_key in ("bg_color", "text_color", "off_bg_color", "off_text_color"):
                if opt.get(color_key) and not re.fullmatch(r"#[0-9A-Fa-f]{3}([0-9A-Fa-f]{3})?", opt[color_key]):
                    errors.append(label + ": неверный цвет кнопки")
            action = opt.get("action", "choice")
            if action not in ACTIONS:
                errors.append(label + ": неизвестное действие кнопки")
            if n["kind"] == "end" and action not in {"link", "download"}:
                errors.append(label + ": после создания задачи можно добавить ссылки и файлы")
            if action in {"link", "download"}:
                try:
                    validated_link(opt.get("link"), download=action == "download")
                except ValueError as exc:
                    errors.append(label + " — " + str(exc))
            if action == "choice":
                value = opt.get("value", opt["text"])
                if not isinstance(value, str) or not value.strip():
                    errors.append(label + ": укажите сохраняемый ответ")
                if n["kind"] == "multi" and value in values:
                    errors.append(label + ": ответы множественного выбора должны различаться")
                values.add(value)
                if not (opt.get("next") or n.get("next")):
                    errors.append(label + ": у кнопки нет перехода")
            if action == "create_task":
                ending = nodes.get(opt.get("next"), {})
                if n["kind"] == "end" or ending.get("kind") != "end" or not ending.get("enabled", True) or conditional(ending.get("show_if", {})):
                    errors.append(label + ": укажите доступное окно завершения для создания задачи")
            if opt.get("next") and action in {"choice", "create_task"}:
                edges[key].append(opt["next"])
        for route in n.get("routes", []):
            check_condition(route.get("when", {}), label)
            if route.get("next"):
                edges[key].append(route["next"])
            else:
                errors.append(label + ": у развилки нет окна назначения")
        if n.get("next"): edges[key].append(n["next"])
        if n.get("skip_next"): edges[key].append(n["skip_next"])
        for dest in edges[key]:
            if dest not in nodes: errors.append(label + ": переход в отсутствующий шаг " + str(dest))
        if n.get("kind") in {"menu", "single", "multi"} and n.get("input_mode") == "buttons":
            if not any(o.get("enabled", True) and o.get("action", "choice") in {"choice", "create_task", "finish", "cancel"} for o in n.get("options", [])):
                errors.append(label + ": нет доступных вариантов ответа")
        if n.get("kind") in {"text", "number", "date", "files", "multi"} and n.get("input_mode") != "buttons" and not n.get("next"):
            errors.append(label + ": укажите следующий шаг после ввода ответа")
        if n.get("kind") == "multi" and not n.get("next"):
            errors.append(label + ": укажите общий переход после выбора")
        if n.get("kind") not in {"end", "info", "review"} and not edges[key] and not any(
            o.get("enabled", True) and o.get("action") in {"finish", "cancel"} for o in n.get("options", [])
        ):
            errors.append(label + ": нет перехода после ответа")
        if n.get("kind") in {"text", "number", "date", "files"} and n.get("input_mode") == "buttons" and not n.get("options"):
            errors.append(label + ": текстовый шаг полностью отключает ввод")
    colors = {}
    for root in nodes:
        if colors.get(root) == 2: continue
        pending = [(root, False)]
        while pending:
            key, finish = pending.pop()
            if finish: colors[key] = 2; continue
            if colors.get(key) == 1:
                errors.append("Обнаружен цикл переходов. Для возврата используйте кнопку «Назад»"); continue
            if colors.get(key) == 2: continue
            colors[key] = 1; pending.append((key, True))
            pending.extend((dest, False) for dest in edges.get(key, []) if dest in nodes)
    reached = set(); pending = [doc.get("start")]
    while pending:
        key = pending.pop()
        if key in reached: continue
        reached.add(key); pending.extend(edges.get(key, []))
    for key, n in nodes.items():
        if key not in reached and n.get("enabled", True):
            errors.append(str(n.get("label") or key) + ": шаг недоступен из главного меню")
    settings = doc.get("settings", {})
    for key in ("idle_hours", "max_drafts"):
        if type(settings.get(key, 1)) is not int or not 1 <= settings.get(key, 1) <= (100 if key == "max_drafts" else 8760):
            errors.append("Общие настройки: " + key + " должно быть положительным числом")
    for key, value in settings.get("output", {}).items():
        if key not in {"project_id", "responsible_id"} or not isinstance(value, int) or value <= 0:
            errors.append("Параметры результата должны содержать положительные ID проекта и ответственного")
    for value in settings.get("styles", {}).values():
        if value not in STYLES: errors.append("Неизвестный общий стиль кнопки")
    errors.extend(mapping_errors(settings.get("mapping", {}), bindings - {"system.task_id"}))
    for prop in ("labels", "messages"):
        for key, value in settings.get(prop, {}).items():
            check_template(value, key)
    for key, point in doc.get("layout", {}).items():
        if key not in nodes or any(type(point.get(axis)) not in (int, float) or not 0 <= point[axis] <= 1_000_000 for axis in ("x", "y")):
            errors.append("Неверные координаты окна")
    return list(dict.fromkeys(errors))


def insert_before(doc, target, new_node):
    result = deepcopy(doc)
    for node in result["nodes"]:
        if node.get("next") == target: node["next"] = new_node["id"]
        if node.get("skip_next") == target: node["skip_next"] = new_node["id"]
        for item in node.get("options", []) + node.get("routes", []):
            if item.get("next") == target: item["next"] = new_node["id"]
    if result["start"] == target: result["start"] = new_node["id"]
    new_node = deepcopy(new_node); new_node["next"] = target
    position = next(i for i, n in enumerate(result["nodes"]) if n["id"] == target)
    result["nodes"].insert(position, new_node)
    return result


def remove_node(doc, key, replacement):
    if key == replacement: raise ValueError("Нельзя заменить шаг им же")
    result = deepcopy(doc)
    if replacement not in {n["id"] for n in result["nodes"] if n["id"] != key}:
        raise ValueError("Выберите существующий шаг для перехода")
    result["nodes"] = [n for n in result["nodes"] if n["id"] != key]
    result.get("layout", {}).pop(key, None)
    if result["start"] == key: result["start"] = replacement
    for n in result["nodes"]:
        if n.get("next") == key: n["next"] = replacement
        if n.get("skip_next") == key: n["skip_next"] = replacement
        for item in n.get("options", []) + n.get("routes", []):
            if item.get("next") == key: item["next"] = replacement
    return result


def move_before(doc, key, target):
    """Move a linear question; reject branch merges instead of silently changing routes."""
    result = deepcopy(doc); nodes = {n["id"]: n for n in result["nodes"]}
    node = nodes[key]
    outgoing = {x.get("next") or node.get("next") for x in node.get("options", [])}
    outgoing.add(node.get("next")); outgoing.discard(None)
    if len(outgoing) != 1 or node.get("routes") or node.get("kind") in {"review", "end", "info"}:
        raise ValueError("Переставлять можно линейный вопрос; для развилки измените переходы кнопок")
    successor = next(iter(outgoing))
    if target == key or successor == key: raise ValueError("Неверный порядок")
    result = remove_node(result, key, successor)
    node["next"] = target
    for opt in node.get("options", []):
        if opt.get("next"): opt["next"] = target
    return insert_before(result, target, node)
