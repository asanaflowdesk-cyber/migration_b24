from __future__ import annotations

import unittest
from copy import deepcopy

from processes.flowdesk_chatbot.config import (
    DETAIL_FIELDS,
    INSTRUCTIONS,
    PRODUCT_TREE,
    REQUEST_TREE,
    REQUEST_TYPES,
)
from processes.flowdesk_chatbot.engine import (
    ACTION_CONFIRM,
    ACTION_SKIP,
    META_KEYS,
    new_session,
    normalize_session,
    submit_action,
    submit_text,
    view,
)


VALID_DETAIL_VALUES = {
    "name_fio": "ТОО Тест / Иван Иванов",
    "bin_iin": "БИН: 001234567890",
    "doc_num": "ДОГ-123",
    "doc_date": "02.10.2026",
    "approv_list_num": "ЛС-55",
    "reason": "Тестовая причина",
    "card_num": "СКД-12",
    "card_position": "Аналитик",
    "card_branch": "Головной офис",
    "period": "Октябрь 2026",
}


def iter_request_leaves(node: dict, path: tuple[str, ...] = ()):
    if "route" in node:
        yield path, node
        return

    for key, child in node.items():
        if key in META_KEYS:
            continue
        if not isinstance(child, dict):
            raise AssertionError(f"REQUEST_TREE node {path + (key,)} is not a dict")
        yield from iter_request_leaves(child, path + (key,))


