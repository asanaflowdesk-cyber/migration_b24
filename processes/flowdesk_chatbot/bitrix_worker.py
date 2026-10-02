from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common.bitrix import BitrixClient, BitrixError, sanitize_error
from processes.flowdesk_chatbot.engine import (
    mark_task_created,
    new_session,
    submit_action,
    submit_attachment_message,
    submit_text,
    task_description,
    task_registry_fields,
    task_title,
    view,
)
from processes.flowdesk_chatbot.storage import SessionStore

LOG = logging.getLogger("flowdesk_chatbot")

TRIGGERS = {"sos", "help", "помощь", "чп", "/start", "начать"}
COMMAND_NAME = "flowdesk"

TASK_USER_FIELDS: dict[str, dict[str, Any]] = {
    "UF_FLOWDESK_REQUEST_TYPE": {"label": "Тип обращения", "sort": 200, "rows": 1},
    "UF_FLOWDESK_TARGET": {"label": "К кому / подразделение", "sort": 210, "rows": 1},
    "UF_FLOWDESK_REQUEST": {"label": "Запрос", "sort": 220, "rows": 1},
    "UF_FLOWDESK_REQUEST_DETAIL": {"label": "Уточнение запроса", "sort": 230, "rows": 1},
    "UF_FLOWDESK_INSURANCE_TYPE": {"label": "Вид страхования", "sort": 240, "rows": 1},
    "UF_FLOWDESK_INSURANCE_CLASS": {"label": "Класс страхования", "sort": 250, "rows": 1},
    "UF_FLOWDESK_PRODUCT": {"label": "Продукт", "sort": 260, "rows": 1},
    "UF_FLOWDESK_DETAILS": {"label": "Детализация", "sort": 270, "rows": 8},
    "UF_FLOWDESK_DESCRIPTION": {"label": "Суть обращения", "sort": 280, "rows": 12},
    "UF_FLOWDESK_INITIATOR_ID": {"label": "Инициатор обращения ID", "sort": 290, "rows": 1},
}


