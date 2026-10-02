from __future__ import annotations

import unittest

from processes.flowdesk_chatbot.bitrix_worker import Runtime
from processes.flowdesk_chatbot.config import PRODUCT_TREE
from processes.flowdesk_chatbot.engine import (
    ACTION_ATTACHMENT_ADD,
    ACTION_ATTACHMENT_DELETE,
    ACTION_ATTACHMENT_REPLACE,
    ACTION_BACK,
    ACTION_EDIT,
    ACTION_EDIT_MORE,
    ACTION_EDIT_REVIEW,
    EDIT_FIELD_PREFIX,
    new_session,
    submit_action,
    submit_attachment_message,
    submit_text,
    summary_text,
    task_registry_fields,
    view,
)


def session_at(screen: str) -> dict:
    session = new_session(user_id=153, dialog_id="chat999")
    session["current_screen"] = screen
    return session


class DeskFlowEngineTests(unittest.TestCase):
    def test_start_screen_has_clean_labels_and_locked_text(self) -> None:
        session = new_session(153, "chat999")
        current = view(session)

        self.assertEqual(current["screen"], "type")
        self.assertFalse(current["accepts_text"])

        labels = [button["label"] for button in current["buttons"]]
        self.assertEqual(
            labels[:6],
            [
                "Запрос",
                "Предложение / Идея",
                "Вопрос / Уточнение",
                "Горит контракт",
                "Жалоба",
                "Другое",
            ],
        )
        self.assertTrue(all(not label.startswith("2.") for label in labels))

        styles = {button["label"]: button["style"] for button in current["buttons"]}
        self.assertEqual(styles["Горит контракт"], "alert")
        self.assertEqual(styles["Жалоба"], "alert")
        self.assertEqual(styles["Другое"], "primary")

    def test_button_screen_is_locked_and_back_is_primary(self) -> None:
        session = new_session(153, "chat999")
        submit_action(session, "Запрос")

        current = view(session)
        self.assertEqual(current["screen"], "target")
        self.assertFalse(current["accepts_text"])

        back = next(button for button in current["buttons"] if button["action"] == ACTION_BACK)
        self.assertEqual(back["style"], "primary")

    def test_bin_iin_extracts_exactly_twelve_digits_and_keeps_leading_zero(self) -> None:
        session = session_at("details")
        session["detail_fields"] = ["bin_iin"]
        session["detail_index"] = 0

        result = submit_text(session, "БИН: 001 234-567-890")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["data"]["details"]["bin_iin"], "001234567890")
        self.assertEqual(session["current_screen"], "description")

    def test_invalid_bin_does_not_mutate_or_add_back_history(self) -> None:
        session = session_at("details")
        session["detail_fields"] = ["bin_iin"]
        session["detail_index"] = 0
        before_history = len(session["history"])

        result = submit_text(session, "БИН: 12345")

        self.assertEqual(result["status"], "validation_error")
        self.assertEqual(session["current_screen"], "details")
        self.assertNotIn("bin_iin", session["data"]["details"])
        self.assertEqual(len(session["history"]), before_history)

    def test_detail_length_limits(self) -> None:
        cases = [
            ("name_fio", 200),
            ("doc_num", 50),
            ("doc_date", 20),
            ("approv_list_num", 50),
            ("reason", 500),
            ("card_num", 50),
            ("card_position", 200),
            ("card_branch", 200),
            ("period", 50),
        ]

        for field_name, limit in cases:
            with self.subTest(field=field_name):
                session = session_at("details")
                session["detail_fields"] = [field_name]
                session["detail_index"] = 0

                result = submit_text(session, "x" * (limit + 1))
                self.assertEqual(result["status"], "validation_error")
                self.assertEqual(session["current_screen"], "details")

    def test_description_has_no_character_limit_and_summary_keeps_full_text(self) -> None:
        session = session_at("description")
        value = "Очень подробное описание. " * 500

        result = submit_text(session, value)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["data"]["description"], value.strip())
        self.assertEqual(session["current_screen"], "insurance_type")

        session["current_screen"] = "confirm"
        text = summary_text(session)
        self.assertIn(value.strip(), text)

    def test_not_applicable_skips_class_and_product(self) -> None:
        session = session_at("insurance_type")
        session["data"]["product"] = "старый класс"
        session["data"]["subproduct"] = "старый продукт"

        result = submit_action(session, "Не применимо")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["current_screen"], "document")
        self.assertIsNone(session["data"]["product"])
        self.assertIsNone(session["data"]["subproduct"])

    def test_edit_insurance_type_resets_and_reselects_dependencies(self) -> None:
        session = session_at("confirm")
        session["data"].update(
            {
                "request_type": "Вопрос / Уточнение",
                "description": "Нужна помощь",
                "insurance_type": "Добровольное",
                "product": "Страхование имущества",
                "subproduct": "Light House",
            }
        )

        submit_action(session, ACTION_EDIT)
        submit_action(session, f"{EDIT_FIELD_PREFIX}insurance_type")

        self.assertEqual(session["current_screen"], "insurance_type")
        self.assertIsNone(session["data"]["insurance_type"])
        self.assertIsNone(session["data"]["product"])
        self.assertIsNone(session["data"]["subproduct"])

        submit_action(session, "Обязательное")
        self.assertEqual(session["current_screen"], "product")

        insurance_class = next(iter(PRODUCT_TREE["Обязательное"]))
        submit_action(session, insurance_class)
        self.assertEqual(session["current_screen"], "subproduct")

        product = PRODUCT_TREE["Обязательное"][insurance_class][0]
        submit_action(session, product)

        self.assertEqual(session["current_screen"], "edit_after")
        self.assertEqual(session["data"]["insurance_type"], "Обязательное")
        self.assertEqual(session["data"]["product"], insurance_class)
        self.assertEqual(session["data"]["subproduct"], product)

    def test_back_during_parent_edit_moves_one_step_not_whole_edit(self) -> None:
        session = session_at("confirm")
        session["data"].update(
            {
                "request_type": "Запрос",
                "target": "Юристы",
                "request": "Согласование документа",
                "request_detail": "Договор страхования",
                "description": "Текст",
            }
        )

        submit_action(session, ACTION_EDIT)
        submit_action(session, f"{EDIT_FIELD_PREFIX}target")
        submit_action(session, "Статисты")
        self.assertEqual(session["current_screen"], "request")

        # One Back returns to the department choice inside the active edit.
        submit_action(session, "__edit_cancel__")
        self.assertEqual(session["current_screen"], "target")
        self.assertTrue(session["edit_mode"])
        self.assertIsNone(session["data"]["target"])

        # Back again exits this field correction and restores the original data.
        submit_action(session, "__edit_cancel__")
        self.assertEqual(session["current_screen"], "edit_menu")
        self.assertFalse(session["edit_mode"])
        self.assertEqual(session["data"]["target"], "Юристы")
        self.assertEqual(session["data"]["request"], "Согласование документа")
        self.assertEqual(session["data"]["request_detail"], "Договор страхования")

    def test_edit_department_resets_dependent_request_data(self) -> None:
        session = session_at("confirm")
        session["data"].update(
            {
                "request_type": "Запрос",
                "target": "Юристы",
                "request": "Согласование документа",
                "request_detail": "Договор страхования",
                "details": {"bin_iin": "001234567890"},
                "description": "Текст",
            }
        )

        submit_action(session, ACTION_EDIT)
        submit_action(session, f"{EDIT_FIELD_PREFIX}target")

        self.assertEqual(session["current_screen"], "target")
        self.assertIsNone(session["data"]["target"])
        self.assertIsNone(session["data"]["request"])
        self.assertIsNone(session["data"]["request_detail"])
        self.assertEqual(session["data"]["details"], {})

        submit_action(session, "Статисты")
        self.assertEqual(session["current_screen"], "request")

        submit_action(session, "Бонусы/Премия")
        self.assertEqual(session["current_screen"], "edit_after")
        self.assertEqual(session["data"]["target"], "Статисты")
        self.assertEqual(session["data"]["request"], "Бонусы/Премия")

    def test_attachment_editor_is_available_even_after_original_skip(self) -> None:
        session = session_at("confirm")
        session["data"]["request_type"] = "Другое"
        session["data"]["description"] = "Текст"
        session["data"]["insurance_type"] = "Не применимо"

        submit_action(session, ACTION_EDIT)
        labels = [button["label"] for button in view(session)["buttons"]]
        self.assertIn("Вложения / ссылка", labels)

    def test_bitrix_file_id_param_is_parsed_from_message_event(self) -> None:
        message = {
            "id": 501,
            "params": {
                "FILE_ID": ["15423", "15424"],
            },
        }

        files = Runtime.message_files(message)

        self.assertEqual(
            files,
            [
                {"id": 15423, "name": ""},
                {"id": 15424, "name": ""},
            ],
        )

    def test_rich_file_object_fallback_is_still_supported(self) -> None:
        message = {
            "params": {
                "FILES": [
                    {"ID": "77", "NAME": "photo.jpg"},
                    {"id": 78, "name": "contract.pdf"},
                ],
            }
        }

        files = Runtime.message_files(message)

        self.assertEqual(
            files,
            [
                {"id": 77, "name": "photo.jpg"},
                {"id": 78, "name": "contract.pdf"},
            ],
        )

    def test_attachment_add_replace_delete(self) -> None:
        session = session_at("confirm")
        session["data"].update(
            {
                "request_type": "Другое",
                "description": "Текст",
                "insurance_type": "Не применимо",
            }
        )

        submit_action(session, ACTION_EDIT)
        submit_action(session, f"{EDIT_FIELD_PREFIX}document")
        submit_action(session, ACTION_ATTACHMENT_ADD)
        result = submit_attachment_message(
            session,
            [11, 12],
            ["a.pdf", "b.jpg"],
            "Вот договор и переписка",
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["current_screen"], "edit_after")
        self.assertEqual(session["data"]["document_file_ids"], [11, 12])
        self.assertEqual(session["data"]["document_comment"], "Вот договор и переписка")

        submit_action(session, ACTION_EDIT_MORE)
        submit_action(session, f"{EDIT_FIELD_PREFIX}document")
        submit_action(session, ACTION_ATTACHMENT_REPLACE)
        self.assertEqual(session["data"]["document_file_ids"], [11, 12])

        result = submit_attachment_message(session, [99], ["new.pdf"], "Новая версия")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["data"]["document_file_ids"], [99])
        self.assertEqual(session["data"]["document_comment"], "Новая версия")

        submit_action(session, ACTION_EDIT_MORE)
        submit_action(session, f"{EDIT_FIELD_PREFIX}document")
        submit_action(session, ACTION_ATTACHMENT_DELETE)
        self.assertEqual(session["data"]["document_file_ids"], [])
        self.assertIsNone(session["data"]["document_comment"])
        self.assertIsNone(session["data"]["document"])

    def test_edit_after_offers_edit_more_and_return_to_review(self) -> None:
        session = session_at("confirm")
        session["data"].update(
            {
                "request_type": "Другое",
                "description": "Старый текст",
                "insurance_type": "Не применимо",
            }
        )

        submit_action(session, ACTION_EDIT)
        submit_action(session, f"{EDIT_FIELD_PREFIX}description")
        submit_text(session, "Новый текст")

        current = view(session)
        actions = [button["action"] for button in current["buttons"]]
        self.assertIn(ACTION_EDIT_MORE, actions)
        self.assertIn(ACTION_EDIT_REVIEW, actions)

    def test_back_revision_is_monotonic(self) -> None:
        session = new_session(153, "chat999")
        first_revision = session["revision"]

        submit_action(session, "Запрос")
        forward_revision = session["revision"]
        self.assertGreater(forward_revision, first_revision)

        submit_action(session, ACTION_BACK)
        self.assertEqual(session["current_screen"], "type")
        self.assertGreater(session["revision"], forward_revision)

    def test_document_screen_accepts_text_but_final_screen_does_not(self) -> None:
        document = session_at("document")
        self.assertTrue(view(document)["accepts_text"])

        done = session_at("done")
        done["task_id"] = "777"
        self.assertFalse(view(done)["accepts_text"])

    def test_summary_contains_all_filled_fields_and_attachment_comment(self) -> None:
        session = session_at("confirm")
        session["data"].update(
            {
                "request_type": "Запрос",
                "target": "Андеррайтинг",
                "request": "Согласовать тариф",
                "details": {
                    "bin_iin": "001234567890",
                    "approv_list_num": "ЛС-55",
                    "reason": "Причина полностью",
                },
                "description": "Полная суть обращения без обрезки",
                "insurance_type": "Добровольное",
                "product": "Страхование имущества",
                "subproduct": "Light House",
                "document_file_ids": [1, 2, 3],
                "document_file_names": ["a.pdf", "b.pdf", "c.jpg"],
                "document_comment": "Комментарий как есть",
            }
        )

        text = summary_text(session)
        for expected in (
            "Андеррайтинг",
            "Согласовать тариф",
            "001234567890",
            "ЛС-55",
            "Причина полностью",
            "Полная суть обращения без обрезки",
            "Добровольное",
            "Страхование имущества",
            "Light House",
            "3 файла",
            "Комментарий как есть",
        ):
            self.assertIn(expected, text)

    def test_registry_fields_are_structured_and_details_stay_in_one_field(self) -> None:
        session = session_at("confirm")
        session["data"].update(
            {
                "request_type": "Запрос",
                "target": "Андеррайтинг",
                "request": "Согласовать тариф",
                "details": {
                    "bin_iin": "001234567890",
                    "approv_list_num": "ЛС-55",
                },
                "description": "Суть",
                "insurance_type": "Добровольное",
                "product": "Страхование имущества",
                "subproduct": "Light House",
            }
        )

        fields = task_registry_fields(session)
        self.assertEqual(fields["UF_FLOWDESK_REQUEST_TYPE"], "Запрос")
        self.assertEqual(fields["UF_FLOWDESK_TARGET"], "Андеррайтинг")
        self.assertEqual(fields["UF_FLOWDESK_REQUEST"], "Согласовать тариф")
        self.assertEqual(fields["UF_FLOWDESK_INSURANCE_TYPE"], "Добровольное")
        self.assertEqual(fields["UF_FLOWDESK_INSURANCE_CLASS"], "Страхование имущества")
        self.assertEqual(fields["UF_FLOWDESK_PRODUCT"], "Light House")
        self.assertEqual(fields["UF_FLOWDESK_DESCRIPTION"], "Суть")
        self.assertEqual(fields["UF_FLOWDESK_INITIATOR_ID"], "153")
        self.assertIn("БИН / ИИН: 001234567890", fields["UF_FLOWDESK_DETAILS"])
        self.assertIn("№ ЛС: ЛС-55", fields["UF_FLOWDESK_DETAILS"])


if __name__ == "__main__":
    unittest.main()
