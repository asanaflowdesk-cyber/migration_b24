from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from common.bitrix import BitrixClient, BitrixError, sanitize_error
from processes.flowdesk_chatbot.avatar_asset import SERVICE_CHAT_AVATAR_B64
from processes.flowdesk_chatbot import constructor_engine as graph, initiatives
from processes.flowdesk_chatbot.initiator_profile import fetch_profile
from processes.flowdesk_chatbot.internal_admin import start_editor
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
from processes.flowdesk_chatbot.field_templates import render_template
from processes.flowdesk_chatbot.flow_store import FlowStore, step_label, worker_lock

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
TASK_USER_FIELDS.update(initiatives.TASK_FIELDS)


class Runtime:
    def __init__(self) -> None:
        self.client = BitrixClient.from_env()
        self.db_path = os.getenv(
            "FLOWDESK_CHATBOT_DB",
            "processes/flowdesk_chatbot/flowdesk_state.sqlite3",
        )
        self.store = FlowStore(self.db_path)
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
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Укажите конечный HTTPS-адрес входящего вебхука Б24")
        self.task_user_fields: set[str] = set()
        self.task_field_catalog: dict[str, str] = {}
        self.service_chat_avatar_url = os.getenv(
            "FLOWDESK_SERVICE_CHAT_AVATAR_URL",
            "https://bitrix.theeurasia.kz/picture/K2blnYG5Z7Cm0teajIEf",
        ).strip()
        self._service_chat_avatar_b64: str | None = None
        self._service_chat_avatar_version: str | None = None
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

        if bot.get("type") and bot["type"] != desired_type:
            raise RuntimeError("Код DeskFlow уже занят ботом другого типа. Укажите код персонального бота.")

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
            self.task_field_catalog = {}
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

        catalog = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("FIELD_NAME") or row.get("fieldName") or "")
            kind = str(row.get("USER_TYPE_ID") or row.get("userTypeId") or "").lower()
            if name.startswith("UF_") and kind == "string" and row.get("MULTIPLE", row.get("multiple", "N")) in ("N", 0, False):
                label = row.get("EDIT_FORM_LABEL") or row.get("editFormLabel") or row.get("LABEL")
                if isinstance(label, dict):
                    label = label.get("ru") or label.get("en")
                catalog[name] = TASK_USER_FIELDS.get(name, {}).get("label") or (label if isinstance(label, str) else name)
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
            catalog[field_name] = spec["label"]
            LOG.info("Created task user field %s", field_name)

        self.task_user_fields = existing
        self.task_field_catalog = catalog

    @staticmethod
    def session_key(dialog_id: str, user_id: int) -> str:
        return f"{dialog_id}:{user_id}"

    def load_service_chat_avatar(self) -> tuple[str, str] | None:
        """Return a stable Base64 avatar for service chats.

        Prefer the bundled approved avatar so the worker does not depend on a
        browser-authenticated /picture/... URL. The configured URL is kept only
        as a fallback for future replacements.
        """
        if self._service_chat_avatar_b64 and self._service_chat_avatar_version:
            return (
                self._service_chat_avatar_b64,
                self._service_chat_avatar_version,
            )

        bundled = str(SERVICE_CHAT_AVATAR_B64 or "").strip()
        if bundled:
            try:
                content = base64.b64decode(bundled, validate=True)
            except Exception as exc:
                LOG.error(
                    "Bundled DeskFlow avatar is invalid Base64: %s",
                    sanitize_error(exc),
                )
            else:
                if content:
                    version = hashlib.sha256(content).hexdigest()[:16]
                    self._service_chat_avatar_b64 = bundled
                    self._service_chat_avatar_version = version
                    return bundled, version

        if not self.service_chat_avatar_url:
            return None

        try:
            response = requests.get(
                self.service_chat_avatar_url,
                timeout=20,
                allow_redirects=True,
                headers={"User-Agent": "DeskFlow/1.0"},
            )
            response.raise_for_status()
        except Exception as exc:
            LOG.warning(
                "Service chat avatar fallback could not be loaded from %s: %s",
                self.service_chat_avatar_url,
                sanitize_error(exc),
            )
            return None

        content = bytes(response.content or b"")
        content_type = str(response.headers.get("Content-Type") or "").casefold()
        if not content:
            LOG.warning("Service chat avatar response was empty")
            return None

        if content_type and not content_type.startswith("image/"):
            LOG.warning(
                "Service chat avatar URL returned non-image content-type=%s",
                content_type,
            )
            return None

        avatar_b64 = base64.b64encode(content).decode("ascii")
        version = hashlib.sha256(content).hexdigest()[:16]
        self._service_chat_avatar_b64 = avatar_b64
        self._service_chat_avatar_version = version
        return avatar_b64, version

    @staticmethod
    def service_chat_avatar_meta_key(dialog_id: str) -> str:
        return f"service_chat_avatar:{dialog_id}"

    def ensure_service_chat_avatar(
        self,
        dialog_id: str,
        *,
        force: bool = False,
    ) -> None:
        avatar = self.load_service_chat_avatar()
        if avatar is None:
            return

        avatar_b64, version = avatar
        meta_key = self.service_chat_avatar_meta_key(dialog_id)
        if not force and self.store.get_meta(meta_key).strip() == version:
            return

        result = self.call(
            "imbot.v2.Chat.update",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "dialogId": dialog_id,
                "fields": {
                    "avatar": avatar_b64,
                },
            },
        )

        ok = result is True or (
            isinstance(result, dict) and result.get("result") is True
        )
        if not ok:
            raise RuntimeError(
                f"Chat.update did not confirm avatar update: {result!r}"
            )

        self.store.set_meta(meta_key, version)
        LOG.info(
            "Service chat avatar applied: dialog=%s version=%s",
            dialog_id,
            version,
        )

    @staticmethod
    def service_chat_meta_key(user_id: int) -> str:
        return f"service_chat:{int(user_id)}"

    def get_service_chat(self, user_id: int) -> str:
        return self.store.get_meta(self.service_chat_meta_key(user_id)).strip()

    @staticmethod
    def chat_user_rows(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        if isinstance(result, dict):
            for key in ("users", "items", "result"):
                value = result.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

    def service_chat_has_user(self, dialog_id: str, user_id: int) -> bool:
        result = self.call(
            "imbot.v2.Chat.User.list",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "dialogId": dialog_id,
                "limit": 200,
            },
        )
        return any(
            int(row.get("id") or 0) == int(user_id)
            for row in self.chat_user_rows(result)
        )

    def ensure_service_chat(self, user_id: int, user_name: str = "") -> str:
        existing = self.get_service_chat(user_id)
        resolved_user_name = (user_name or "").strip()

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
                chat_exists = (
                    isinstance(chat, dict)
                    and str(chat.get("dialogId") or "") == existing
                )
                if chat_exists:
                    if not resolved_user_name:
                        old_name = str(
                            chat.get("name")
                            or chat.get("title")
                            or ""
                        ).strip()
                        prefix = "DeskFlow — "
                        if old_name.startswith(prefix):
                            resolved_user_name = old_name[len(prefix):].strip()

                    try:
                        member_ok = self.service_chat_has_user(existing, user_id)
                    except Exception as exc:
                        member_ok = False
                        LOG.warning(
                            "Saved service chat membership check failed "
                            "for user=%s chat=%s: %s",
                            user_id,
                            existing,
                            sanitize_error(exc),
                        )

                    if member_ok:
                        try:
                            self.ensure_service_chat_avatar(existing)
                        except Exception as exc:
                            LOG.warning(
                                "Existing service chat avatar update failed for %s: %s",
                                existing,
                                sanitize_error(exc),
                            )
                        return existing

                    # Important: after a user explicitly deletes/leaves a chat,
                    # Bitrix may keep the chat object but the old dialog remains
                    # unavailable in the UI. Re-adding membership is therefore
                    # not enough. Abandon the stale chat and create a fresh one.
                    LOG.warning(
                        "Saved service chat %s no longer contains user=%s; "
                        "creating a fresh replacement instead of reusing it",
                        existing,
                        user_id,
                    )
            except Exception as exc:
                LOG.warning(
                    "Saved service chat %s is unavailable for user=%s: %s",
                    existing,
                    user_id,
                    sanitize_error(exc),
                )

        title_name = resolved_user_name or f"ID {user_id}"
        chat_fields: dict[str, Any] = {
            "title": f"DeskFlow — {title_name}",
            "description": "Служебный чат для внутренних обращений DeskFlow",
            "userIds": [int(user_id)],
        }

        avatar = self.load_service_chat_avatar()
        avatar_version = None
        if avatar is not None:
            avatar_b64, avatar_version = avatar
            chat_fields["avatar"] = avatar_b64

        result = self.call(
            "imbot.v2.Chat.add",
            {
                "botId": self.bot_id,
                "botToken": self.bot_token,
                "fields": chat_fields,
            },
        )

        chat = result.get("chat") if isinstance(result, dict) else None
        dialog_id = str(chat.get("dialogId") or "") if isinstance(chat, dict) else ""
        if not dialog_id.startswith("chat"):
            raise RuntimeError(f"Chat.add не вернул dialogId группового чата: {result!r}")

        self.store.set_meta(self.service_chat_meta_key(user_id), dialog_id)
        if existing and existing != dialog_id:
            self.store.delete_session(self.session_key(existing, user_id))
            self._text_field_state.pop(existing, None)
            LOG.info(
                "Service chat replaced for user=%s: %s -> %s",
                user_id,
                existing,
                dialog_id,
            )

        # Some on-premise builds accept avatar in Chat.add but still return the
        # default initials icon. Force one explicit update after creation and
        # only then mark the avatar version as applied.
        if avatar_version:
            try:
                self.ensure_service_chat_avatar(dialog_id, force=True)
            except Exception as exc:
                LOG.warning(
                    "New service chat avatar update failed for %s: %s",
                    dialog_id,
                    sanitize_error(exc),
                )

        LOG.info("Service chat created for user=%s: %s", user_id, dialog_id)
        return dialog_id

    @staticmethod
    def launcher_message_meta_key(user_id: int) -> str:
        return f"launcher_message:{int(user_id)}"

    @staticmethod
    def private_launcher_meta_key(user_id: int) -> str:
        return f"private_launcher_message:{int(user_id)}"

    def service_chat_link(self, dialog_id: str) -> str:
        path = f"/online/?IM_DIALOG={dialog_id}"
        return f"{self.portal_base}{path}" if self.portal_base else path

    def add_private_editor_button(self, fields: dict[str, Any], user_id: int | None) -> None:
        editor = getattr(self, "internal_editor", None)
        if not editor or not user_id:
            return
        try:
            role = editor.store.role(user_id)
        except sqlite3.Error:
            LOG.warning("DeskFlow editor access is temporarily unavailable")
            return
        if role:
            fields["keyboard"]["BUTTONS"].append({
                "TEXT": "Управление DeskFlow",
                "LINK": editor.entry_url,
                "BLOCK": "N",
                "DISPLAY": "LINE",
                "BG_COLOR_TOKEN": "base",
            })

    def published_setting(self, group, key, fallback, user_id=None):
        editor = getattr(self, "internal_editor", None)
        _, doc = editor.store.active() if editor else (None, None)
        value = doc["settings"].get(group, {}).get(key, fallback) if doc else fallback
        return render_template(value, {"system.user_id": user_id or ""}, chat=True)

    def launcher_message_fields(self, private_user_id: int | None = None) -> dict[str, Any]:
        fields = {
            "message": self.published_setting("messages", "launcher", (
                "[b]DeskFlow готов к работе 🙂[/b]\n"
                "Нажми кнопку ниже — я проверю служебный чат и подготовлю его к работе."
            ), private_user_id),
            "keyboard": {
                "BOT_ID": self.bot_id,
                "BUTTONS": [
                    {
                        "TEXT": self.published_setting("labels", "launcher_open", "Открыть служебный чат", private_user_id)[:250],
                        "COMMAND": f"/{self.command_name}",
                        "COMMAND_PARAMS": "launcher",
                        "BLOCK": "N",
                        "DISPLAY": "LINE",
                        "BG_COLOR_TOKEN": "primary",
                        "TEXT_COLOR": "#FFFFFF",
                    }
                ],
            },
        }
        self.add_private_editor_button(fields, private_user_id)
        return fields

    def launcher_ready_fields(self, service_dialog_id: str) -> dict[str, Any]:
        return {
            "message": self.published_setting("messages", "launcher_ready", "[b]Готово 🙂[/b]\nСлужебный чат проверен и готов к работе."),
            "keyboard": {
                "BOT_ID": self.bot_id,
                "BUTTONS": [
                    {
                        "TEXT": self.published_setting("labels", "launcher_ready", "Перейти в служебный чат")[:250],
                        "ACTION": "DIALOG",
                        "ACTION_VALUE": service_dialog_id,
                        "BLOCK": "N",
                        "DISPLAY": "LINE",
                        "BG_COLOR_TOKEN": "primary",
                        "TEXT_COLOR": "#FFFFFF",
                    }
                ],
            },
        }

    def ensure_launcher_message(self, user_id: int, dialog_id: str) -> int:
        """Keep one reusable on-demand launcher in the personal bot dialog."""
        meta_key = self.launcher_message_meta_key(user_id)
        existing_text = self.store.get_meta(meta_key).strip()
        private_key = self.private_launcher_meta_key(user_id)
        verified_message = self.store.get_meta(private_key).strip()
        is_private = dialog_id == str(user_id)
        fields = self.launcher_message_fields(user_id if is_private else None)
        if is_private and len(fields["keyboard"]["BUTTONS"]) > 1:
            # An older launcher ID alone does not prove which chat owns it.
            existing_text = verified_message
        elif not is_private and existing_text == verified_message:
            existing_text = ""

        if existing_text:
            try:
                message_id = int(existing_text)
                result = self.call(
                    "imbot.v2.Chat.Message.update",
                    {
                        "botId": self.bot_id,
                        "botToken": self.bot_token,
                        "messageId": message_id,
                        "fields": fields,
                    },
                )
                if isinstance(result, dict):
                    return message_id
            except Exception as exc:
                LOG.warning(
                    "Launcher message %s could not be refreshed for user=%s: %s",
                    existing_text,
                    user_id,
                    sanitize_error(exc),
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
            raise RuntimeError(
                f"Launcher Message.send не вернул id: {result!r}"
            )

        message_id = int(result["id"])
        if is_private:
            self.store.set_meta(private_key, message_id)
        if is_private or not verified_message:
            self.store.set_meta(meta_key, message_id)
        LOG.info(
            "Launcher button message created for user=%s message=%s",
            user_id,
            message_id,
        )
        return message_id

    def refresh_editor_access(self) -> None:
        editor = getattr(self, "internal_editor", None)
        if not editor or time.monotonic() < self.next_editor_access_check:
            return
        self.next_editor_access_check = time.monotonic() + 2
        try:
            changes = editor.store.access_changes(self.last_editor_access_change)
        except sqlite3.Error:
            LOG.warning("DeskFlow editor access changes are temporarily unavailable")
            return
        for seq, user_id in changes:
            self.last_editor_access_change = seq
            message_id = self.store.get_meta(self.private_launcher_meta_key(user_id)).strip()
            if not message_id.isdigit():
                continue
            try:
                self.call("imbot.v2.Chat.Message.update", {
                    "botId": self.bot_id,
                    "botToken": self.bot_token,
                    "messageId": int(message_id),
                    "fields": self.launcher_message_fields(user_id),
                })
            except Exception as exc:
                LOG.warning("Private DeskFlow launcher could not be updated: %s", sanitize_error(exc))

    def refresh_known_launchers(self) -> None:
        """Convert saved launchers to the on-demand command without creating chats."""
        try:
            entries = self.store.list_meta("launcher_message:")
        except Exception as exc:
            LOG.warning(
                "Could not enumerate DeskFlow launcher messages: %s",
                sanitize_error(exc),
            )
            return

        for key, message_text in entries.items():
            try:
                user_id = int(key.split(":", 1)[1])
                message_id = int(message_text)
            except (IndexError, TypeError, ValueError):
                continue

            known_private = self.store.get_meta(self.private_launcher_meta_key(user_id)).strip()
            private_user_id = user_id if known_private == str(message_id) else None
            try:
                result = self.call(
                    "imbot.v2.Chat.Message.update",
                    {
                        "botId": self.bot_id,
                        "botToken": self.bot_token,
                        "messageId": message_id,
                        "fields": self.launcher_message_fields(private_user_id),
                    },
                )
                if not isinstance(result, dict):
                    raise RuntimeError(
                        f"Launcher update returned unexpected result: {result!r}"
                    )
                LOG.info(
                    "Launcher converted to on-demand command: user=%s",
                    user_id,
                )
            except Exception as exc:
                LOG.warning(
                    "Launcher migration failed for user=%s message=%s: %s",
                    user_id,
                    message_id,
                    sanitize_error(exc),
                )

    def answer_launcher_command(
        self,
        data: dict[str, Any],
        service_dialog_id: str,
    ) -> None:
        command = data.get("command") or {}
        message = data.get("message") or {}
        chat = data.get("chat") or {}

        command_id = int(command.get("id") or 0)
        message_id = int(message.get("id") or 0)
        dialog_id = str(chat.get("dialogId") or "")

        if not command_id or not message_id or not dialog_id:
            raise ValueError(
                "Неполные данные launcher-команды: "
                f"command_id={command_id}, message_id={message_id}, "
                f"dialog_id={dialog_id!r}"
            )

        fields = self.launcher_ready_fields(service_dialog_id)
        user_id = int((data.get("user") or {}).get("id") or 0)
        if user_id and dialog_id == str(user_id):
            known_private = self.store.get_meta(self.private_launcher_meta_key(user_id)).strip()
            if known_private == str(message_id):
                self.add_private_editor_button(fields, user_id)

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
            raise RuntimeError(
                f"Launcher Command.answer вернул неожиданный ответ: {result!r}"
            )

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

            for source, target in (("bg_color", "BG_COLOR"), ("text_color", "TEXT_COLOR"), ("off_bg_color", "OFF_BG_COLOR"),
                                   ("off_text_color", "OFF_TEXT_COLOR"), ("width", "WIDTH"), ("display", "DISPLAY")):
                if item.get(source) not in (None, ""):
                    button[target] = item[source]
            if "BG_COLOR" in button:
                button.pop("BG_COLOR_TOKEN", None)
            if item.get("link"):
                editor = getattr(self, "internal_editor", None)
                resources = getattr(getattr(editor, "application", None), "resources", None)
                if resources is None:
                    button.update(DISABLED="Y")
                else:
                    button["LINK"] = resources.issue(session, item["option_id"])
                button.pop("COMMAND", None)
                button.pop("COMMAND_PARAMS", None)
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

    def initiative_draft(self, user_id: int) -> dict[str, Any] | None:
        raw = self.store.get_meta(f"initiative_draft:{int(user_id)}")
        if not raw:
            return None
        try:
            draft = json.loads(raw)
            if (
                not isinstance(draft, dict)
                or int(draft.get("user_id") or 0) != int(user_id)
                or draft.get("current_screen") != "initiative_saved"
                or not initiatives.is_initiative(draft)
            ):
                return None
            return draft
        except (ValueError, TypeError):
            LOG.warning("Invalid initiative draft for user=%s", user_id)
            return None

    def prepare_initiative(self, session: dict[str, Any]) -> None:
        if not session.get("current_screen", "").startswith("initiative_"):
            return
        ini = initiatives.state(session)
        user_id = int(session["user_id"])
        if not ini["draft_checked"]:
            ini["draft_checked"] = True
            draft = self.initiative_draft(user_id)
            if draft and draft.get("request_id") != session.get("request_id"):
                session["current_screen"] = "initiative_existing_draft"
                session["revision"] = int(session["revision"]) + 1
        if session["current_screen"] == "initiative_existing_draft" or ini["profile_loaded"]:
            return

        cached: dict[str, Any] = {}
        raw = self.store.get_meta(f"initiative_profile:{user_id}")
        try:
            parsed = json.loads(raw) if raw else {}
            cached = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            pass
        previous = cached.get("profile") or {}
        previous_source = cached.get("source") or {}
        if not isinstance(previous, dict):
            previous = {}
        if not isinstance(previous_source, dict):
            previous_source = {}
        try:
            profile = fetch_profile(self.call, user_id)
            ini["profile_source"] = dict(profile)
            # Reuse corrections only while the corresponding profile source
            # remains unchanged. Employee transfers/renames are picked up.
            ini["profile"] = {
                key: previous.get(key, value) if previous_source.get(key, "") == value else value
                for key, value in profile.items()
            }
        except Exception as exc:
            LOG.warning("Initiator profile could not be read for user=%s: %s", user_id, sanitize_error(exc))
            ini["profile"] = dict(previous)
            ini["profile_source"] = dict(previous_source)
        ini["profile_loaded"] = True

    def persist_initiative(self, session: dict[str, Any], *, draft: bool = False) -> None:
        if not initiatives.is_initiative(session):
            return
        ini = initiatives.state(session)
        user_id = int(session["user_id"])
        if ini["profile_confirmed"] and not initiatives.profile_missing(session):
            self.store.set_meta(
                f"initiative_profile:{user_id}",
                json.dumps({"profile": ini["profile"], "source": ini.get("profile_source") or {}}, ensure_ascii=False),
            )
        if draft:
            self.store.set_meta(f"initiative_draft:{user_id}", json.dumps(session, ensure_ascii=False))

    def resume_initiative_draft(self, session: dict[str, Any]) -> dict[str, Any]:
        saved = self.initiative_draft(int(session["user_id"]))
        if saved is None:
            session["current_screen"] = "initiative_profile"
            session["revision"] = int(session["revision"]) + 1
            return session
        saved = normalize_session(saved)
        saved["dialog_id"] = session["dialog_id"]
        saved["active_message_id"] = session.get("active_message_id")
        saved["revision"] = max(int(saved["revision"]), int(session["revision"])) + 1
        initiatives.state(saved)["draft_checked"] = True
        initiatives.submit_action(saved, initiatives.RESUME)
        return saved

    def clear_submitted_draft(self, session: dict[str, Any]) -> None:
        if not initiatives.is_initiative(session):
            return
        draft = self.initiative_draft(int(session["user_id"]))
        if draft and draft.get("request_id") == session.get("request_id"):
            self.store.set_meta(f"initiative_draft:{int(session['user_id'])}", "")

    def render_current(
        self,
        session: dict[str, Any],
        *,
        link_button: dict[str, str] | None = None,
        text_override: str | None = None,
    ) -> None:
        normalize_session(session)
        self.sync_delivery(session)
        self.prepare_graph_profile(session)
        self.prepare_initiative(session)
        self.persist_initiative(session)
        history = ""
        state = session.get("constructor")
        home = state and state["node"] == state["document"]["start"]
        if (not state or state["document"]["settings"].get("show_history", True)) and (home or session.get("current_screen") in {"type", "done"}) and not session.get("edit_mode"):
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
        if isinstance(self.store, FlowStore):
            self.store.shown(key, session, text)
        else:
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
            rows = result.get("tasks", result.get("items"))
        else:
            rows = result
        if not isinstance(rows, list):
            raise ValueError("Б24 не подтвердил результат поиска задачи")
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or str(row.get("xmlId") or row.get("XML_ID") or "") != xml_id:
                raise ValueError("Б24 вернул несвязанные данные поиска задачи")
            task = str(row.get("id") or row.get("ID") or "")
            if not task.isascii() or not task.isdigit() or int(task) < 1:
                raise ValueError("Б24 не подтвердил номер задачи")
            ids.add(task)
        if len(ids) > 1:
            raise ValueError("Найдено несколько задач для одного обращения")
        return next(iter(ids), "")

    def create_task(self, session: dict[str, Any]) -> str:
        existing = self.find_existing_task(f"FLOWDESK_CHAT_{session['request_id']}")
        if existing:
            return existing
        task = self.add_task(self.task_fields(session))
        self.send_attachment_comment(session, task)
        return task

    def task_fields(self, session):
        user_id = int(session["user_id"])
        responsible_id = int(os.getenv("FLOWDESK_DEFAULT_RESPONSIBLE_ID", str(user_id)))
        project_id = int(os.getenv("FLOWDESK_PROJECT_ID", "0"))
        xml_id = f"FLOWDESK_CHAT_{session['request_id']}"
        state = session.get("constructor")
        if state:
            output = state["document"]["settings"].get("output", {})
            responsible_id = output.get("responsible_id", responsible_id)
            project_id = output.get("project_id", project_id)

        fields: dict[str, Any] = {
            "TITLE": task_title(session),
            "DESCRIPTION": task_description(session),
            "CREATED_BY": user_id,
            "RESPONSIBLE_ID": responsible_id,
            "XML_ID": xml_id,
        }
        if project_id > 0:
            fields["GROUP_ID"] = project_id

        registry = task_registry_fields(session)
        if state:
            mapping = graph.task_mapping(session)
            requested = {key for key, value in registry.items() if value} | {key for key in mapping if key.startswith("UF_")}
            missing = requested - self.task_field_catalog.keys()
            if missing:
                raise ValueError("Поля задачи недоступны для записи: " + ", ".join(sorted(missing)))
        for field_name, value in registry.items():
            if field_name in self.task_user_fields and value:
                fields[field_name] = value
        if state:
            fields.update(mapping)
            for key in ("TITLE", "DESCRIPTION"):
                fields[key] = fields[key].replace("[", "［").replace("]", "］")

        file_ids = [
            int(file_id)
            for file_id in (session["data"].get("document_file_ids") or [])
            if str(file_id).isdigit() and int(file_id) > 0
        ]
        if file_ids:
            fields["UF_TASK_WEBDAV_FILES"] = [
                f"n{file_id}" for file_id in file_ids
            ]

        return fields

    def add_task(self, fields):
        result = self.call("tasks.task.add", {"fields": fields})
        task = result.get("task") if isinstance(result, dict) else None
        task_id = str(task.get("id") or "") if isinstance(task, dict) else ""
        if not task_id.isascii() or not task_id.isdigit() or int(task_id) <= 0:
            raise RuntimeError("Б24 не подтвердил номер задачи")
        return task_id

    def sync_delivery(self, session):
        if not isinstance(self.store, FlowStore):
            return
        state, task, error = self.store.delivery(session["request_id"])
        if task and not session.get("task_id"):
            self.clear_submitted_draft(session)
            mark_task_created(session, task)
        if state:
            session.update(delivery_locked=True, delivery_status="failed" if state in {"lookup", "rejected"} else state, delivery_error=error)

    def deliver_task(self, session, event=None, *, check_only=False):
        if not isinstance(self.store, FlowStore):
            return self.create_task(session)
        rid = session["request_id"]
        session["delivery_locked"] = True
        session["revision"] += 1
        key = self.session_key(session["dialog_id"], session["user_id"])
        state, task, confirmation = self.store.begin_delivery(key, session, event)
        if task:
            return task
        ambiguous = state in {"sending", "unknown"}
        try:
            task = self.find_existing_task(f"FLOWDESK_CHAT_{rid}")
        except Exception:
            self.store.delivery_result(rid, "unknown" if ambiguous else "rejected", error="Б24 пока не подтвердил результат поиска задачи.")
            return ""
        if task:
            self.store.delivery_result(rid, "created", task=task)
            return task
        if ambiguous or check_only:
            self.store.delivery_result(rid, "unknown" if ambiguous else "rejected", error="Задача пока не найдена. Ответы сохранены.")
            return ""
        try:
            fields = self.task_fields(confirmation)
        except ValueError as exc:
            self.store.delivery_result(rid, "rejected", error=str(exc))
            return ""
        if not self.store.claim_delivery(rid):
            return ""
        try:
            task = self.add_task(fields)
        except Exception as exc:
            rejected = isinstance(exc, BitrixError) and exc.code.upper() in {"ACCESS_DENIED", "ERROR_ACCESS_DENIED", "ERROR_REQUIRED_PARAMETER", "ERROR_ARGUMENT"}
            message = "Б24 отклонил параметры задачи. Проверьте настройки и повторите отправку." if rejected else "Б24 не подтвердил создание. Проверьте наличие задачи. Ответы сохранены."
            self.store.delivery_result(rid, "rejected" if rejected else "unknown", error=message)
            return ""
        self.store.delivery_result(rid, "created", task=task)
        self.send_attachment_comment(confirmation, task)
        return task

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
        if previous:
            session["revision"] = int(previous.get("revision") or 0) + 1
            if isinstance(self.store, FlowStore):
                self.store.abandon(previous)
        editor = getattr(self, "internal_editor", None)
        version, document = editor.store.active() if editor else (None, None)
        if document:
            session = graph.start(session, document, version)
            if isinstance(self.store, FlowStore):
                graph.state(session)["drafts"] = self.store.drafts(user_id)
                graph.state(session)["legacy_draft"] = bool(self.initiative_draft(user_id))
        if isinstance(self.store, FlowStore):
            pending = bool(self.store.pending_for_user(user_id))
            session["pending_delivery"] = pending
            if document:
                graph.state(session)["pending_delivery"] = pending
        session["active_message_id"] = active_message_id
        self.store.put_session(key, session)
        return session

    def refresh_idle(self, session):
        editor = getattr(self, "internal_editor", None)
        if not editor:
            return session
        state = session.get("constructor")
        idle = (state["node"] == state["document"]["start"] and not state["trail"] and state["status"] == "in_progress"
                if state else session.get("current_screen") == "type" and not session.get("edit_mode") and not session["data"].get("request_type"))
        if idle and not session.get("delivery_locked") and editor.store.active_version() != (state.get("version") if state else None):
            return self.restart_session(session["dialog_id"], session["user_id"])
        return session

    def prepare_graph_profile(self, session):
        state = session.get("constructor")
        if not state or state.get("profile_loaded") or not graph.node(session).get("bind", "").startswith("profile."):
            return
        state["profile_loaded"] = True
        try:
            profile = fetch_profile(self.call, session["user_id"])
            cached = json.loads(self.store.get_meta(f"initiative_profile:{session['user_id']}") or "{}")
        except Exception as exc:
            LOG.warning("Could not prefill DeskFlow profile (%s)", type(exc).__name__)
            return
        cached = cached if isinstance(cached, dict) else {}
        source, values = cached.get("source", {}), cached.get("profile", {})
        source = source if isinstance(source, dict) else {}
        values = values if isinstance(values, dict) else {}
        state["profile_source"] = profile
        state["answers"].update({"profile." + key: values.get(key, value) if source.get(key) == value else value for key, value in profile.items()})
        graph.settle(session)

    def write_transition(self, key, session, event):
        if isinstance(self.store, FlowStore):
            self.store.put_session(key, session, event)
        else:
            self.store.put_session(key, session)
        state = session.get("constructor")
        if state and state.get("profile_source"):
            values = {key: state["answers"].get("profile." + key) for key in initiatives.PROFILE_LABELS}
            if all(values.values()):
                self.store.set_meta(f"initiative_profile:{session['user_id']}", json.dumps({"profile": values, "source": state["profile_source"]}, ensure_ascii=False))

    def resume_request(self, session, transition):
        status = transition["status"]
        uid = session["user_id"]
        if status == "legacy_draft_requested" or status == "draft_resume_requested" and not graph.is_custom(session):
            saved = self.resume_initiative_draft(session) if self.initiative_draft(uid) else None
        elif not isinstance(self.store, FlowStore):
            saved = None
        elif status == "pending_delivery_requested":
            saved = self.store.pending_for_user(uid)
        else:
            saved = self.store.request_for_user(transition["request_id"], uid, "draft")
            if saved and graph.is_custom(saved):
                graph.state(saved)["status"] = "in_progress"
        if saved:
            if isinstance(self.store, FlowStore) and saved["request_id"] != session["request_id"]:
                self.store.abandon(session)
            saved.update(dialog_id=session["dialog_id"], active_message_id=session.get("active_message_id"),
                         revision=max(saved["revision"], session["revision"]) + 1)
        return saved

    def receive_graph_files(self, session, files, text):
        if len(files) > 30:
            return {"status": "validation_error", "message": "Прикрепите до 30 файлов за один раз."}
        checked = []
        try:
            for item in files:
                fid = int(item["id"])
                info = self.call("disk.file.get", {"id": fid})
                if not isinstance(info, dict) or int(info.get("ID", info.get("id", 0))) != fid or str(info.get("DELETED_TYPE", "0")) != "0":
                    raise ValueError()
                name = info.get("NAME", info.get("name"))
                size = int(info.get("SIZE", info.get("size")))
                if not isinstance(name, str) or not name or size < 0:
                    raise ValueError()
                checked.append({"id": fid, "name": name, "size": size})
        except Exception:
            return {"status": "validation_error", "message": "Б24 не подтвердил доступ к одному из файлов. Проверьте вложения и отправьте ещё раз."}
        return graph.attachments(session, checked, text)

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
            # In the personal bot dialog, prepare the managed chat first and
            # refresh the launcher into a pure LINK button. A link does not
            # create a command event, so Bitrix has no loading spinner to wait on.
            if not is_service_chat:
                service_dialog_id = self.ensure_service_chat(user_id, user_name)
                try:
                    self.ensure_launcher_message(
                        user_id,
                        source_dialog_id,
                    )
                except Exception as exc:
                    LOG.warning(
                        "Launcher button could not be prepared for user=%s: %s",
                        user_id,
                        sanitize_error(exc),
                    )
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

            session = self.store.get_session(self.session_key(service_dialog_id, user_id)) if isinstance(self.store, FlowStore) else None
            if session is None or session.get("current_screen") in {"done", "instruction"}:
                session = self.restart_session(service_dialog_id, user_id)
            self.render_current(self.refresh_idle(session))
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

        refreshed = self.refresh_idle(session)
        if refreshed is not session:
            self.render_current(refreshed)
            return
        self.sync_delivery(session)
        self.prepare_graph_profile(session)
        event_key = f"message:{message_id}" if message_id else None
        if event_key and isinstance(self.store, FlowStore) and self.store.seen(session["request_id"], event_key):
            self.render_current(session)
            return
        step = step_label(session)
        accepts_files = session.get("current_screen") in {"document", "edit_attachments_input"} or view(session).get("accepts_files")
        if files and accepts_files and not session.get("delivery_locked"):
            result = (self.receive_graph_files(session, files, text) if graph.is_custom(session)
                      else submit_attachment_message(session, [int(item["id"]) for item in files], [str(item.get("name") or "") for item in files], text))
        elif files:
            result = {"status": "validation_error", "message": "На этом экране вложения не принимаются."}
        else:
            result = submit_text(session, text)
        self.prepare_graph_profile(session)
        detail = {"text": text[:20000], "files": [{"id": item["id"], "name": str(item.get("name") or "")[:250]} for item in files[:30]],
                  "result": result["status"], "next": step_label(session)}
        self.write_transition(key, session, (event_key, "files" if files else "text", step, detail) if event_key else None)
        if not files or not isinstance(self.store, FlowStore):
            self.delete_chat_message(message_id)
        override = view(session)["text"] + "\n\n" + result["message"] if result["status"] == "validation_error" else None
        self.render_current(session, text_override=override)

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

        raw_params = str(command.get("params") or "")
        if raw_params == "launcher":
            service_dialog_id = self.ensure_service_chat(
                user_id,
                str(user.get("name") or ""),
            )

            key = self.session_key(service_dialog_id, user_id)
            session = self.store.get_session(key)
            if session is None:
                session = self.restart_session(service_dialog_id, user_id)
            else:
                session = normalize_session(
                    session,
                    user_id=user_id,
                    dialog_id=service_dialog_id,
                )
            self.render_current(self.refresh_idle(session))

            # Command.answer is mandatory for a keyboard COMMAND. It releases
            # Bitrix's loading state and returns the native DIALOG button for
            # the verified/new service chat.
            self.answer_launcher_command(data, service_dialog_id)
            return

        service_dialog_id = self.get_service_chat(user_id)
        if not service_dialog_id or dialog_id != service_dialog_id or command.get("context", "keyboard") != "keyboard":
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

        refreshed = self.refresh_idle(session)
        if refreshed is not session:
            self.render_current(refreshed)
            return
        self.sync_delivery(session)
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

        origin_message = data.get("message") or {}
        origin = int(origin_message.get("id") or 0)
        bot_message = origin_message.get("authorId") in (None, self.bot_id, str(self.bot_id))
        if revision != int(session["revision"]) or bot_message and origin and session.get("active_message_id") and origin != int(session["active_message_id"]):
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

        button = current["buttons"][index]
        action = button["action"]
        step = step_label(session)
        transition = submit_action(session, action)
        self.prepare_graph_profile(session)
        status = transition["status"]
        detail = {"button": button["label"], "action": action, "result": status, "next": step_label(session)}
        event = (f"button:{revision}:{index}", "button", step, detail)

        if status in {"task_ready", "delivery_check_requested"}:
            task_id = self.deliver_task(session, event, check_only=status == "delivery_check_requested")
            if task_id:
                self.clear_submitted_draft(session)
                mark_task_created(session, task_id)
                self.store.put_session(key, session)
            self.render_current(session)
            return

        if status == "draft_saved":
            if graph.is_custom(session) and isinstance(self.store, FlowStore):
                state = graph.state(session)
                limit = state["document"]["settings"].get("max_drafts", 10)
                drafts = [draft for draft in self.store.drafts(user_id) if draft["id"] != session["request_id"]]
                if len(drafts) >= limit:
                    state["status"] = "in_progress"
                    transition = {"status": "validation_error", "message": "Продолжите один из сохранённых черновиков, чтобы освободить место."}
                    detail["result"] = transition["status"]
            else:
                self.persist_initiative(session, draft=True)
        elif status in {"draft_resume_requested", "legacy_draft_requested", "pending_delivery_requested"}:
            self.write_transition(key, session, event)
            saved = self.resume_request(session, transition)
            if saved is None:
                self.render_current(session, text_override=current["text"] + "\n\nОбращение уже недоступно.")
                return
            session = saved
            event = (f"resume:{session['revision']}", "resume", step_label(session), {})
        elif status == "restart_requested":
            self.write_transition(key, session, event)
            self.render_current(self.restart_session(dialog_id, user_id))
            return

        self.write_transition(key, session, event)
        override = view(session)["text"] + "\n\n" + transition["message"] if transition["status"] == "validation_error" else None
        self.render_current(session, text_override=override)

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

        # Do not create a technical chat just because the employee opened
        # the bot. The launcher validates/creates it only when clicked.
        self.ensure_launcher_message(
            user_id,
            dialog_id,
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
        self.store.recover_deliveries()
        self.internal_editor = start_editor(self.portal_base, self.db_path, self.store, lambda: self.task_field_catalog, self.task_fields)
        self.register()
        self.last_editor_access_change = self.internal_editor.store.latest_change() if self.internal_editor else 0
        self.next_editor_access_check = 0.0
        self.refresh_known_launchers()
        next_housekeeping = 0.0
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
                self.refresh_editor_access()
                if time.monotonic() >= next_housekeeping:
                    _, document = self.internal_editor.store.active() if self.internal_editor else (None, None)
                    self.store.mark_idle(document["settings"].get("idle_hours", 24) if document else 24)
                    next_housekeeping = time.monotonic() + 60
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
        path = os.getenv("FLOWDESK_CHATBOT_DB", "processes/flowdesk_chatbot/flowdesk_state.sqlite3")
        with worker_lock(path):
            Runtime().run()
        return 0
    except Exception as exc:
        print(f"ERROR: {sanitize_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
