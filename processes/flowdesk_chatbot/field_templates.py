"""Answer interpolation and validation shared by the editor and bot."""
from datetime import date, datetime
import re
from urllib.parse import parse_qsl, unquote, urlsplit

TOKEN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_.]{0,100})\}\}")
STANDARD_FIELDS = {
    "TITLE": "Название", "DESCRIPTION": "Описание", "DEADLINE": "Срок",
    "PRIORITY": "Приоритет (0–2)", "GROUP_ID": "Проект", "RESPONSIBLE_ID": "Ответственный",
}
PROFILE_FIELDS = {"profile.name", "profile.department", "profile.manager", "profile.contact"}
SYSTEM_FIELDS = {"system.user_id", "system.request_id", "system.task_id"}
PROTECTED_FIELDS = {"UF_TASK_WEBDAV_FILES", "UF_FLOWDESK_INITIATOR_ID"}


def as_text(value):
    if isinstance(value, list):
        return "; ".join(as_text(item) for item in value)
    if isinstance(value, dict):
        files = [f.get("name") or f"Файл #{f['id']}" for f in value.get("files", [])]
        return "\n".join(filter(None, [value.get("text", ""), *files]))
    return str(value) if value is not None else ""


def answer_text(answers, key):
    return " — ".join(filter(None, [as_text(answers.get(key)), as_text(answers.get(key + ".other"))]))


def neutral_markup(value):
    return value.replace("[", "［").replace("]", "］")


def render_template(template, answers, *, chat=False):
    def substitute(match):
        value = answer_text(answers, match.group(1))
        return neutral_markup(value) if chat else value
    return TOKEN.sub(substitute, template)


def template_errors(template, bindings):
    if not isinstance(template, str) or len(template) > 20000:
        return ["Шаблон должен быть текстом до 20 000 символов"]
    errors = ["Неизвестный ответ " + field for field in sorted(set(TOKEN.findall(template)) - bindings)]
    remaining = TOKEN.sub("", template)
    if "{{" in remaining or "}}" in remaining:
        errors.append("Вставьте ответ в формате {{имя_ответа}}")
    return errors


def mapping_errors(rows, bindings):
    if not isinstance(rows, dict) or len(rows) > 100:
        return ["Укажите до 100 полей задачи"]
    errors = []
    for name, spec in rows.items():
        if name in PROTECTED_FIELDS or name not in STANDARD_FIELDS and not re.fullmatch(r"UF_[A-Z0-9_]{1,80}", name):
            errors.append("Недоступное поле задачи " + name)
        elif not isinstance(spec, dict) or type(spec.get("required", False)) is not bool:
            errors.append("Неверная настройка поля " + name)
        else:
            errors.extend(f"{name} — {error}" for error in template_errors(spec.get("template"), bindings))
            if isinstance(spec.get("template"), str) and not TOKEN.search(spec["template"]):
                try:
                    task_values({name: spec}, {})
                except ValueError as exc:
                    errors.append(str(exc))
    return errors


def task_values(rows, answers):
    values = {}
    for field, spec in rows.items():
        template = spec["template"]
        refs = TOKEN.findall(template)
        filled = [bool(answer_text(answers, key).strip()) for key in refs]
        value = render_template(template, answers).strip()
        if refs and not any(filled):
            value = ""
        label = STANDARD_FIELDS.get(field, field)
        if (spec.get("required") and (not value or not all(filled))) or (not value and field in {"TITLE", "RESPONSIBLE_ID"}):
            raise ValueError(f"Заполните данные для поля задачи «{label}».")
        if not value:
            continue
        if field in {"GROUP_ID", "RESPONSIBLE_ID", "PRIORITY"}:
            if not re.fullmatch(r"[0-9]{1,10}", value):
                raise ValueError(f"Поле «{label}» должно содержать целое число.")
            value = int(value)
            if field == "RESPONSIBLE_ID" and value == 0 or field == "PRIORITY" and value > 2:
                raise ValueError(f"Недопустимое значение поля «{label}».")
        if field == "TITLE" and len(value) > 250:
            raise ValueError("Название задачи должно содержать до 250 символов.")
        if field == "DEADLINE":
            try:
                if len(value) == 10:
                    value = date.fromisoformat(value).isoformat() + "T23:59:59+05:00"
                else:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None:
                        raise ValueError()
                    value = parsed.isoformat()
            except ValueError:
                raise ValueError("Укажите срок датой ГГГГ-ММ-ДД или датой со временем и часовым поясом.") from None
        values[field] = value
    return values


def validated_link(raw, *, download=False):
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2048:
        raise ValueError("Укажите адрес ссылки длиной до 2048 символов")
    decoded = raw
    for _ in range(4):
        decoded = unquote(decoded)
        if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in decoded):
            raise ValueError("В ссылке есть недопустимые символы")
    if any(char.isspace() for char in raw) or "{{" in raw:
        raise ValueError("Укажите постоянный адрес без пробелов")
    parts = urlsplit(raw)
    relative = not parts.scheme and not parts.netloc and raw.startswith("/") and not decoded.startswith("//")
    if not relative and (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None):
        raise ValueError("Нужна ссылка HTTPS или путь внутри Б24")
    path = urlsplit(decoded).path.casefold()
    if any(part in {".", "..", "rest"} for part in path.split("/")) or "/deskflow-admin" in path or "/deskflow-action" in path:
        raise ValueError("Используйте ссылку на материал или рабочий раздел")
    if any(re.search(r"(?:^auth$|token|password|signature|sessid)", key, re.I)
           for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError("Ссылка содержит данные авторизации")
    if download and (not relative or not path.startswith(("/disk/", "/bitrix/tools/disk/"))):
        raise ValueError("Для скачивания укажите путь к файлу на Диске этого Б24")
    return raw