class DeskFlowIntegrityTests(unittest.TestCase):
    def _finish_branch_three(self, session: dict) -> None:
        self.assertEqual(session["current_screen"], "description")
        result = submit_text(session, "Подробное тестовое обращение")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["current_screen"], "insurance_type")

        result = submit_action(session, "Не применимо")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["current_screen"], "document")

        result = submit_action(session, ACTION_SKIP)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(session["current_screen"], "confirm")

        result = submit_action(session, ACTION_CONFIRM)
        self.assertEqual(result["status"], "task_ready")

    def test_every_request_tree_leaf_is_reachable(self) -> None:
        leaves = list(iter_request_leaves(REQUEST_TREE))
        self.assertGreater(len(leaves), 20)

        for path, leaf in leaves:
            with self.subTest(path=" > ".join(path), route=leaf["route"]):
                session = new_session(153, "chat999")
                submit_action(session, "Запрос")

                for option in path:
                    current = view(session)
                    actions = [button["action"] for button in current["buttons"]]
                    self.assertIn(
                        option,
                        actions,
                        msg=f"{option!r} is not offered on screen {current['screen']}",
                    )
                    submit_action(session, option)

                route = leaf["route"]
                self.assertIn(route, {"details", "branch_3", "instruction"})

                if route == "details":
                    self.assertEqual(session["current_screen"], "details")
                    expected_fields = list(leaf.get("fields") or [])
                    self.assertEqual(session["detail_fields"], expected_fields)
                    for field_name in expected_fields:
                        self.assertIn(field_name, DETAIL_FIELDS)
                        value = VALID_DETAIL_VALUES[field_name]
                        result = submit_text(session, value)
                        self.assertEqual(result["status"], "ok")
                    self._finish_branch_three(session)
                    continue

                if route == "instruction" and tuple(path) in INSTRUCTIONS:
                    self.assertEqual(session["current_screen"], "instruction")
                    self.assertTrue(view(session)["terminal"])
                    continue

                # Until a concrete instruction is configured, instruction leaves
                # intentionally fall through into the ordinary appeal flow.
                self._finish_branch_three(session)

    def test_all_direct_request_types_reach_expected_destination(self) -> None:
        for request_type, route in REQUEST_TYPES.items():
            if route == "request_tree":
                continue
            with self.subTest(request_type=request_type, route=route):
                session = new_session(153, "chat999")
                result = submit_action(session, request_type)
                self.assertEqual(result["status"], "ok")

                if route == "branch_3":
                    self._finish_branch_three(session)
                elif route == "instruction":
                    self.assertEqual(session["current_screen"], "instruction")
                    self.assertIn((request_type,), INSTRUCTIONS)
                    current = view(session)
                    self.assertTrue(current["terminal"])
                    self.assertEqual(
                        [button["action"] for button in current["buttons"]],
                        ["__new_request__"],
                    )
                else:
                    self.fail(f"Unexpected direct route: {route}")

    def test_marketing_business_cards_is_instruction_leaf(self) -> None:
        session = new_session(153, "chat999")
        submit_action(session, "Запрос")
        submit_action(session, "Маркетологи")
        submit_action(session, "Заказ визиток")

        self.assertEqual(session["current_screen"], "instruction")
        current = view(session)
        self.assertIn(
            "12K7Nu-iEuqgnW1xEk0roaqnMf19YAfre",
            current["text"],
        )
        self.assertEqual(
            [button["label"] for button in current["buttons"]],
            ["Создать новое обращение"],
        )

    def test_every_product_tree_path_is_reachable(self) -> None:
        for insurance_type, classes in PRODUCT_TREE.items():
            self.assertIsInstance(classes, dict)
            self.assertTrue(classes)

            for insurance_class, products in classes.items():
                self.assertIsInstance(products, list)
                self.assertEqual(
                    len(products),
                    len(set(products)),
                    msg=f"Duplicate product in {insurance_type} / {insurance_class}",
                )

                if not products:
                    session = new_session(153, "chat999")
                    submit_action(session, "Другое")
                    submit_text(session, "Тест")
                    submit_action(session, insurance_type)
                    submit_action(session, insurance_class)
                    self.assertEqual(session["current_screen"], "document")
                    continue

                for product in products:
                    with self.subTest(
                        insurance_type=insurance_type,
                        insurance_class=insurance_class,
                        product=product,
                    ):
                        session = new_session(153, "chat999")
                        submit_action(session, "Другое")
                        submit_text(session, "Тест")
                        submit_action(session, insurance_type)

                        current = view(session)
                        self.assertIn(
                            insurance_class,
                            [button["action"] for button in current["buttons"]],
                        )
                        submit_action(session, insurance_class)

                        current = view(session)
                        self.assertIn(
                            product,
                            [button["action"] for button in current["buttons"]],
                        )
                        submit_action(session, product)
                        self.assertEqual(session["current_screen"], "document")

    def test_request_tree_detail_fields_are_defined(self) -> None:
        for path, leaf in iter_request_leaves(REQUEST_TREE):
            for field_name in leaf.get("fields") or []:
                with self.subTest(path=path, field=field_name):
                    self.assertIn(field_name, DETAIL_FIELDS)
                    self.assertIn(field_name, VALID_DETAIL_VALUES)

    def test_instruction_keys_point_to_instruction_routes(self) -> None:
        leaf_routes = {
            path: leaf["route"]
            for path, leaf in iter_request_leaves(REQUEST_TREE)
        }
        direct_instruction_paths = {
            (request_type,)
            for request_type, route in REQUEST_TYPES.items()
            if route == "instruction"
        }

        for path in INSTRUCTIONS:
            with self.subTest(path=path):
                if path in direct_instruction_paths:
                    self.assertEqual(
                        REQUEST_TYPES[path[0]],
                        "instruction",
                    )
                    continue

                self.assertIn(path, leaf_routes)
                self.assertEqual(leaf_routes[path], "instruction")

    def test_old_session_schema_is_forward_filled_without_losing_data(self) -> None:
        old = {
            "user_id": 153,
            "dialog_id": "chat999",
            "request_id": "stable-request-id",
            "current_screen": "confirm",
            "revision": 42,
            "data": {
                "request_type": "Другое",
                "description": "Старое сохранённое обращение",
            },
        }

        normalized = normalize_session(deepcopy(old))

        self.assertEqual(normalized["request_id"], "stable-request-id")
        self.assertEqual(normalized["revision"], 42)
        self.assertEqual(
            normalized["data"]["description"],
            "Старое сохранённое обращение",
        )
        self.assertIn("edit_history", normalized)
        self.assertIn("document_file_ids", normalized["data"])
        self.assertIn("document_file_names", normalized["data"])
        self.assertIn("details", normalized["data"])
        view(normalized)


if __name__ == "__main__":
    unittest.main()
