from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from processes.flowdesk_chatbot import initiatives as ini
from processes.flowdesk_chatbot import test_worker as worker_fixtures
from processes.flowdesk_chatbot.bitrix_worker import TASK_USER_FIELDS
from processes.flowdesk_chatbot.engine import (
    new_session, normalize_session, submit_action, submit_attachment_message,
    submit_text, task_description, task_registry_fields, task_title, view,
)
from processes.flowdesk_chatbot.initiator_profile import fetch_profile
from processes.flowdesk_chatbot.storage import SessionStore

PROFILE = {"name": "Иванова Анна", "department": "Продажи", "contact": "anna@example.test", "manager": "Петров Иван"}


def click(session: dict, label: str) -> dict:
    button = next(button for button in view(session)["buttons"] if button["label"] == label)
    return submit_action(session, button["action"])


def start(scale: str = ini.SMALL) -> dict:
    session = new_session(153, "chat999")
    click(session, "Предложение / Идея")
    data = ini.state(session)
    data.update(profile=dict(PROFILE), profile_loaded=True, draft_checked=True)
    click(session, "Данные верны")
    click(session, scale)
    click(session, "Изменить существующее")
    click(session, "Процесс")
    click(session, "Клиенты")
    click(session, "Сотрудники")
    click(session, "Продолжить")
    click(session, "В одном подразделении")
    return session


def finish(session: dict, *, conditions: bool = True, files: bool = False) -> list[str]:
    visited = []
    while session["current_screen"] != "initiative_review":
        key = ini.state(session)["step"]
        visited.append(key)
        current = view(session)
        if key == "materials":
            if files:
                submit_attachment_message(session, [501, 502], ["Пример.pdf", "Схема.png"], "Связана с задачей #12")
            else:
                click(session, "Материалов пока нет")
        elif current["accepts_text"]:
            submit_text(session, f"Ответ для {key}")
        elif key == "deadline":
            click(session, "Есть срок" if conditions else "Нет жёсткого срока")
        elif key == "requirements":
            click(session, "Есть" if conditions else view(session)["buttons"][1]["label"])
        elif key == "risks":
            click(session, "Есть" if conditions else "Не выявлены")
        else:
            raise AssertionError(f"Unexpected screen: {current}")
        if len(visited) > 35:
            raise AssertionError("Questionnaire did not terminate")
    return visited


def runtime():
    result = worker_fixtures.DeskFlowWorkerTests().runtime()
    result.send = lambda *args, **kwargs: 111
    result.update_message = lambda *args, **kwargs: None
    result.set_text_field = lambda *args, **kwargs: True
    result.history_block = lambda *args: ""
    return result


def command(worker, session: dict, label: str) -> None:
    buttons = view(session)["buttons"]
    index = next(index for index, button in enumerate(buttons) if button["label"] == label)
    worker.handle_command({
        "command": {"command": worker.command_name, "params": f"{session['revision']}:{index}"},
        "user": {"id": session["user_id"]}, "chat": {"dialogId": session["dialog_id"]},
    })