class Runtime:
    def __init__(self) -> None:
        self.client = BitrixClient.from_env()
        self.db_path = os.getenv(
            "FLOWDESK_CHATBOT_DB",
            "processes/flowdesk_chatbot/flowdesk_state.sqlite3",
        )
        self.store = SessionStore(self.db_path)
        self.bot_name = os.getenv("FLOWDESK_BOT_NAME", "DeskFlow")
        self.bot_token = self._load_or_create_bot_token()
        suffix = self.bot_token[:8].lower().replace("-", "_")
        self.bot_code = os.getenv("FLOWDESK_BOT_CODE", f"flowdesk_chatbot_{suffix}")
        self.command_name = os.getenv("FLOWDESK_COMMAND_NAME", f"flowdesk_{suffix}")
        self.bot_id = 0
        self._last_api_call = 0.0
        webhook = os.getenv("TARGET_BITRIX_WEBHOOK_URL", "").strip()
        parsed = urlsplit(webhook)
        self.portal_base = (
            f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        )
        self.task_user_fields: set[str] = set()
        self.event_poll_seconds = float(
            os.getenv("FLOWDESK_EVENT_POLL_SECONDS", "1.5")
        )

    def _load_or_create_bot_token(self) -> str:
        configured = os.getenv("FLOWDESK_BOT_TOKEN", "").strip()
        if configured:
            if len(configured) > 40:
                raise ValueError("FLOWDESK_BOT_TOKEN должен быть не длиннее 40 символов")
            return configured

        saved = self.store.get_meta("bot_token")
        if saved:
            return saved

        token = secrets.token_urlsafe(24)[:40]
        self.store.set_meta("bot_token", token)
        return token

    def _throttle(self) -> None:
        # Keep a single conservative limit for classic and REST 3.0 calls.
        elapsed = time.monotonic() - self._last_api_call
        if elapsed < 0.52:
            time.sleep(0.52 - elapsed)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._throttle()
        result = self.client.call(method, params or {})
        self._last_api_call = time.monotonic()
        return result

    def call_v3(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._throttle()
        result = self.client.call_v3(method, params or {})
        self._last_api_call = time.monotonic()
        return result

    def register(self) -> None:
        desired_type = "personal"
        fields = {
            "code": self.bot_code,
            "botToken": self.bot_token,
            "properties": {
                "name": self.bot_name,
                "workPosition": "Внутренние обращения",
            },
            "type": desired_type,
            "eventMode": "fetch",
        }

        result = self.call(
            "imbot.v2.Bot.register",
            {"fields": fields},
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"Bot.register вернул неожиданный ответ: {result!r}")

        bot = result.get("bot")
        if not isinstance(bot, dict) or not bot.get("id"):
            raise RuntimeError(f"Bot.register не вернул bot.id: {result!r}")

        # Re-registering the same code returns the existing bot and does not
        # change its immutable type. Migrate old test bot once so it can see
        # every ordinary message in its per-user service chat.
        current_type = str(bot.get("type") or "")
        if current_type and current_type != desired_type:
            old_bot_id = int(bot["id"])
            LOG.warning(
                "Migrating DeskFlow bot type %s -> %s (old botId=%s)",
                current_type,
                desired_type,
                old_bot_id,
            )
            self.call(
                "imbot.v2.Bot.unregister",
                {
                    "botId": old_bot_id,
                    "botToken": self.bot_token,
                },
            )
            result = self.call(
                "imbot.v2.Bot.register",
                {"fields": fields},
            )
            if not isinstance(result, dict):
                raise RuntimeError(
                    f"Bot.register после миграции вернул неожиданный ответ: {result!r}"
                )
            bot = result.get("bot")
            if not isinstance(bot, dict) or not bot.get("id"):
                raise RuntimeError(
                    f"Bot.register после миграции не вернул bot.id: {result!r}"
                )
            self.store.set_meta("event_offset", "0")

        self.bot_id = int(bot["id"])
        self.store.set_meta("bot_id", self.bot_id)

        self.call(
            "imbot.v2.Command.register",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "fields": {
                    "command": self.command_name,
                    "common": False,
                    "hidden": True,
                    "extranetSupport": False,
                },
            },
        )

        # Custom task fields are optional at transport level, but they are what
        # makes the registry exportable without parsing DESCRIPTION. Creation is
        # best-effort because task user fields require administrator permission.
        self.ensure_task_user_fields()

    def ensure_task_user_fields(self) -> None:
        try:
            result = self.call(
                "task.item.userfield.getlist",
                {"ORDER": {"SORT": "ASC"}},
            )
        except Exception as exc:
            LOG.warning(
                "Could not read task user fields; structured registry fields disabled: %s",
                sanitize_error(exc),
            )
            self.task_user_fields = set()
            return

        rows = result if isinstance(result, list) else []
        existing = {
            str(row.get("FIELD_NAME") or row.get("fieldName") or "")
            for row in rows
            if isinstance(row, dict)
        }

        for field_name, spec in TASK_USER_FIELDS.items():
            if field_name in existing:
                continue
            try:
                self.call(
                    "task.item.userfield.add",
                    {
                        "PARAMS": {
                            "USER_TYPE_ID": "string",
                            "FIELD_NAME": field_name,
                            "XML_ID": field_name,
                            "LABEL": spec["label"],
                            "EDIT_FORM_LABEL": {
                                "ru": spec["label"],
                                "en": spec["label"],
                            },
                            "SORT": int(spec["sort"]),
                            "MULTIPLE": "N",
                            "MANDATORY": "N",
                            "SETTINGS": {
                                "ROWS": int(spec["rows"]),
                            },
                        }
                    },
                )
            except BitrixError as exc:
                LOG.warning(
                    "Task user field %s was not created: %s",
                    field_name,
                    sanitize_error(exc),
                )
                continue
            except Exception as exc:
                LOG.warning(
                    "Task user field %s creation failed: %s",
                    field_name,
                    sanitize_error(exc),
                )
                continue
            existing.add(field_name)
            LOG.info("Created task user field %s", field_name)

        self.task_user_fields = existing

    @staticmethod
    def session_key(dialog_id: str, user_id: int) -> str:
        return f"{dialog_id}:{user_id}"

    @staticmethod
    def service_chat_meta_key(user_id: int) -> str:
        return f"service_chat:{int(user_id)}"

    def get_service_chat(self, user_id: int) -> str:
        return self.store.get_meta(self.service_chat_meta_key(user_id)).strip()

    def ensure_service_chat(self, user_id: int, user_name: str = "") -> str:
        existing = self.get_service_chat(user_id)

        if existing:
            try:
                result = self.call(
                    "imbot.v2.Chat.get",
                    {
                        "botId": self.bot_id,
                        "botToken": self.bot_token,
                        "dialogId": existing,
                    },
                )
                chat = result.get("chat") if isinstance(result, dict) else None
                if isinstance(chat, dict) and str(chat.get("dialogId") or "") == existing:
                    return existing
            except Exception as exc:
                LOG.warning(
                    "Saved service chat %s is unavailable for user=%s: %s",
                    existing,
                    user_id,
                    sanitize_error(exc),
                )

        title_name = (user_name or "").strip() or f"ID {user_id}"
        result = self.call(
            "imbot.v2.Chat.add",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "fields": {
                    "title": f"DeskFlow — {title_name}",
                    "description": "Служебный чат для внутренних обращений DeskFlow",
                    "userIds": [int(user_id)],
                },
            },
        )

        chat = result.get("chat") if isinstance(result, dict) else None
        dialog_id = str(chat.get("dialogId") or "") if isinstance(chat, dict) else ""
        if not dialog_id.startswith("chat"):
            raise RuntimeError(f"Chat.add не вернул dialogId группового чата: {result!r}")

        self.store.set_meta(self.service_chat_meta_key(user_id), dialog_id)
        LOG.info("Service chat created for user=%s: %s", user_id, dialog_id)
        return dialog_id

    def delete_chat_message(self, message_id: int) -> bool:
        if not message_id:
            return False

        # Message cleanup must never wedge the event queue. If this particular
        # Bitrix build refuses deletion, continue the scenario and log it.
        try:
            result = self.call(
                "imbot.v2.Chat.Message.delete",
                {
                    "botId": self.bot_id,
                    "botToken": self.bot_token,
                    "messageId": int(message_id),
                    "complete": True,
                },
            )
        except Exception as exc:
            LOG.warning(
                "Message cleanup skipped for message=%s: %s",
                message_id,
                sanitize_error(exc),
            )
            return False

        ok = result is True or (
            isinstance(result, dict) and result.get("result") is True
        )
        if not ok:
            LOG.warning(
                "Message cleanup returned unexpected result for message=%s: %r",
                message_id,
                result,
            )
            return False

        return True

    def keyboard(self, session: dict[str, Any]) -> dict[str, Any] | None:
        current = view(session)
        if not current["buttons"]:
            return None

        buttons = []
        for index, item in enumerate(current["buttons"]):
            style = str(item.get("style") or "secondary")
            button: dict[str, Any] = {
                "TEXT": item["label"],
                "COMMAND": f"/{self.command_name}",
                "COMMAND_PARAMS": f"{current['revision']}:{index}",
                "BLOCK": "Y",
                "DISPLAY": "LINE",
                "BG_COLOR_TOKEN": style,
            }

            # Secondary is the calm light-blue choice style. Back/confirm/new
            # request use primary, while urgent contract and complaint use alert.
            if style == "secondary":
                button["TEXT_COLOR"] = "#2067B0"
            elif style in {"primary", "alert"}:
                button["TEXT_COLOR"] = "#FFFFFF"

            buttons.append(button)

        return {
            "BOT_ID": self.bot_id,
            "BUTTONS": buttons,
        }

    def message_fields(
        self,
        text: str,
        *,
        session: dict[str, Any] | None = None,
        link_button: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"message": text}

        keyboard = self.keyboard(session) if session is not None else None

        if link_button:
            link = {
                "TEXT": link_button["text"],
                "LINK": link_button["link"],
                "BG_COLOR_TOKEN": "primary",
                "BLOCK": "Y",
                "DISPLAY": "LINE",
            }
            if keyboard:
                keyboard["BUTTONS"].insert(0, link)
            else:
                keyboard = {
                    "BOT_ID": self.bot_id,
                    "BUTTONS": [link],
                }

        if keyboard:
            fields["keyboard"] = keyboard

        return fields

    def set_text_field(self, dialog_id: str, enabled: bool) -> bool:
        # Some on-premise Bitrix24 builds may not expose this newer UI method.
        # Text-field control is cosmetic; it must never block the business flow.
        try:
            result = self.call(
                "imbot.v2.Chat.TextField.enabled",
                {
                    "botId": self.bot_id,
                    "botToken": self.bot_token,
                    "dialogId": dialog_id,
                    "enabled": bool(enabled),
                },
            )
        except Exception as exc:
            LOG.warning(
                "Text-field toggle skipped for dialog=%s enabled=%s: %s",
                dialog_id,
                enabled,
                sanitize_error(exc),
            )
            return False

        ok = result is True or (
            isinstance(result, dict) and result.get("result") is True
        )
        if not ok:
            LOG.warning(
                "Text-field toggle returned unexpected result for dialog=%s: %r",
                dialog_id,
                result,
            )
            return False

        return True

    def send(
        self,
        dialog_id: str,
        text: str,
        *,
        session: dict[str, Any] | None = None,
        link_button: dict[str, str] | None = None,
    ) -> int:
        fields = self.message_fields(
            text,
            session=session,
            link_button=link_button,
        )

        result = self.call(
            "imbot.v2.Chat.Message.send",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "dialogId": dialog_id,
                "fields": fields,
            },
        )

        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError(f"Message.send не вернул id: {result!r}")
        return int(result["id"])

    def answer_command(
        self,
        data: dict[str, Any],
        text: str,
        *,
        session: dict[str, Any] | None = None,
        link_button: dict[str, str] | None = None,
    ) -> None:
        command = data.get("command") or {}
        message = data.get("message") or {}
        chat = data.get("chat") or {}

        command_id = int(command.get("id") or 0)
        message_id = int(message.get("id") or 0)
        dialog_id = str(chat.get("dialogId") or "")

        if not command_id or not message_id or not dialog_id:
            raise ValueError(
                f"Неполные данные ONIMBOTV2COMMANDADD: "
                f"command_id={command_id}, message_id={message_id}, dialog_id={dialog_id!r}"
            )

        fields = self.message_fields(
            text,
            session=session,
            link_button=link_button,
        )

        result = self.call(
            "imbot.v2.Command.answer",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "commandId": command_id,
                "messageId": message_id,
                "dialogId": dialog_id,
                "fields": fields,
            },
        )

        if not isinstance(result, dict) or result.get("result") is not True:
            raise RuntimeError(f"Command.answer вернул неожиданный ответ: {result!r}")

    def update_message(
        self,
        message_id: int,
        text: str,
        *,
        session: dict[str, Any] | None = None,
        link_button: dict[str, str] | None = None,
    ) -> None:
        fields = self.message_fields(
            text,
            session=session,
            link_button=link_button,
        )

        result = self.call(
            "imbot.v2.Chat.Message.update",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "messageId": int(message_id),
                "fields": fields,
            },
        )

        if not isinstance(result, dict):
            raise RuntimeError(f"Message.update вернул неожиданный ответ: {result!r}")

    def recent_flowdesk_tasks(
        self,
        user_id: int,
        needed: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0

        # Bitrix returns 50 tasks per classic list page. Continue only while
        # needed so opening the bot does not turn into a heavy registry query.
        for _ in range(10):
            result = self.call(
                "tasks.task.list",
                {
                    "order": {"ID": "desc"},
                    "filter": {"CREATED_BY": int(user_id)},
                    "select": ["ID", "TITLE", "XML_ID", "CREATED_DATE"],
                    "start": start,
                },
            )
            page = []
            if isinstance(result, dict):
                page = result.get("tasks") or result.get("items") or []
            elif isinstance(result, list):
                page = result

            if not isinstance(page, list) or not page:
                break

            for row in page:
                if not isinstance(row, dict):
                    continue
                xml_id = str(row.get("xmlId") or row.get("XML_ID") or "")
                if xml_id.startswith("FLOWDESK_CHAT_"):
                    rows.append(row)
                    if len(rows) >= needed:
                        return rows

            if len(page) < 50:
                break
            start += 50

        return rows

    def history_block(self, session: dict[str, Any]) -> str:
        page = max(0, int(session.get("history_page") or 0))
        shown = (page + 1) * 10
        rows = self.recent_flowdesk_tasks(int(session["user_id"]), shown + 1)
        session["history_has_more"] = len(rows) > shown

        visible = rows[:shown]
        if not visible:
            return ""

        lines = ["", "", "[b]Последние обращения 📋[/b]"]
        for row in visible:
            task_id = str(row.get("id") or row.get("ID") or "").strip()
            title = str(row.get("title") or row.get("TITLE") or "").strip()
            if not task_id:
                continue

            parts = [part.strip() for part in title.split("|") if part.strip()]
            request_type = parts[0] if parts else "Обращение"
            target = parts[1] if len(parts) > 1 else ""
            label = f"#{task_id} · {request_type}"
            if target:
                label += f" · {target}"

            link = self.task_link(int(session["user_id"]), task_id)
            lines.append(f"[url={link}]{label}[/url]")

        return "\n".join(lines)

    def render_current(
        self,
        session: dict[str, Any],
        *,
        link_button: dict[str, str] | None = None,
        text_override: str | None = None,
    ) -> None:
        history = ""
        if session.get("current_screen") in {"type", "done"} and not session.get("edit_mode"):
            history = self.history_block(session)

        current = view(session)
        text = text_override if text_override is not None else current["text"]
        if history:
            text += history

        # Global UX rule: text is available only on screens that explicitly
        # accept text/files. Button-only screens, start and final included, are locked.
        self.set_text_field(
            session["dialog_id"],
            bool(current["accepts_text"]),
        )

        message_id = session.get("active_message_id")
        if message_id:
            self.update_message(
                int(message_id),
                text,
                session=session,
                link_button=link_button,
            )
        else:
            message_id = self.send(
                session["dialog_id"],
                text,
                session=session,
                link_button=link_button,
            )
            session["active_message_id"] = int(message_id)

        key = self.session_key(session["dialog_id"], int(session["user_id"]))
        self.store.put_session(key, session)

        if current["terminal"] and current["screen"] == "instruction":
            session["current_screen"] = "done"
            session["history"] = []
            session["revision"] = int(session["revision"]) + 1
            self.store.put_session(key, session)

    def find_existing_task(self, xml_id: str) -> str:
        result = self.call(
            "tasks.task.list",
            {
                "filter": {"=XML_ID": xml_id},
                "select": ["ID", "XML_ID"],
            },
        )

        if isinstance(result, dict):
            rows = result.get("tasks") or result.get("items") or []
        elif isinstance(result, list):
            rows = result
        else:
            rows = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            row_xml = str(row.get("xmlId") or row.get("XML_ID") or "")
            if row_xml == xml_id:
                return str(row.get("id") or row.get("ID") or "")
        return ""

    def create_task(self, session: dict[str, Any]) -> str:
        user_id = int(session["user_id"])
        responsible_id = int(os.getenv("FLOWDESK_DEFAULT_RESPONSIBLE_ID", str(user_id)))
        project_id = int(os.getenv("FLOWDESK_PROJECT_ID", "0"))
        xml_id = f"FLOWDESK_CHAT_{session['request_id']}"

        existing = self.find_existing_task(xml_id)
        if existing:
            return existing

        fields: dict[str, Any] = {
            "TITLE": task_title(session),
            "DESCRIPTION": task_description(session),
            "CREATED_BY": user_id,
            "RESPONSIBLE_ID": responsible_id,
            "XML_ID": xml_id,
        }
        if project_id > 0:
            fields["GROUP_ID"] = project_id

        for field_name, value in task_registry_fields(session).items():
            if field_name in self.task_user_fields:
                fields[field_name] = value

        file_ids = [
            int(file_id)
            for file_id in (session["data"].get("document_file_ids") or [])
            if str(file_id).isdigit() and int(file_id) > 0
        ]
        if file_ids:
            fields["UF_TASK_WEBDAV_FILES"] = [
                f"n{file_id}" for file_id in file_ids
            ]

        result = self.call("tasks.task.add", {"fields": fields})
        if not isinstance(result, dict):
            raise RuntimeError(f"tasks.task.add вернул неожиданный ответ: {result!r}")

        task = result.get("task")
        if not isinstance(task, dict) or not task.get("id"):
            raise RuntimeError(f"tasks.task.add не вернул task.id: {result!r}")

        task_id = str(task["id"])
        self.send_attachment_comment(session, task_id)
        return task_id

    def send_attachment_comment(
        self,
        session: dict[str, Any],
        task_id: str,
    ) -> None:
        comment = str(session["data"].get("document_comment") or "").strip()
        if not comment:
            return

        try:
            self.call_v3(
                "tasks.task.chat.message.send",
                {
                    "fields": {
                        "taskId": int(task_id),
                        "text": comment,
                    }
                },
            )
            return
        except Exception as exc:
            LOG.warning(
                "REST 3.0 task chat message failed for task=%s, using legacy fallback: %s",
                task_id,
                sanitize_error(exc),
            )

        try:
            self.call(
                "task.commentitem.add",
                {
                    "TASKID": int(task_id),
                    "FIELDS": {
                        "POST_MESSAGE": comment,
                        "AUTHOR_ID": int(session["user_id"]),
                    },
                },
            )
        except Exception as exc:
            # The task itself must remain successfully created even if this box
            # version refuses both comment transports.
            LOG.error(
                "Attachment comment could not be written to task=%s: %s",
                task_id,
                sanitize_error(exc),
            )

    def task_link(self, user_id: int, task_id: str) -> str:
        path = f"/company/personal/user/{user_id}/tasks/task/view/{task_id}/"
        return f"{self.portal_base}{path}" if self.portal_base else path

    def restart_session(self, dialog_id: str, user_id: int) -> dict[str, Any]:
        key = self.session_key(dialog_id, user_id)
        previous = self.store.get_session(key)
        active_message_id = previous.get("active_message_id") if previous else None

        session = new_session(user_id=user_id, dialog_id=dialog_id)
        session["active_message_id"] = active_message_id
        self.store.put_session(key, session)
        return session

    @staticmethod
    def message_files(message: dict[str, Any]) -> list[dict[str, Any]]:
        params = message.get("params") or {}
        raw = params.get("files") or params.get("FILES") or []

        if isinstance(raw, dict):
            raw = list(raw.values())
        elif not isinstance(raw, list):
            raw = [raw] if raw else []

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
                )
                name = str(item.get("name") or item.get("NAME") or "").strip()
            else:
                value = item

            try:
                file_id = int(value or 0)
            except (TypeError, ValueError):
                file_id = 0

            if file_id <= 0 or file_id in seen:
                continue

            seen.add(file_id)
            files.append({"id": file_id, "name": name})

        return files

    def handle_message(self, data: dict[str, Any]) -> None:
        user = data.get("user") or {}
        message = data.get("message") or {}
        chat = data.get("chat") or {}

        if user.get("bot"):
            return

        user_id = int(user.get("id") or message.get("authorId") or 0)
        source_dialog_id = str(chat.get("dialogId") or user_id)
        message_id = int(message.get("id") or 0)
        text = str(message.get("text") or "").strip()
        files = self.message_files(message)
        user_name = str(user.get("name") or "").strip()

        if not user_id or not source_dialog_id:
            return

        service_dialog_id = self.get_service_chat(user_id)
        is_service_chat = bool(
            service_dialog_id
            and source_dialog_id == service_dialog_id
            and source_dialog_id.startswith("chat")
        )

        if text.casefold() in TRIGGERS:
            # A trigger may arrive in the old personal dialog. The actual UI
            # always lives in a bot-owned service chat where DeskFlow is owner.
            if not is_service_chat:
                service_dialog_id = self.ensure_service_chat(user_id, user_name)
            else:
                service_dialog_id = source_dialog_id
                # In the managed chat the trigger itself must disappear too.
                self.delete_chat_message(message_id)

            LOG.info(
                "Trigger received from user=%s source=%s service=%s text=%r",
                user_id,
                source_dialog_id,
                service_dialog_id,
                text,
            )

            session = self.restart_session(service_dialog_id, user_id)
            self.render_current(session)
            return

        # Free-form flow input is accepted only inside the managed service chat.
        if not is_service_chat:
            return

        key = self.session_key(service_dialog_id, user_id)
        session = self.store.get_session(key)

        if session is None:
            # Remove stray user text and show a clean first screen.
            self.delete_chat_message(message_id)
            session = self.restart_session(service_dialog_id, user_id)
            self.render_current(session)
            return

        if session.get("current_screen") == "done":
            self.delete_chat_message(message_id)
            self.render_current(session)
            return

        if session.get("current_screen") in {"document", "edit_attachments_input"} and files:
            result = submit_attachment_message(
                session,
                [int(item["id"]) for item in files],
                [str(item.get("name") or "") for item in files],
                text,
            )
            self.delete_chat_message(message_id)
            self.store.put_session(key, session)
            self.render_current(session)
            return

        result = submit_text(session, text)
        status = result["status"]

        # The bot owns this group chat, so user input is removed immediately
        # after being consumed. The only visible item is the live DeskFlow screen.
        self.delete_chat_message(message_id)

        if status == "buttons_expected":
            self.render_current(session)
            return

        if status == "empty":
            self.render_current(session)
            return

        if status == "validation_error":
            current = view(session)
            message = str(result.get("message") or "").strip()
            text_override = current["text"]
            if message:
                text_override += "\n\n" + message
            self.store.put_session(key, session)
            self.render_current(session, text_override=text_override)
            return

        self.store.put_session(key, session)
        self.render_current(session)

    def handle_command(self, data: dict[str, Any]) -> None:
        command = data.get("command") or {}
        if str(command.get("command") or "").lstrip("/") != self.command_name:
            return

        user = data.get("user") or {}
        chat = data.get("chat") or {}
        user_id = int(user.get("id") or 0)
        dialog_id = str(chat.get("dialogId") or user_id)
        if not user_id or not dialog_id:
            return

        service_dialog_id = self.get_service_chat(user_id)
        if not service_dialog_id or dialog_id != service_dialog_id:
            service_dialog_id = self.ensure_service_chat(
                user_id,
                str(user.get("name") or ""),
            )
            session = self.restart_session(service_dialog_id, user_id)
            self.render_current(session)
            return

        key = self.session_key(dialog_id, user_id)
        session = self.store.get_session(key)
        if session is None:
            session = self.restart_session(dialog_id, user_id)
            self.render_current(session)
            return

        raw_params = str(command.get("params") or "")
        try:
            revision_text, index_text = raw_params.split(":", 1)
            revision = int(revision_text)
            index = int(index_text)
        except (ValueError, TypeError):
            self.render_current(
                session,
                text_override="[b]Ошибочка вышла 🤔[/b]\n[i]Используй кнопки в текущем сообщении — так я пойму, куда идти дальше.[/i]",
            )
            return

        current = view(session)

        if revision != int(session["revision"]):
            # Old keyboards stay visible in chat history, but can never mutate state.
            self.render_current(
                session,
                text_override="[b]Этот экран уже изменился 🙂[/b]\n[i]Используй кнопки в текущем сообщении.[/i]",
            )
            return

        if index < 0 or index >= len(current["buttons"]):
            self.render_current(
                session,
                text_override="[b]Этот экран уже изменился 🙂[/b]\n[i]Используй кнопки в текущем сообщении.[/i]",
            )
            return

        action = current["buttons"][index]["action"]
        transition = submit_action(session, action)

        if transition["status"] == "task_ready":
            task_id = self.create_task(session)
            mark_task_created(session, task_id)
            self.store.put_session(key, session)
            self.render_current(
                session,
                link_button={
                    "text": "Открыть обращение",
                    "link": self.task_link(user_id, task_id),
                },
            )
            return

        if transition["status"] == "restart_requested":
            session = self.restart_session(dialog_id, user_id)
            self.render_current(session)
            return

        self.store.put_session(key, session)
        self.render_current(session)

    def handle_join(self, data: dict[str, Any]) -> None:
        user = data.get("user") or {}
        chat = data.get("chat") or {}
        user_id = int(user.get("id") or 0)
        dialog_id = str(data.get("dialogId") or chat.get("dialogId") or user_id)

        if not user_id or not dialog_id:
            return

        # Group service chats are rendered explicitly by ensure/restart logic.
        # Do not create a second greeting message there.
        if dialog_id.startswith("chat"):
            return

        # The personal bot dialog is only a launcher.
        self.send(
            dialog_id,
            "Напишите «SOS». DeskFlow откроет ваш служебный чат обращения.",
        )

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        event_id = event.get("eventId")
        data = event.get("data") or {}
        LOG.info("Event received: id=%s type=%s", event_id, event_type)

        if event_type == "ONIMBOTV2MESSAGEADD":
            self.handle_message(data)
        elif event_type == "ONIMBOTV2COMMANDADD":
            self.handle_command(data)
        elif event_type == "ONIMBOTV2JOINCHAT":
            self.handle_join(data)

    def run(self) -> None:
        self.register()
        print(f"DeskFlow bot registered: ID={self.bot_id}")
        print("Worker is running. Open the bot in Bitrix24 and send: SOS")

        offset_text = self.store.get_meta("event_offset")
        offset = int(offset_text) if offset_text else None

        while True:
            try:
                params: dict[str, Any] = {
                    "botId": self.bot_id,
                    "botToken": self.bot_token,
                    "limit": 100,
                }
                if offset is not None:
                    params["offset"] = offset

                result = self.call("imbot.v2.Event.get", params)
                if not isinstance(result, dict):
                    raise RuntimeError(f"Event.get вернул неожиданный ответ: {result!r}")

                events = result.get("events") or []

                for event in events:
                    event_id = int(event.get("eventId") or 0)
                    try:
                        self.handle_event(event)
                    except Exception:
                        LOG.exception("Ошибка обработки eventId=%s", event_id)
                        # Keep the failed event in the queue for the next pass.
                        if event_id:
                            offset = event_id
                            self.store.set_meta("event_offset", offset)
                        raise
                    else:
                        if event_id:
                            offset = event_id + 1
                            self.store.set_meta("event_offset", offset)

                next_offset = result.get("nextOffset")
                if events and next_offset is not None:
                    offset = int(next_offset)
                    self.store.set_meta("event_offset", offset)

                if not events:
                    time.sleep(self.event_poll_seconds)

            except KeyboardInterrupt:
                print("\nWorker stopped.")
                return
            except BitrixError as exc:
                LOG.error("Bitrix error: %s", sanitize_error(exc))
                time.sleep(2)
            except Exception as exc:
                LOG.error("Worker error: %s", sanitize_error(exc), exc_info=True)
                time.sleep(2)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not os.getenv("TARGET_BITRIX_WEBHOOK_URL", "").strip():
        print(
            "ERROR: TARGET_BITRIX_WEBHOOK_URL не задан. "
            "Для теста в Bitrix24 нужен входящий webhook с правами imbot и task.",
            file=sys.stderr,
        )
        return 2

    try:
        Runtime().run()
        return 0
    except Exception as exc:
        print(f"ERROR: {sanitize_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
