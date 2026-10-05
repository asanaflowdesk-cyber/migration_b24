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
    normalize_session,
    parse_message_files,
    submit_action,
    submit_attachment_message,
    submit_text,
    task_description,
    task_registry_fields,
    task_title,
    view,
)
from processes.flowdesk_chatbot.polling import poll_interval_seconds
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
        # Cache the last text-field state per dialog. Most DeskFlow screens are
        # button-only; repeating the same Bitrix UI toggle on every click costs
        # one extra REST roundtrip and one throttle slot for no visible benefit.
        self._text_field_state: dict[str, bool] = {}
        # Daytime: near-realtime polling. Night: low-frequency background check.
        # FLOWDESK_EVENT_POLL_SECONDS remains a backward-compatible daytime fallback.
        self.active_poll_seconds = float(
            os.getenv(
                "FLOWDESK_ACTIVE_POLL_SECONDS",
                os.getenv("FLOWDESK_EVENT_POLL_SECONDS", "1.0"),
            )
        )
        self.quiet_poll_seconds = float(
            os.getenv("FLOWDESK_QUIET_POLL_SECONDS", "60.0")
        )
        self.event_retry_attempts = max(
            1,
            int(os.getenv("FLOWDESK_EVENT_RETRY_ATTEMPTS", "3")),
        )
        self.event_retry_delay = max(
            0.0,
            float(os.getenv("FLOWDESK_EVENT_RETRY_DELAY", "0.75")),
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

        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = []
            for key in ("items", "fields", "result"):
                value = result.get(key)
                if isinstance(value, list):
                    rows = value
                    break
        else:
            rows = []

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
        desired = bool(enabled)
        if self._text_field_state.get(dialog_id) is desired:
            return True

        # Some on-premise Bitrix24 builds may not expose this newer UI method.
        # Text-field control is cosmetic; it must never block the business flow.
        try:
            result = self.call(
                "imbot.v2.Chat.TextField.enabled",
                {
                    "botId": self.bot_id,
                    "botToken": self.bot_token,
                    "dialogId": dialog_id,
                    "enabled": desired,
                },
            )
        except Exception as exc:
            self._text_field_state.pop(dialog_id, None)
            LOG.warning(
                "Text-field toggle skipped for dialog=%s enabled=%s: %s",
                dialog_id,
                desired,
                sanitize_error(exc),
            )
            return False

        ok = result is True or (
            isinstance(result, dict) and result.get("result") is True
        )
        if not ok:
            self._text_field_state.pop(dialog_id, None)
            LOG.warning(
                "Text-field toggle returned unexpected result for dialog=%s: %r",
                dialog_id,
                result,
            )
            return False

        self._text_field_state[dialog_id] = desired
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

    def _query_flowdesk_tasks(
        self,
        task_filter: dict[str, Any],
        needed: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0

        while len(rows) < needed:
            result = self.call(
                "tasks.task.list",
                {
                    "order": {"ID": "desc"},
                    "filter": task_filter,
                    "select": [
                        "ID",
                        "TITLE",
                        "XML_ID",
                        "CREATED_DATE",
                        "UF_FLOWDESK_INITIATOR_ID",
                    ],
                    "start": start,
                },
            )
            if isinstance(result, dict):
                page = result.get("tasks") or result.get("items") or []
            elif isinstance(result, list):
                page = result
            else:
                page = []

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

    @staticmethod
    def _merge_task_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for group in groups:
            for row in group:
                task_id = str(row.get("id") or row.get("ID") or "").strip()
                if task_id:
                    merged[task_id] = row

        def sort_key(row: dict[str, Any]) -> tuple[int, str]:
            raw_id = str(row.get("id") or row.get("ID") or "")
            try:
                numeric = int(raw_id)
            except ValueError:
                numeric = 0
            created = str(row.get("createdDate") or row.get("CREATED_DATE") or "")
            return numeric, created

        return sorted(merged.values(), key=sort_key, reverse=True)

    def recent_flowdesk_tasks(
        self,
        user_id: int,
        needed: int,
    ) -> list[dict[str, Any]]:
        xml_filter = {"%XML_ID": "FLOWDESK_CHAT_"}

        # New tasks use the dedicated initiator field. This keeps history correct
        # even if CREATED_BY is later switched to a service account.
        initiator_rows: list[dict[str, Any]] = []
        if "UF_FLOWDESK_INITIATOR_ID" in self.task_user_fields:
            try:
                initiator_rows = self._query_flowdesk_tasks(
                    {
                        **xml_filter,
                        "UF_FLOWDESK_INITIATOR_ID": str(user_id),
                    },
                    needed,
                )
            except Exception as exc:
                LOG.warning(
                    "DeskFlow initiator history query failed: %s",
                    sanitize_error(exc),
                )

        if len(initiator_rows) >= needed:
            return initiator_rows[:needed]

        # Compatibility path keeps old tasks created before the custom field and
        # boxes that reject custom-field filtering visible in history.
        try:
            created_rows = self._query_flowdesk_tasks(
                {
                    **xml_filter,
                    "CREATED_BY": int(user_id),
                },
                needed,
            )
            merged = self._merge_task_rows(initiator_rows, created_rows)
            if len(merged) >= needed or merged:
                return merged[:needed]
        except Exception as exc:
            LOG.warning(
                "Filtered DeskFlow history query failed; using compatibility scan: %s",
                sanitize_error(exc),
            )

        # Last-resort scan for older on-premise builds that reject %XML_ID.
        rows: list[dict[str, Any]] = []
        start = 0
        for _ in range(4):
            result = self.call(
                "tasks.task.list",
                {
                    "order": {"ID": "desc"},
                    "filter": {"CREATED_BY": int(user_id)},
                    "select": ["ID", "TITLE", "XML_ID", "CREATED_DATE"],
                    "start": start,
                },
            )
            if isinstance(result, dict):
                page = result.get("tasks") or result.get("items") or []
            elif isinstance(result, list):
                page = result
            else:
                page = []

            if not isinstance(page, list) or not page:
                break

            for row in page:
                if not isinstance(row, dict):
                    continue
                xml_id = str(row.get("xmlId") or row.get("XML_ID") or "")
                if xml_id.startswith("FLOWDESK_CHAT_"):
                    rows.append(row)
                    if len(rows) >= needed:
                        return self._merge_task_rows(initiator_rows, rows)[:needed]

            if len(page) < 50:
                break
            start += 50

        return self._merge_task_rows(initiator_rows, rows)[:needed]

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
            # Only "Запрос" has a department in the title's second slot.
            # Direct branch-3 appeals have no department; their next title part
            # can be an insurance class and must not be mislabeled as a department.
            target = (
                parts[1]
                if request_type == "Запрос" and len(parts) > 1
                else ""
            )
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

        # Keep the final direct-link button on every final re-render, including
        # after "Показать ещё".
        if (
            link_button is None
            and current["screen"] == "done"
            and session.get("task_id")
        ):
            link_button = {
                "text": "Открыть обращение",
                "link": self.task_link(
                    int(session["user_id"]),
                    str(session["task_id"]),
                ),
            }

        # Global UX rule: text is available only on screens that explicitly
        # accept text/files. Button-only screens, start and final included, are locked.
        self.set_text_field(
            session["dialog_id"],
            bool(current["accepts_text"]),
        )

        message_id = session.get("active_message_id")
        if message_id:
            try:
                self.update_message(
                    int(message_id),
                    text,
                    session=session,
                    link_button=link_button,
                )
            except BitrixError as exc:
                code = str(exc.code or "").upper()
                description = str(exc.description or "").casefold()
                message_missing = (
                    "NOT_FOUND" in code
                    or "MESSAGE_ID" in code
                    or "message not found" in description
                    or "сообщ" in description and "не найден" in description
                )
                if not message_missing:
                    raise

                LOG.warning(
                    "Active DeskFlow message %s is gone; creating a replacement",
                    message_id,
                )
                message_id = self.send(
                    session["dialog_id"],
                    text,
                    session=session,
                    link_button=link_button,
                )
                session["active_message_id"] = int(message_id)
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
        return parse_message_files(message)

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
        if files:
            LOG.info(
                "Attachment message detected: message=%s file_ids=%s",
                message_id,
                [item["id"] for item in files],
            )
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
        if session is not None:
            session = normalize_session(
                session,
                user_id=user_id,
                dialog_id=service_dialog_id,
            )

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
        if session is not None:
            session = normalize_session(
                session,
                user_id=user_id,
                dialog_id=dialog_id,
            )
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

        if event_type in {
            "ONIMBOTV2MESSAGEADD",
            "ONIMBOTV2COMMANDADD",
            "ONIMBOTV2JOINCHAT",
        }:
            LOG.info("Event received: id=%s type=%s", event_id, event_type)
        else:
            LOG.debug("Ignored event: id=%s type=%s", event_id, event_type)

        if event_type == "ONIMBOTV2MESSAGEADD":
            self.handle_message(data)
        elif event_type == "ONIMBOTV2COMMANDADD":
            self.handle_command(data)
        elif event_type == "ONIMBOTV2JOINCHAT":
            self.handle_join(data)

    def process_event_with_retry(self, event: dict[str, Any]) -> bool:
        event_id = int(event.get("eventId") or 0)
        event_type = str(event.get("type") or "")

        for attempt in range(1, self.event_retry_attempts + 1):
            started = time.monotonic()
            try:
                self.handle_event(event)
            except Exception as exc:
                if attempt >= self.event_retry_attempts:
                    LOG.exception(
                        "Event permanently failed and will be skipped: "
                        "id=%s type=%s attempts=%s",
                        event_id,
                        event_type,
                        attempt,
                    )
                    return False

                wait = min(5.0, self.event_retry_delay * attempt)
                LOG.warning(
                    "Event failed, retrying: id=%s type=%s attempt=%s/%s "
                    "wait=%.2fs error=%s",
                    event_id,
                    event_type,
                    attempt,
                    self.event_retry_attempts,
                    wait,
                    sanitize_error(exc),
                )
                if wait:
                    time.sleep(wait)
                continue

            duration = time.monotonic() - started
            if duration >= 2.0:
                LOG.warning(
                    "Slow event handling: id=%s type=%s duration=%.2fs",
                    event_id,
                    event_type,
                    duration,
                )
            return True

        return False

    def run(self) -> None:
        self.register()
        print(f"DeskFlow bot registered: ID={self.bot_id}")
        print(
            "Polling: 08:00-22:00 Kazakhstan every "
            f"{self.active_poll_seconds:g}s; 22:01-07:59 every "
            f"{self.quiet_poll_seconds:g}s"
        )
        print("Worker is running. Open the bot in Bitrix24 and send: SOS")

        offset_text = self.store.get_meta("event_offset")
        try:
            offset = int(offset_text) if offset_text else None
        except (TypeError, ValueError):
            LOG.warning(
                "Invalid stored event_offset=%r; requesting a fresh offset",
                offset_text,
            )
            offset = None

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
                    ok = self.process_event_with_retry(event)
                    if not ok:
                        LOG.error(
                            "DeskFlow skipped poison event id=%s so later events can continue",
                            event_id,
                        )

                    if event_id:
                        offset = event_id + 1
                        self.store.set_meta("event_offset", offset)

                next_offset = result.get("nextOffset")
                if events and next_offset is not None:
                    offset = int(next_offset)
                    self.store.set_meta("event_offset", offset)

                if not events:
                    time.sleep(
                        poll_interval_seconds(
                            active_seconds=self.active_poll_seconds,
                            quiet_seconds=self.quiet_poll_seconds,
                        )
                    )

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