class InitiativeQuestionnaireTests(unittest.TestCase):
    def test_small_questionnaire_conditional_details_and_task_payload(self):
        session = start()
        visited = finish(session, files=True)
        self.assertEqual(len(set(visited)), 8)
        self.assertEqual(visited.count("requirements"), 2)
        self.assertEqual(visited.count("deadline"), 2)
        self.assertEqual(click(session, "Отправить")["status"], "task_ready")
        self.assertIn("Ответ для title", task_title(session))
        description = task_description(session)
        for value in PROFILE.values():
            self.assertIn(value, description)
        self.assertIn("Клиенты; Сотрудники", description)
        self.assertIn("Связана с задачей #12", description)
        self.assertEqual(session["data"]["document_file_ids"], [501, 502])
        fields = task_registry_fields(session)
        self.assertEqual(fields["UF_FLOWDESK_INIT_SCALE"], ini.SMALL)
        self.assertEqual(fields["UF_FLOWDESK_INIT_TITLE"], "Ответ для title")
        self.assertEqual(fields["UF_FLOWDESK_INIT_MANAGER"], PROFILE["manager"])

    def test_large_questionnaire_without_conditional_followups(self):
        session = start(ini.LARGE)
        visited = finish(session, conditions=False)
        self.assertEqual(len(visited), 14)
        self.assertEqual(click(session, "Отправить")["status"], "task_ready")
        self.assertIn("Работа сейчас: Ответ для current", task_description(session))
        self.assertIn("Границы инициативы: Ответ для bounds", task_description(session))
        self.assertIn("Признаки успеха: Ответ для success", task_description(session))

    def test_unknown_scale_uses_short_questionnaire_and_flags_review(self):
        session = start(ini.UNKNOWN)
        finish(session, conditions=False)
        self.assertEqual(len(ini.questions(session)), 8)
        self.assertIn("Масштаб инициативы", ini.open_items(session))
        self.assertEqual(task_registry_fields(session)["UF_FLOWDESK_INIT_SCALE"], ini.UNKNOWN)

    def test_scale_switch_asks_only_missing_large_answers_and_keeps_small_answers(self):
        session = start()
        finish(session, files=True)
        original = deepcopy(ini.state(session)["answers"])
        original_files = list(session["data"]["document_file_ids"])
        click(session, "Исправить ответ")
        click(session, "Масштаб")
        click(session, ini.LARGE)
        self.assertEqual(ini.state(session)["step"], "current")
        visited = finish(session, conditions=False)
        self.assertEqual(visited, ["current", "bounds", "success", "owner", "resources", "risks"])
        for key, answer in original.items():
            self.assertEqual(ini.state(session)["answers"][key], answer)
        self.assertEqual(session["data"]["document_file_ids"], original_files)
        click(session, "Исправить ответ")
        click(session, "Масштаб")
        click(session, ini.SMALL)
        self.assertEqual(session["current_screen"], "initiative_review")
        self.assertIn("Дополнительные сведения", task_description(session))
        self.assertIn("Ответ для current", task_description(session))
        self.assertEqual(task_registry_fields(session)["UF_FLOWDESK_INIT_CURRENT"], "Ответ для current")

    def test_other_values_and_multi_select_are_not_lost_on_back(self):
        session = new_session(153, "chat999")
        click(session, "Предложение / Идея")
        ini.state(session)["profile"] = dict(PROFILE)
        click(session, "Данные верны")
        click(session, ini.SMALL)
        click(session, ini.OTHER)
        submit_text(session, "Объединить два процесса")
        click(session, ini.OTHER)
        submit_text(session, "Партнёрская программа")
        click(session, "Клиенты")
        click(session, ini.OTHER)
        click(session, "Продолжить")
        submit_text(session, "Агенты")
        self.assertEqual(ini.state(session)["step"], "scope")
        click(session, "← Назад")
        self.assertEqual(ini.state(session)["audience"], ["Клиенты", ini.OTHER])
        self.assertEqual(ini.meta_value(session, "action"), "Другое: Объединить два процесса")
        self.assertEqual(ini.meta_value(session, "subject"), "Другое: Партнёрская программа")
        self.assertEqual(ini.meta_value(session, "audience"), "Клиенты; Другое: Агенты")

    def test_edit_changes_only_selected_answer(self):
        session = start()
        finish(session)
        before = deepcopy(ini.state(session)["answers"])
        click(session, "Исправить ответ")
        click(session, "Предлагаемое изменение")
        submit_text(session, "Новое предложение")
        self.assertEqual(session["current_screen"], "initiative_review")
        self.assertEqual(ini.state(session)["answers"]["change"]["text"], "Новое предложение")
        for key, answer in before.items():
            if key != "change":
                self.assertEqual(ini.state(session)["answers"][key], answer)

    def test_large_unknown_owner_resources_and_risks_are_flagged(self):
        session = start(ini.LARGE)
        while ini.state(session)["step"] != "owner":
            key = ini.state(session)["step"]
            submit_text(session, f"Ответ для {key}")
        click(session, "Нужно определить при рассмотрении")
        click(session, "Нужно уточнить")
        click(session, "Нужно оценить при рассмотрении")
        click(session, "Нужно оценить")
        click(session, "Нет жёсткого срока")
        click(session, "Материалов пока нет")
        self.assertEqual(len(ini.open_items(session)), 4)
        self.assertIn("Владелец результата и проверка", task_description(session))
        self.assertEqual(click(session, "Отправить")["status"], "task_ready")

    def test_conditional_details_are_preserved_but_not_submitted_after_no(self):
        session = start()
        finish(session)
        click(session, "Исправить ответ")
        click(session, "Требования и условия")
        click(session, "Нет известных условий")
        self.assertEqual(ini.state(session)["answers"]["requirements"]["text"], "Ответ для requirements")
        self.assertNotIn("Ответ для requirements", task_description(session))
        click(session, "Исправить ответ")
        click(session, "Требования и условия")
        click(session, "Есть")
        self.assertIn("Ответ для requirements", view(session)["text"])
        submit_text(session, "Уточнённое условие")
        self.assertIn("Уточнённое условие", task_description(session))

    def test_empty_replacement_does_not_remove_original_materials(self):
        session = start()
        finish(session, files=True)
        click(session, "Исправить ответ")
        click(session, "Материалы")
        click(session, "Заменить материалы")
        before = deepcopy(session["data"])
        self.assertEqual(submit_attachment_message(session, [], [], "")["status"], "empty")
        self.assertEqual(session["data"], before)
        submit_attachment_message(session, [900], ["Новый.pdf"], "Новый пакет")
        self.assertEqual(session["data"]["document_file_ids"], [900])
        self.assertEqual(session["current_screen"], "initiative_review")

    def test_attachment_addition_deduplicates_file_ids(self):
        session = start()
        finish(session, files=True)
        click(session, "Исправить ответ")
        click(session, "Материалы")
        click(session, "Добавить материалы")
        submit_attachment_message(session, [502, 900], ["Схема.png", "Дополнение.pdf"])
        self.assertEqual(session["data"]["document_file_ids"], [501, 502, 900])

    def test_profile_missing_values_are_collected_before_scale(self):
        session = new_session(153, "chat999")
        click(session, "Предложение / Идея")
        click(session, "Заполнить недостающие данные")
        for key, value in PROFILE.items():
            self.assertEqual(ini.state(session)["profile_field"], key)
            submit_text(session, value)
        self.assertEqual(session["current_screen"], "initiative_profile")
        click(session, "Данные верны")
        self.assertTrue(ini.state(session)["profile_confirmed"])
        self.assertEqual(ini.state(session)["step"], "scale")

    def test_legacy_unfinished_proposal_keeps_text_files_and_id(self):
        old = new_session(153, "chat999")
        old.update(current_screen="confirm", request_id="stable-id")
        old["data"].update(request_type="Предложение / Идея", description="Старая идея", document_file_ids=[19])
        old_revision = old["revision"]
        normalize_session(old)
        self.assertEqual(old["current_screen"], "initiative_profile")
        self.assertEqual(old["request_id"], "stable-id")
        self.assertEqual(ini.state(old)["answers"]["change"]["text"], "Старая идея")
        self.assertEqual(old["data"]["document_file_ids"], [19])
        self.assertEqual(old["revision"], old_revision + 1)

    def test_uncertainty_in_free_text_is_included_in_review(self):
        session = start(ini.LARGE)
        finish(session)
        ini.state(session)["answers"]["owner"] = {"text": "Владелец результата пока неизвестен, нужно уточнить при рассмотрении."}
        self.assertIn("Владелец результата и проверка", ini.open_items(session))
        self.assertIn("Владелец результата и проверка", task_registry_fields(session)["UF_FLOWDESK_INIT_OPEN"])

    def test_service_desk_redirect_does_not_create_task(self):
        session = new_session(153, "chat999")
        click(session, "Сообщить о сбое")
        self.assertEqual(session["current_screen"], "instruction")
        self.assertIn("Service Desk", view(session)["text"])
        self.assertIn("objectId=237", view(session)["text"])
        self.assertIsNone(session["task_id"])

    def test_first_back_returns_to_main_menu_and_keeps_initiative_answers(self):
        session = start()
        submit_text(session, "Сохранённое название")
        while session["current_screen"] != "type":
            click(session, "← Назад")
        click(session, "Предложение / Идея")
        self.assertEqual(ini.state(session)["answers"]["title"]["text"], "Сохранённое название")
        click(session, "← Назад")
        click(session, "Вопрос / Уточнение")
        self.assertFalse(ini.is_initiative(session))
        submit_text(session, "Обычный вопрос")
        self.assertIn("Обычный вопрос", task_description(session))
        self.assertNotIn("Сохранённое название", task_description(session))

    def test_profile_back_moves_one_field_without_erasing_already_entered_value(self):
        session = new_session(153, "chat999")
        click(session, "Предложение / Идея")
        click(session, "Заполнить недостающие данные")
        submit_text(session, "Анна Иванова")
        self.assertEqual(ini.state(session)["profile_field"], "department")
        click(session, "← Назад")
        self.assertEqual(ini.state(session)["profile_field"], "name")
        self.assertEqual(ini.state(session)["profile"]["name"], "Анна Иванова")


