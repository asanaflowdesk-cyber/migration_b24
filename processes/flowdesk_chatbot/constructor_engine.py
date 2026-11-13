"""Pure state machine. Preview and live bot execute the same published graph."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from processes.flowdesk_chatbot import initiatives
from processes.flowdesk_chatbot.field_templates import answer_text, as_text, neutral_markup, render_template, task_values


def is_custom(session):
    return isinstance(session.get("constructor"), dict)


def start(base, document, version, profile=None):
    session = deepcopy(base)
    session["constructor"] = {"document": deepcopy(document), "version": version, "node": document["start"],
                              "answers": {}, "trail": [], "selected": [], "other": None,
                              "status": "in_progress", "profile": profile or {}}
    for key, value in (profile or {}).items():
        if value: session["constructor"]["answers"]["profile." + key] = value
    session["constructor"]["answers"].update({"system.user_id": str(session["user_id"]),
                                                "system.request_id": session["request_id"]})
    session["current_screen"] = "constructor"
    settle(session)
    return session


def state(session):
    return session["constructor"]


def node(session):
    s = state(session)
    return next(n for n in s["document"]["nodes"] if n["id"] == s["node"])


def matches(condition, answers):
    def check(rule):
        value = answers.get(rule["field"])
        expected = rule.get("value")
        op = rule["op"]
        if op == "exists": return value not in (None, "", [])
        if op == "not_exists": return value in (None, "", [])
        if op == "eq": return value == expected
        if op == "ne": return value != expected
        if op == "contains": return expected in value if isinstance(value, list) else str(expected) in value if isinstance(value, str) else False
        if op == "not_contains": return expected not in value if isinstance(value, list) else str(expected) not in value if isinstance(value, str) else True
        if op == "in": return value in expected if isinstance(expected, list) else value == expected
        try:
            a, b = Decimal(str(value)), Decimal(str(expected))
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}.get(op, False)
        except (InvalidOperation, TypeError, ValueError): return False
    condition = condition or {}
    return (all(check(r) for r in condition.get("all", []))
            and (not condition.get("any") or any(check(r) for r in condition["any"])))


def next_node(n, answers):
    return next((route["next"] for route in n.get("routes", []) if matches(route.get("when"), answers)), n.get("next"))


def settle(session):
    s = state(session)
    for _ in range(len(s["document"]["nodes"]) + 1):
        n = node(session)
        if (not n.get("enabled", True) or not matches(n.get("show_if"), s["answers"])) and s.get("force_node") != n["id"]:
            dest = n.get("skip_next") or n.get("next")
        elif n.get("hidden_router"):
            dest = next_node(n, s["answers"])
        else:
            session["current_screen"] = "done" if n["kind"] == "end" else "constructor"
            sync_data(session)
            return
        if not dest: raise ValueError("Отключённый шаг должен иметь следующий шаг")
        s["node"] = dest
    raise ValueError("Цикл переходов")


def sync_data(session):
    s = state(session)
    data = session["data"]
    for key in ("request_type", "target", "request", "request_detail", "description", "insurance_type", "product", "subproduct", "document"):
        data[key] = s["answers"].get(key)
    data["details"] = {key[8:]: value for key, value in s["answers"].items() if key.startswith("details.")}
    file_ids, names, comments = [], [], []
    for value in s["answers"].values():
        if isinstance(value, dict) and "files" in value:
            comments.append(str(value.get("text") or ""))
            for f in value["files"]:
                if f["id"] not in file_ids:
                    file_ids.append(f["id"]); names.append(f.get("name", ""))
    data["document_file_ids"], data["document_file_names"] = file_ids, names
    data["document_comment"] = "\n".join(c for c in comments if c)
    if isinstance(data.get("document"), dict):
        data["document"] = data["document"].get("text", "")


def options(session):
    s = state(session)
    return [o for o in node(session).get("options", [])
            if o.get("enabled", True) and matches(o.get("show_if"), s["answers"])]


def summaries(session):
    s = state(session)
    result, seen = [], set()
    for n in s["document"]["nodes"]:
        key = n.get("bind")
        if key and key in s["answers"] and key not in seen:
            seen.add(key)
            result.append((n.get("label") or n.get("title") or key, answer_text(s["answers"], key)))
    return result


def description(session):
    return "\n\n".join(str(label) + ":\n" + value for label, value in summaries(session) if value)


def title(session):
    a = state(session)["answers"]
    return " | ".join(as_text(a[k]) for k in ("request_type", "initiative.title", "target", "request", "product") if a.get(k))[:250] or "Обращение DeskFlow"


def registry_fields(session):
    s = state(session)
    answers = s["answers"]
    parts, seen = {}, set()
    for n in s["document"]["nodes"]:
        field, bind = n.get("registry_field"), n.get("bind")
        if field and bind in answers and (field, bind) not in seen:
            seen.add((field, bind))
            if value := answer_text(answers, bind):
                parts.setdefault(field, []).append(value)
    result = {field: "\n".join(values) for field, values in parts.items()}
    result["UF_FLOWDESK_INITIATOR_ID"] = str(session["user_id"])
    result.setdefault("UF_FLOWDESK_REQUEST_TYPE", as_text(answers.get("request_type")))
    result.setdefault("UF_FLOWDESK_DETAILS", "\n".join(k[8:] + ": " + as_text(v) for k, v in answers.items() if k.startswith("details.")))
    if answers.get("request_type") == "Предложение / Идея":
        projected = {"data": deepcopy(session["data"])}
        ini = initiatives.state(projected)
        ini["profile"] = {key: answers.get("profile." + key, "") for key in initiatives.PROFILE_LABELS}
        for key in initiatives.META:
            value = answers.get("initiative." + key)
            ini[key] = ([value] if isinstance(value, str) else value or []) if key == "audience" else value
            if extra := answers.get("initiative." + key + ".other"):
                ini["other"][key] = extra
        for key, spec in initiatives.specs(projected).items():
            bind = "initiative." + key
            if value := answers.get(bind):
                ini["answers"][key] = {"choice": value, "text": answers.get(bind + "_explanation", "")} if spec["kind"] == "gate" else {"text": as_text(value)}
        for field, value in initiatives.registry_fields(projected).items():
            if value and (field == "UF_FLOWDESK_INIT_OPEN" or field.startswith("UF_FLOWDESK_INIT_") and len(parts.get(field, [])) <= 1):
                result[field] = value
    return result


def task_mapping(session):
    s = state(session)
    return task_values(s["document"]["settings"].get("mapping", {}), s["answers"])


def render(session):
    s = state(session); n = node(session); settings = s["document"]["settings"]
    labels, styles = settings.get("labels", {}), settings.get("styles", {})
    answers = s["answers"]
    buttons = []

    def message(key, default):
        return render_template(settings.get("messages", {}).get(key, default), answers, chat=True)

    def control(key, label, style="primary"):
        text = render_template(labels.get(key, label), answers, chat=True).strip() or label
        buttons.append({"label": text[:250], "action": "cx:" + key, "style": styles.get(key, style)})

    def custom_buttons():
        for opt in options(session) if n.get("input_mode") != "text" else []:
            item = deepcopy(opt)
            effect = item.get("action", "choice")
            item["style"] = opt.get("style") or styles.get("choice", "secondary")
            text = render_template(opt["text"], answers, chat=True).strip() or opt["text"]
            if n["kind"] == "multi" and effect == "choice":
                text = ("☑ " if opt["id"] in s["selected"] else "☐ ") + text
            item.update(label=text[:250], action="cx:option:" + opt["id"], option_id=opt["id"], link_type=effect)
            if effect not in {"link", "download"}:
                item.pop("link", None)
            buttons.append(item)

    current_text = render_template(n.get("title", ""), answers, chat=True).strip() or n.get("label") or "Выберите действие."
    if n.get("hint"):
        current_text += "\n\n[i]" + render_template(n["hint"], answers, chat=True) + "[/i]"
    if session.get("delivery_locked") and not session.get("task_id"):
        current_text = message("confirmed", "Обращение подтверждено. Ответы сохранены.")
        current_text += "\n\n" + str(session.get("delivery_error") or "Ожидает передачи")
        if session.get("delivery_status") == "failed":
            control("retry", "Повторить передачу")
        control("check_task", "Проверить наличие задачи", "secondary")
        control("restart", "Новое обращение", "base")
    elif s["status"] == "draft":
        current_text = message("saved", "Черновик сохранён.")
        control("resume", "Продолжить"); control("restart", "Новое обращение")
    elif s["status"] == "cancelled":
        current_text = message("cancelled", "Заполнение отменено.")
        control("restart", "Новое обращение")
    elif s["status"] == "information":
        control("restart", "Новое обращение")
    elif s.get("other"):
        current_text = message("other_prompt", "Уточните свой вариант") + "\n\n" + current_text
        control("back", "← Назад")
    elif s.get("editing"):
        current_text = message("edit_prompt", "Что изменить?")
        for label, binding in editable(session):
            buttons.append({"label": label, "action": "cx:edit:" + binding, "style": "secondary"})
        control("review", "К проверке")
    elif n["kind"] == "review":
        current_text += "\n\n" + neutral_markup(description(session))
        custom_buttons()
        if not any(o.get("action") == "create_task" for o in n.get("options", [])):
            control("submit", "Создать обращение")
        control("edit", "Исправить данные", "secondary")
    elif n["kind"] == "end":
        if session.get("task_id") and "{{system.task_id}}" not in n.get("title", ""):
            current_text += "\n\nНомер обращения: #" + str(session["task_id"])
        custom_buttons()
        control("restart", "Новое обращение")
    else:
        custom_buttons()
        if n["kind"] == "multi": control("continue", "Продолжить")
        elif n["kind"] == "files": control("continue", "Продолжить")
        elif n["kind"] == "info":
            if next_node(n, answers): control("continue", "Продолжить")
            else: control("restart", "Новое обращение")
        if not n.get("required") and n["kind"] in {"text", "number", "date", "files", "single", "multi"} and next_node(n, answers) and not (n["kind"] == "files" and answers.get(n["bind"])):
            control("skip", "Пропустить", "base")
        if n["kind"] == "files" and s["answers"].get(n.get("bind")):
            material = answers[n["bind"]]
            if isinstance(material, dict):
                current_text += "\n\n" + neutral_markup(as_text(material))
                control("files_replace", "Заменить вложения", "secondary")
                control("files_clear", "Удалить вложения", "alert")
                for file in material.get("files", []):
                    buttons.append({"label": "Удалить " + (file.get("name") or f"файл #{file['id']}")[:90],
                                    "action": f"cx:file_remove:{file['id']}", "style": "base"})
        if n["id"] == s["document"]["start"]:
            for draft in s.get("drafts", []):
                buttons.append({"label": "Продолжить черновик · " + draft["label"] + " #" + draft["id"][:8],
                                "action": "cx:draft:" + draft["id"], "style": "base"})
            if s.get("legacy_draft"):
                control("legacy_draft", "Продолжить прежний черновик", "base")
            if s.get("pending_delivery"):
                control("pending_delivery", "Проверить незавершённую отправку", "base")
    terminal = n["kind"] == "end" or n["kind"] == "info" and not next_node(n, answers) or s["status"] == "information"
    if (settings.get("show_history", True) and session.get("history_has_more")
            and (n["id"] == s["document"]["start"] or n["kind"] == "end")):
        control("history_more", "Показать ещё", "secondary")
    if s["status"] not in {"draft", "cancelled"} and not s.get("other") and not s.get("editing") and not terminal and not session.get("delivery_locked"):
        if n.get("allow_back", True) and s["trail"]: control("back", "← Назад")
        if n.get("allow_save", True): control("save", "Сохранить черновик", "secondary")
        if n.get("allow_cancel", True) and n["id"] != s["document"]["start"]:
            control("cancel", "Отменить", "alert")
    accepts_text = (n.get("input_mode") in {"text", "both"} or bool(s.get("other"))) and s["status"] == "in_progress" and not s.get("editing") and not session.get("delivery_locked") and n["kind"] not in {"review", "end", "info"}
    return {"screen": "done" if n["kind"] == "end" else "constructor", "text": current_text,
            "buttons": buttons, "accepts_text": accepts_text, "accepts_files": n["kind"] == "files" and accepts_text,
            "terminal": terminal or s["status"] in {"draft", "cancelled"}, "revision": session["revision"]}


def editable(session):
    s = state(session)
    seen = set(); values = []
    for n in s["document"]["nodes"]:
        binding = n.get("bind")
        if binding in s["answers"] and binding not in seen:
            seen.add(binding); values.append((n.get("label") or n.get("title") or binding, binding))
    return values


def bump(session):
    session["revision"] = int(session["revision"]) + 1


def advance(session, value=None, dest=None, *, choice=None, extra=None, replay=True):
    s = state(session)
    n = node(session)
    binding = n.get("bind")
    s["trail"].append({"node": n["id"], "answers": deepcopy(s["answers"]), "binding": binding,
                       "value": deepcopy(value), "choice": choice, "extra": extra})
    if binding and value is not None:
        s["answers"][binding] = value
        s["answers"].pop(binding + ".other", None)
        if extra:
            s["answers"][binding + ".other"] = extra
    s.update(selected=[], other=None, status="in_progress")
    s.pop("force_node", None)
    s.pop("file_replace", None)
    s["node"] = s.pop("edit_return", None) or dest or next_node(n, s["answers"])
    if not s["node"]:
        raise ValueError("Не указан следующий шаг")
    bump(session)
    settle(session)
    if replay:
        replay_answers(session)


def edit_from(session, position):
    s = state(session)
    trail = s["trail"]
    visit = trail[position]
    cache = s.get("replay") or {"nodes": {}, "fields": {}}
    for entry in trail[position:]:
        cached = {key: deepcopy(entry.get(key)) for key in ("value", "binding", "choice", "extra")}
        cache["nodes"][entry["node"]] = cached
        if entry.get("binding"):
            cache["fields"][entry["binding"]] = cached
    s.update(replay=cache, answers=deepcopy(visit["answers"]), trail=trail[:position], node=visit["node"],
             force_node=visit["node"], selected=[], other=None, editing=False)
    s.pop("file_replace", None)
    n = node(session)
    if n["kind"] == "files" and isinstance(visit.get("value"), dict):
        s["answers"][n["bind"]] = deepcopy(visit["value"])
    bump(session)
    settle(session)


def replay_answers(session):
    s = state(session)
    cache = s.get("replay")
    if not cache:
        return
    for _ in s["document"]["nodes"]:
        n = node(session)
        kind = n["kind"]
        if kind in {"review", "end"}:
            s.pop("replay", None)
            return
        entry = cache["nodes"].get(n["id"]) or cache["fields"].get(n.get("bind"))
        if not entry or entry["binding"] != n.get("bind"):
            return
        value, extra = deepcopy(entry["value"]), entry["extra"]
        choices = [o for o in options(session) if o.get("action", "choice") == "choice"]
        candidates = [o for o in choices if o.get("value", o["text"]) == value]
        selected = next((o for o in candidates if o["id"] == entry["choice"]), None)
        if not selected and kind in {"menu", "single"} and len(candidates) == 1:
            selected = candidates[0]
        if selected and (extra or not selected.get("other")):
            advance(session, value, selected.get("next"), choice=selected["id"], extra=extra, replay=False)
        elif value == "" and not n.get("required"):
            advance(session, value, replay=False)
        elif kind == "multi" and isinstance(value, list) and valid_choices(n, choices, value, extra):
            advance(session, value, extra=extra, replay=False)
        elif kind == "files" and isinstance(value, dict) and not materials_error(n, value):
            advance(session, value, replay=False)
        elif kind == "info" and next_node(n, s["answers"]) and not entry["choice"]:
            advance(session, replay=False)
        elif kind in {"text", "number", "date"} and n.get("input_mode") != "buttons" and isinstance(value, str) and not validation(n, value)[1]:
            advance(session, value, replay=False)
        else:
            return


def valid_choices(n, choices, values, extra):
    selected = [option for option in choices if option.get("value", option["text"]) in values]
    limits = n.get("limits", {})
    return (len(selected) == len(values) and limits.get("min_choices", int(bool(n.get("required")))) <= len(values) <= limits.get("max_choices", len(choices))
            and not (len(values) > 1 and any(o.get("exclusive") for o in selected))
            and (extra or not any(o.get("other") for o in selected)))


def materials_error(n, value):
    files = value.get("files", [])
    limits = n.get("limits", {})
    if n.get("required") and not (files or value.get("text")):
        return "Приложите материалы или ссылку"
    if len(files) > limits.get("max_files", 20):
        return "Слишком много файлов"
    extensions = {ext.lower().lstrip(".") for ext in limits.get("extensions", [])}
    for file in files:
        if extensions and file.get("name", "").rsplit(".", 1)[-1].lower() not in extensions:
            return "Недопустимый формат файла"
        if limits.get("max_file_mb") and (file.get("size") is None or file["size"] > limits["max_file_mb"] * 1024 * 1024):
            return "Размер файла неизвестен или превышает допустимый"
    return ""


def prepare_task(session, dest):
    s, n = state(session), node(session)
    if n.get("required") and not s["answers"].get(n.get("bind")):
        return {"status": "validation_error", "message": "Сначала заполните обязательный ответ."}
    if n["kind"] == "files" and (error := materials_error(n, s["answers"].get(n["bind"]) or {})):
        return {"status": "validation_error", "message": error}
    try:
        task_mapping(session)
    except ValueError as exc:
        return {"status": "validation_error", "message": str(exc)}
    s["success_node"] = dest
    return {"status": "task_ready"}


def validation(n, raw):
    limits = n.get("limits", {})
    raw = raw.strip()
    if not raw and n.get("required"): return None, "Ответ обязателен"
    if len(raw) < int(limits.get("min_length") or 0): return None, "Ответ слишком короткий"
    if len(raw) > int(limits.get("max_length") or 10000): return None, "Ответ слишком длинный"
    if limits.get("validator") == "bin_iin":
        raw = "".join(c for c in raw if c.isdigit())
        if len(raw) != 12: return None, "БИН / ИИН должен содержать 12 цифр"
    if n["kind"] == "number":
        try:
            v = Decimal(raw.replace(",", "."))
            if not v.is_finite(): raise InvalidOperation
            if limits.get("min") is not None and v < Decimal(str(limits["min"])): return None, "Число меньше допустимого"
            if limits.get("max") is not None and v > Decimal(str(limits["max"])): return None, "Число больше допустимого"
            raw = str(v)
        except InvalidOperation: return None, "Введите число"
    if n["kind"] == "date":
        try:
            if "." in raw:
                d, m, y = raw.split("."); parsed = date(int(y), int(m), int(d))
            else: parsed = date.fromisoformat(raw)
            if limits.get("min_date") and parsed < date.fromisoformat(limits["min_date"]): return None, "Дата раньше допустимой"
            if limits.get("max_date") and parsed > date.fromisoformat(limits["max_date"]): return None, "Дата позже допустимой"
            raw = parsed.isoformat()
        except ValueError: return None, "Введите дату ДД.ММ.ГГГГ"
    return raw, ""


def action(session, action):
    s = state(session); n = node(session)
    allowed = {b["action"] for b in render(session)["buttons"] if not b.get("link")}
    if action not in allowed: return {"status": "validation_error", "message": "Используйте текущие кнопки"}
    if action == "cx:restart": return {"status": "restart_requested"}
    if action.startswith("cx:draft:"): return {"status": "draft_resume_requested", "request_id": action[len("cx:draft:"):]}
    if action == "cx:legacy_draft": return {"status": "legacy_draft_requested"}
    if action == "cx:pending_delivery": return {"status": "pending_delivery_requested"}
    if action.startswith("cx:file_remove:"):
        value = s["answers"][n["bind"]]
        value["files"] = [file for file in value["files"] if str(file["id"]) != action.split(":")[-1]]
        bump(session); sync_data(session)
        return {"status": "ok"}
    if action == "cx:check_task": return {"status": "delivery_check_requested"}
    if action == "cx:files_clear":
        s["answers"].pop(n["bind"], None); bump(session); sync_data(session); return {"status": "ok"}
    if action == "cx:files_replace":
        s["file_replace"] = True; bump(session); return {"status": "ok"}
    if action == "cx:history_more":
        session["history_page"] = int(session.get("history_page") or 0) + 1
        bump(session); return {"status": "ok"}
    if action == "cx:submit": return prepare_task(session, n.get("next"))
    if action == "cx:retry": return {"status": "task_ready"}
    if action == "cx:save":
        s["status"] = "draft"; bump(session); return {"status": "draft_saved"}
    if action == "cx:cancel":
        s["status"] = "cancelled"; bump(session); return {"status": "cancelled"}
    if action == "cx:resume":
        s["status"] = "in_progress"; bump(session); return {"status": "ok"}
    if action == "cx:edit":
        s["editing"] = True; bump(session); return {"status": "ok"}
    if action == "cx:review":
        s["editing"] = False; bump(session); return {"status": "ok"}
    if action.startswith("cx:edit:"):
        binding = action[8:]
        visits = {entry["node"]: index for index, entry in enumerate(s["trail"])}
        candidates = [n for n in s["document"]["nodes"] if n.get("bind") == binding and n.get("enabled", True)]
        target = next((n for n in candidates if n["id"] in visits), candidates[0] if candidates else None)
        if target is None: return {"status": "validation_error", "message": "Вопрос для изменения не найден"}
        if target["id"] in visits:
            edit_from(session, visits[target["id"]])
        elif binding.startswith("profile."):
            s.update(edit_return=n["id"], node=target["id"], force_node=target["id"], editing=False)
            bump(session); settle(session)
        else:
            return {"status": "validation_error", "message": "Ответ относится к другому пути"}
        return {"status": "ok"}
    if action == "cx:back":
        return go_back(session)
    if action == "cx:skip":
        s["answers"].pop(n.get("bind"), None); advance(session, "")
        return {"status": "ok"}
    if action == "cx:continue":
        if n["kind"] == "multi":
            chosen = [o for o in options(session) if o["id"] in s["selected"] and o.get("action", "choice") == "choice"]
            limits = n.get("limits", {})
            if len(chosen) < int(limits.get("min_choices", 1 if n.get("required") else 0)):
                return {"status": "validation_error", "message": "Выберите больше вариантов"}
            if len(chosen) > int(limits.get("max_choices", len(options(session)))):
                return {"status": "validation_error", "message": "Выбрано слишком много вариантов"}
            other = next((o for o in chosen if o.get("other")), None)
            if other:
                s["other"] = {"option": other, "values": [o.get("value", o["text"]) for o in chosen]}
                bump(session); return {"status": "ok"}
            advance(session, [o.get("value", o["text"]) for o in chosen])
        elif n["kind"] == "files":
            value = s["answers"].get(n["bind"])
            if error := materials_error(n, value or {}):
                return {"status": "validation_error", "message": error}
            advance(session, value or {"files": [], "text": ""})
        else: advance(session)
        return {"status": "ok"}
    if action.startswith("cx:option:"):
        opt = next(o for o in options(session) if o["id"] == action[len("cx:option:"):])
        effect = opt.get("action", "choice")
        if effect == "back": return go_back(session)
        if effect == "create_task": return prepare_task(session, opt.get("next"))
        if effect == "cancel":
            s["status"] = "cancelled"
            bump(session)
            return {"status": "cancelled"}
        if effect in {"save_draft", "finish"}:
            s["status"] = "draft" if effect == "save_draft" else "information"
            bump(session)
            return {"status": "draft_saved" if effect == "save_draft" else "information"}
        if n["kind"] == "multi":
            if opt["id"] in s["selected"]: s["selected"].remove(opt["id"])
            else:
                if opt.get("exclusive"): s["selected"] = []
                else:
                    exclusive = {o["id"] for o in options(session) if o.get("exclusive")}
                    s["selected"] = [key for key in s["selected"] if key not in exclusive]
                s["selected"].append(opt["id"])
            bump(session)
        elif opt.get("other"):
            s["other"] = {"option": opt}; bump(session)
        else: advance(session, opt.get("value", opt["text"]), opt.get("next"), choice=opt["id"])
        return {"status": "ok"}
    return {"status": "buttons_expected"}


def go_back(session):
    s = state(session)
    if s.get("other"):
        s["other"] = None
        bump(session)
    elif s.get("edit_return"):
        s["node"] = s.pop("edit_return")
        s.pop("force_node", None)
        bump(session); settle(session)
    elif s["trail"]:
        edit_from(session, len(s["trail"]) - 1)
    else:
        return {"status": "validation_error", "message": "Это первый экран"}
    return {"status": "ok"}


def text(session, value):
    s = state(session); n = node(session)
    if not render(session)["accepts_text"]: return {"status": "buttons_expected"}
    normalized, error = validation(n, value)
    if error: return {"status": "validation_error", "message": error}
    if s.get("other"):
        if not normalized: return {"status": "validation_error", "message": "Уточните свой вариант"}
        other = s["other"]; opt = other["option"]
        answer = opt.get("value", opt["text"])
        if "values" in other:
            answer = [answer if v == opt.get("value") else v for v in other["values"]]
        advance(session, answer, opt.get("next"), choice=opt["id"], extra=normalized)
        return {"status": "ok"}
    if n["kind"] in {"menu", "single"}:
        opt = next((o for o in options(session) if o.get("action", "choice") == "choice" and value.strip().casefold() == o["text"].casefold()), None)
        if opt:
            if opt.get("other"):
                s["other"] = {"option": opt}; bump(session)
            else:
                advance(session, opt.get("value", opt["text"]), opt.get("next"), choice=opt["id"])
            return {"status": "ok"}
        if not n.get("next"): return {"status": "validation_error", "message": "Выберите один из вариантов"}
    if n["kind"] == "files":
        value = deepcopy(s["answers"].get(n["bind"]) or {"files": [], "text": ""})
        value["text"] = normalized
        s["answers"][n["bind"]] = value; bump(session); sync_data(session)
    elif n["kind"] == "multi":
        choices = [o for o in options(session) if o.get("action", "choice") == "choice"]
        names = {o["text"].casefold(): o for o in choices}
        tokens = list(dict.fromkeys(part.strip().casefold() for part in normalized.split(";") if part.strip()))
        selected = [names[token] for token in tokens if token in names]
        values = [o.get("value", o["text"]) for o in selected]
        other = next((o for o in selected if o.get("other")), None)
        if len(selected) != len(tokens) or not valid_choices(n, choices, values, bool(other)):
            return {"status": "validation_error", "message": "Введите названия допустимых вариантов через точку с запятой."}
        if other:
            s["other"] = {"option": other, "values": values}; bump(session)
        else:
            advance(session, values)
    else: advance(session, normalized)
    return {"status": "ok"}


def attachments(session, files, text=""):
    s = state(session); n = node(session)
    if not render(session)["accepts_files"]: return {"status": "buttons_expected"}
    value = deepcopy(s["answers"].get(n["bind"]) or {"files": [], "text": ""})
    if s.get("file_replace"):
        value["files"] = []
    existing = {f["id"]: f for f in value["files"]}
    for file in files:
        fid = int(file["id"])
        if fid <= 0:
            return {"status": "validation_error", "message": "Файл недоступен"}
        existing[fid] = {"id": fid, "name": str(file.get("name") or "")[:250], "size": file.get("size")}
    normalized, error = validation({**n, "required": False}, text)
    if error: return {"status": "validation_error", "message": error}
    value["files"] = list(existing.values())
    if text: value["text"] = normalized
    if error := materials_error(n, value):
        return {"status": "validation_error", "message": error}
    s["answers"][n["bind"]] = value; bump(session); sync_data(session)
    s.pop("file_replace", None)
    return {"status": "ok"}


def created(session, task_id):
    session["task_id"] = str(task_id)
    s = state(session)
    s["status"] = "created"; s["selected"] = []; s["editing"] = False
    s["answers"]["system.task_id"] = str(task_id)
    s["node"] = s.get("success_node") or node(session).get("next") or next(n["id"] for n in s["document"]["nodes"] if n["kind"] == "end")
    session["current_screen"] = "done"; bump(session)