class InitiativeWorkerTests(unittest.TestCase):
    def test_saved_draft_survives_database_reopen_and_moves_to_current_chat(self):
        session = start()
        submit_text(session, "Автоматизация отчёта")
        saved_request = session["request_id"]
        click(session, "Сохранить черновик")
        worker = runtime()
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "state.sqlite3")
            worker.store = SessionStore(path)
            worker.persist_initiative(session, draft=True)
            worker.store = SessionStore(path)
            fresh = new_session(153, "chatNEW")
            click(fresh, "Предложение / Идея")
            fresh["active_message_id"] = 999
            worker.prepare_initiative(fresh)
            self.assertEqual(fresh["current_screen"], "initiative_existing_draft")
            restored = worker.resume_initiative_draft(fresh)
            self.assertEqual(restored["request_id"], saved_request)
            self.assertEqual(restored["dialog_id"], "chatNEW")
            self.assertEqual(restored["active_message_id"], 999)
            self.assertGreater(restored["revision"], session["revision"])
            self.assertEqual(ini.state(restored)["step"], "reason")
            self.assertEqual(ini.state(restored)["answers"]["title"]["text"], "Автоматизация отчёта")

    def test_foreign_or_corrupt_draft_is_never_loaded(self):
        worker = runtime()
        session = start()
        click(session, "Сохранить черновик")
        session["user_id"] = 999
        worker.store.set_meta("initiative_draft:153", json.dumps(session))
        self.assertIsNone(worker.initiative_draft(153))
        worker.store.set_meta("initiative_draft:153", "{broken")
        self.assertIsNone(worker.initiative_draft(153))

    def test_submitting_another_initiative_does_not_remove_saved_draft(self):
        worker = runtime()
        saved = start()
        click(saved, "Сохранить черновик")
        worker.persist_initiative(saved, draft=True)
        another = start()
        worker.clear_submitted_draft(another)
        self.assertIsNotNone(worker.initiative_draft(153))
        worker.clear_submitted_draft(saved)
        self.assertIsNone(worker.initiative_draft(153))

    def test_actual_submit_command_creates_structured_task_and_completion(self):
        worker = runtime()
        session = start(ini.LARGE)
        finish(session, files=True)
        worker.store.set_meta("service_chat:153", "chat999")
        worker.store.put_session("chat999:153", session)
        worker.task_user_fields = set(TASK_USER_FIELDS)
        calls = []
        def call(method, params=None):
            calls.append((method, params))
            if method == "tasks.task.list":
                return {"tasks": []}
            if method == "tasks.task.add":
                return {"task": {"id": "321"}}
            return True
        worker.call = call
        worker.call_v3 = call
        with patch.dict("os.environ", {"FLOWDESK_PROJECT_ID": "2", "FLOWDESK_DEFAULT_RESPONSIBLE_ID": "72"}):
            command(worker, session, "Отправить")
        fields = next(params["fields"] for method, params in calls if method == "tasks.task.add")
        self.assertEqual(fields["GROUP_ID"], 2)
        self.assertEqual(fields["RESPONSIBLE_ID"], 72)
        self.assertEqual(fields["CREATED_BY"], 153)
        self.assertEqual(fields["UF_TASK_WEBDAV_FILES"], ["n501", "n502"])
        self.assertEqual(fields["UF_FLOWDESK_INIT_SCALE"], ini.LARGE)
        self.assertIn("Ответ для current", fields["DESCRIPTION"])
        stored = worker.store.get_session("chat999:153")
        self.assertEqual(stored["current_screen"], "done")
        self.assertIn("первичное рассмотрение", view(stored)["text"])

    def test_material_file_event_advances_to_initiative_review(self):
        worker = runtime()
        session = start()
        while ini.state(session)["step"] != "materials":
            if view(session)["accepts_text"]:
                submit_text(session, "Ответ")
            elif ini.state(session)["step"] == "requirements":
                click(session, "Нет известных условий")
            else:
                click(session, "Нет жёсткого срока")
        worker.store.set_meta("service_chat:153", "chat999")
        worker.store.put_session("chat999:153", session)
        worker.delete_chat_message = lambda message_id: None
        worker.handle_message({"user": {"id": 153}, "chat": {"dialogId": "chat999"}, "message": {"id": 55, "text": "Комментарий", "params": {"FILE_ID": [501]}}})
        stored = worker.store.get_session("chat999:153")
        self.assertEqual(stored["current_screen"], "initiative_review")
        self.assertEqual(stored["data"]["document_file_ids"], [501])

    def test_profile_correction_is_reused_until_source_changes(self):
        worker = runtime()
        initial = {**PROFILE, "contact": ""}
        with patch("processes.flowdesk_chatbot.bitrix_worker.fetch_profile", return_value=initial):
            first = new_session(153, "chat999")
            click(first, "Предложение / Идея")
            worker.prepare_initiative(first)
        ini.state(first)["profile"]["contact"] = "added@example.test"
        ini.state(first)["profile_confirmed"] = True
        worker.persist_initiative(first)
        with patch("processes.flowdesk_chatbot.bitrix_worker.fetch_profile", return_value=initial):
            second = new_session(153, "chat999")
            click(second, "Предложение / Идея")
            worker.prepare_initiative(second)
        self.assertEqual(ini.state(second)["profile"]["contact"], "added@example.test")
        changed = {**initial, "contact": "new-profile@example.test", "department": "Новый отдел"}
        with patch("processes.flowdesk_chatbot.bitrix_worker.fetch_profile", return_value=changed):
            third = new_session(153, "chat999")
            click(third, "Предложение / Идея")
            worker.prepare_initiative(third)
        self.assertEqual(ini.state(third)["profile"]["contact"], "new-profile@example.test")
        self.assertEqual(ini.state(third)["profile"]["department"], "Новый отдел")


class InitiatorProfileTests(unittest.TestCase):
    def test_department_head_and_work_contact_are_resolved(self):
        def call(method, params):
            if method == "department.get":
                return [{"ID": "4", "NAME": "Продажи", "UF_HEAD": "72"}]
            user_id = params["FILTER"]["ID"]
            if user_id == 153:
                return [{"ID": "153", "NAME": "Анна", "LAST_NAME": "Иванова", "EMAIL": "anna@example.test", "UF_DEPARTMENT": [4]}]
            return [{"ID": "72", "NAME": "Иван", "LAST_NAME": "Петров"}]
        self.assertEqual(fetch_profile(call, 153), PROFILE)

    def test_own_department_head_uses_parent_and_cycles_terminate(self):
        def call(method, params):
            if method == "user.get":
                identifier = params["FILTER"]["ID"]
                return [{"ID": identifier, "NAME": "Анна" if identifier == 153 else "Иван", "UF_DEPARTMENT": [4]}]
            identifier = params["ID"]
            return [{"ID": identifier, "NAME": "Отдел", "UF_HEAD": 153 if identifier == 4 else 72, "PARENT": 5}]
        self.assertEqual(fetch_profile(call, 153)["manager"], "Иван")

    def test_multiple_different_heads_leave_manager_for_employee_confirmation(self):
        def call(method, params):
            if method == "user.get":
                return [{"ID": 153, "NAME": "Анна", "UF_DEPARTMENT": [4, 5]}]
            identifier = params["ID"]
            return [{"ID": identifier, "NAME": f"Отдел {identifier}", "UF_HEAD": 70 + identifier}]
        result = fetch_profile(call, 153)
        self.assertEqual(result["department"], "Отдел 4; Отдел 5")
        self.assertEqual(result["manager"], "")


if __name__ == "__main__":
    unittest.main()
