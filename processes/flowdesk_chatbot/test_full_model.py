"""Checks for the configurable dialog, authenticated editor and durable task delivery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from common.bitrix import BitrixClient, BitrixError
from processes.flowdesk_chatbot import config, constructor_engine as graph, initiatives
from processes.flowdesk_chatbot.bitrix_worker import Runtime, TASK_USER_FIELDS
from processes.flowdesk_chatbot.constructor_schema import condition, seed_document, validate_document
from processes.flowdesk_chatbot.engine import new_session, view
from processes.flowdesk_chatbot.field_templates import task_values, validated_link
from processes.flowdesk_chatbot.flow_store import FlowStore, worker_lock
from processes.flowdesk_chatbot.internal_admin import EditorStore, RevisionConflict
from processes.flowdesk_chatbot.resource_links import ResourceLinks
from processes.flowdesk_chatbot.storage import SessionStore


def screen(key, kind, dest=None, **fields):
    return {"id": key, "kind": kind, "title": key, "label": key, "next": dest,
            "input_mode": "both" if kind in {"text", "number", "date", "files"} else "buttons",
            "required": kind in {"text", "number", "date"}, "options": [], **fields}


def button(key, label=None, dest=None, **fields):
    return {"id": key, "text": label or key, "value": label or key, "next": dest, **fields}


def document(*nodes, **settings):
    return {"format": 1, "name": "Проверка", "start": nodes[0]["id"], "nodes": list(nodes),
            "settings": {"labels": {}, "messages": {}, "styles": {}, **settings}}


def simple(**settings):
    return document(screen("question", "text", "review", bind="description"),
                    screen("review", "review", "done"), screen("done", "end"), **settings)


def start(doc, version=1, uid=17, profile=None):
    return graph.start(new_session(uid, "chat17"), doc, version, profile)


def click(session, label):
    control = next(item for item in graph.render(session)["buttons"] if item["label"] == label)
    return graph.action(session, control["action"])


class ScenarioTests(TestCase):
    def test_imported_catalog_and_forms(self):
        doc = seed_document()
        self.assertEqual(validate_document(doc), [])
        self.assertEqual(len(doc["nodes"]), 102)
        nodes = {node["id"]: node for node in doc["nodes"]}
        self.assertEqual([item["text"] for item in nodes["start"]["options"]], list(config.REQUEST_TYPES))
        self.assertEqual(nodes["initiative_audience"]["kind"], "multi")
        for prefix, questions in (("small", initiatives.SMALL_QUESTIONS), ("large", initiatives.LARGE_QUESTIONS)):
            for item in questions:
                self.assertIn(prefix + "_" + item["key"], nodes)
        self.assertLessEqual({n["registry_field"] for n in nodes.values() if n.get("registry_field")}, set(TASK_USER_FIELDS))

    def test_every_insurance_product(self):
        base = start(seed_document())
        click(base, "Вопрос / Уточнение")
        graph.text(base, "Обращение")
        for group, classes in config.PRODUCT_TREE.items():
            group_state = deepcopy(base)
            click(group_state, group)
            self.assertEqual({o["text"] for o in graph.options(group_state)}, set(classes))
            for cls, products in classes.items():
                current = deepcopy(group_state)
                click(current, cls)
                self.assertEqual({o["text"] for o in graph.options(current)}, set(products))
                for product in products:
                    case = deepcopy(current)
                    click(case, product)
                    self.assertEqual(graph.node(case)["id"], "documents")
                    self.assertEqual(graph.registry_fields(case)["UF_FLOWDESK_PRODUCT"], product)

    def test_request_tree_reaches_all_legacy_leaves(self):
        root = start(seed_document())
        click(root, "Запрос")
        leaves = set()

        def traverse(session, path):
            node = graph.node(session)
            if node["id"] == "description" or node["kind"] == "info":
                leaves.add(path)
            elif node["kind"] == "text":
                answer = "123456789012" if node.get("limits", {}).get("validator") == "bin_iin" else "Уточнение"
                self.assertEqual(graph.text(session, answer)["status"], "ok")
                traverse(session, path)
            else:
                choices = graph.options(session)
                self.assertTrue(choices, node["id"])
                for item in choices:
                    case = deepcopy(session)
                    graph.action(case, "cx:option:" + item["id"])
                    traverse(case, path + (item["text"],))

        def paths(tree, prefix=()):
            if "route" in tree:
                return {prefix}
            return {path for label, child in tree.items() if label != "__prompt__" for path in paths(child, prefix + (label,))}

        traverse(root, ())
        self.assertEqual(leaves, paths(config.REQUEST_TREE))

    def test_conditions_else_skip_and_back(self):
        doc = document(screen("start", "menu", "route", bind="selection", options=[button("yes"), button("no")]),
                       screen("route", "info", "hidden", hidden_router=True, routes=[{"when": condition("selection", "yes"), "next": "shown"}]),
                       screen("hidden", "text", "shown", bind="hidden_answer", enabled=False, skip_next="review"),
                       screen("shown", "text", "review", bind="answer"), screen("review", "review", "done"), screen("done", "end"))
        self.assertEqual(validate_document(doc), [])
        s = start(doc)
        click(s, "yes")
        self.assertEqual(graph.node(s)["id"], "shown")
        click(s, "← Назад")
        self.assertEqual(graph.node(s)["id"], "start")
        click(s, "no")
        self.assertEqual(graph.node(s)["id"], "review")
        self.assertNotIn("hidden_answer", graph.state(s)["answers"])

    def test_field_templates_compose_answers_and_type_values(self):
        doc = simple(mapping={"DESCRIPTION": {"template": "Сотрудник {{system.user_id}}\nОтвет {{description}}"},
                              "PRIORITY": {"template": "2"}, "GROUP_ID": {"template": "14"}, "DEADLINE": {"template": "2026-12-05"}})
        self.assertEqual(validate_document(doc), [])
        s = start(doc)
        graph.text(s, "Подробности")
        values = graph.task_mapping(s)
        self.assertEqual(values, {"DESCRIPTION": "Сотрудник 17\nОтвет Подробности", "PRIORITY": 2, "GROUP_ID": 14, "DEADLINE": "2026-12-05T23:59:59+05:00"})
        self.assertEqual(click(s, "Создать обращение")["status"], "task_ready")

    def test_missing_required_mapping_refuses_submission(self):
        s = start(simple(mapping={"DESCRIPTION": {"template": "{{description}} / {{profile.name}}", "required": True}}))
        graph.text(s, "Есть описание")
        self.assertEqual(click(s, "Создать обращение")["status"], "validation_error")

    def test_invalid_schema_reports_errors(self):
        for mutate in (lambda d: d.update(start=[]), lambda d: d["nodes"][0].update(next="question"),
                       lambda d: d["nodes"][0].update(next="missing"), lambda d: d["nodes"][0].update(title="{{unknown}}"),
                       lambda d: d["nodes"][0].update(limits={"max_length": "oops"}),
                       lambda d: d["nodes"][0].update(options=[button("link", action="link", link="javascript:alert(1)")]),
                       lambda d: d["settings"].update(mapping={"CREATED_BY": {"template": "8"}})):
            d = simple()
            mutate(d)
            self.assertTrue(validate_document(d))

    def test_scalar_mapping_checks(self):
        for field, value in (("PRIORITY", "3"), ("RESPONSIBLE_ID", "0"), ("GROUP_ID", "-1"), ("TITLE", "x" * 251), ("DEADLINE", "2026-02-30")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                task_values({field: {"template": value}}, {})

    def test_custom_buttons_and_text_safety(self):
        doc = document(screen("question", "text", "info", bind="description"),
                       screen("info", "info", "done", title="{{description}}", options=[button("back", action="back"),
                              button("save", action="save_draft"), button("finish", action="finish"),
                              button("send", dest="done", action="create_task"), button("link", action="link", link="https://example.test/help")]),
                       screen("done", "end"))
        self.assertEqual(validate_document(doc), [])
        s = start(doc)
        graph.text(s, "[url=https://example.test]Текст[/url]")
        self.assertIn("［url=", view(s)["text"])
        self.assertFalse(view(s)["accepts_text"])
        self.assertEqual(click(deepcopy(s), "send")["status"], "task_ready")
        self.assertEqual(click(deepcopy(s), "finish")["status"], "information")
        self.assertEqual(click(s, "save")["status"], "draft_saved")
        self.assertEqual(click(s, "Продолжить")["status"], "ok")
        click(s, "back")
        self.assertEqual(graph.node(s)["id"], "question")

    def test_scale_change_keeps_common_answers_and_attachment(self):
        profile = dict(name="Тест", department="Отдел", manager="Руководитель", contact="test@example.test")
        s = start(seed_document(), profile=profile)
        for label in ("Предложение / Идея", "Продолжить", "Небольшая инициатива", "Создать новое", "Процесс", "☐ Клиенты", "☐ Сотрудники", "Продолжить", "В одном подразделении"):
            self.assertEqual(click(s, label)["status"], "ok")
        self.fill_form(s)
        before = deepcopy(graph.state(s)["answers"])
        self.assertEqual(before["initiative.audience"], ["Клиенты", "Сотрудники"])
        graph.action(s, "cx:edit")
        graph.action(s, "cx:edit:initiative.scale")
        click(s, "Крупная инициатива")
        self.assertEqual(self.fill_form(s), ["large_current", "large_bounds", "large_success", "large_owner", "large_resources", "large_risks"])
        after = graph.state(s)["answers"]
        for key in ("initiative.title", "initiative.audience", "initiative.materials", "profile.name"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(s["data"]["document_file_ids"], [71])
        self.assertIn("UF_FLOWDESK_INIT_OPEN", graph.registry_fields(s))

    def fill_form(self, s):
        visited = []
        for _ in range(35):
            n = graph.node(s)
            if n["kind"] == "review":
                return visited
            visited.append(n["id"])
            if n["kind"] == "files":
                self.assertEqual(graph.attachments(s, [{"id": 71, "name": "пример.pdf", "size": 10}], "Пояснение")["status"], "ok")
                result = click(s, "Продолжить")
            elif n["kind"] == "single":
                result = click(s, graph.options(s)[-1 if n["id"] == "large_risks" else 0]["text"])
            else:
                result = graph.text(s, "Ответ " + n["id"])
            self.assertEqual(result["status"], "ok")
        self.fail("Анкета не дошла до проверки")

    def test_rejected_file_replacement_preserves_old_file(self):
        s = start(document(screen("files", "files", "review", bind="document", limits={"extensions": ["pdf"], "max_files": 2, "max_file_mb": 1}),
                           screen("review", "review", "done"), screen("done", "end")))
        graph.attachments(s, [{"id": 4, "name": "old.pdf", "size": 20}], "Пояснение")
        click(s, "Заменить вложения")
        self.assertEqual(graph.attachments(s, [{"id": 5, "name": "bad.exe", "size": 20}])["status"], "validation_error")
        self.assertEqual(s["data"]["document_file_ids"], [4])
        self.assertEqual(graph.attachments(s, [{"id": 6, "name": "new.pdf", "size": 20}])["status"], "ok")
        self.assertEqual(s["data"]["document_file_ids"], [6])
        self.assertEqual(s["data"]["document_comment"], "Пояснение")


class RuntimeTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "state.sqlite3"
        self.r = Runtime.__new__(Runtime)
        self.r.store = self.store = FlowStore(self.db)
        self.r.bot_id = 777
        self.r.command_name = "deskflow_test"
        self.r.portal_base = "https://portal.example"
        self.r.task_user_fields = set(TASK_USER_FIELDS)
        self.r.task_field_catalog = {key: spec["label"] for key, spec in TASK_USER_FIELDS.items()}
        self.r.render_current = Mock()
        self.r.send_attachment_comment = Mock()
        self.store.set_meta("service_chat:17", "chat17")
        self.key = "chat17:17"
        self.s = start(simple())
        graph.text(self.s, "Ответ сохранён")
        self.s["active_message_id"] = 55
        self.store.put_session(self.key, self.s)
        self.rid = self.s["request_id"]
        self.rows, self.adds = [], []
        self.r.call = self.call

    def call(self, method, params):
        if method == "tasks.task.list":
            return {"tasks": self.rows}
        self.assertEqual(method, "tasks.task.add")
        self.adds.append(params["fields"])
        self.rows.append({"id": "321", "xmlId": params["fields"]["XML_ID"]})
        return {"task": {"id": "321"}}

    def command(self, session, action):
        index = next(i for i, item in enumerate(view(session)["buttons"]) if item["action"] == action)
        return {"user": {"id": 17}, "chat": {"dialogId": "chat17"}, "message": {"id": 55, "authorId": 777},
                "command": {"command": self.r.command_name, "params": f"{session['revision']}:{index}", "context": "keyboard"}}

    def test_duplicate_click_and_restart_create_one_task(self):
        cmd = self.command(self.s, "cx:submit")
        self.r.handle_command(cmd)
        self.r.handle_command(cmd)
        self.r.store = FlowStore(self.db)
        self.r.handle_command(cmd)
        self.assertEqual(len(self.adds), 1)
        self.assertEqual(self.adds[0]["CREATED_BY"], 17)
        self.assertEqual(self.store.delivery(self.rid)[:2], ("created", "321"))
        self.assertEqual(self.store.get_session(self.key)["task_id"], "321")

    def test_foreign_chat_manual_and_old_message_cannot_submit(self):
        cmd = self.command(self.s, "cx:submit")
        cmd["chat"]["dialogId"] = "chat18"
        self.r.handle_command(cmd)
        cmd["chat"]["dialogId"] = "chat17"
        cmd["command"]["context"] = "textarea"
        self.r.handle_command(cmd)
        cmd["command"]["context"] = "keyboard"
        cmd["message"]["id"] = 57
        self.r.handle_command(cmd)
        self.assertEqual(self.adds, [])
        self.assertEqual(self.store.get_session(self.key), self.s)

    def test_timeout_after_commit_finds_receipt_without_second_add(self):
        def timeout(method, params):
            result = self.call(method, params)
            if method == "tasks.task.add":
                raise TimeoutError()
            return result
        self.r.call = timeout
        self.r.handle_command(self.command(self.s, "cx:submit"))
        self.assertEqual(self.store.delivery(self.rid)[0], "unknown")
        self.r.store = FlowStore(self.db)
        saved = self.store.get_session(self.key)
        self.r.sync_delivery(saved)
        self.assertNotIn("cx:retry", {b["action"] for b in view(saved)["buttons"]})
        self.r.handle_command(self.command(saved, "cx:check_task"))
        self.assertEqual(len(self.adds), 1)
        self.assertEqual(self.store.delivery(self.rid)[:2], ("created", "321"))

    def test_restart_during_send_does_not_resubmit_unknown(self):
        self.store.begin_delivery(self.key, self.s, None)
        self.assertTrue(self.store.claim_delivery(self.rid))
        self.store.recover_deliveries()
        self.assertEqual(self.r.deliver_task(self.s), "")
        self.assertEqual(self.adds, [])
        self.assertEqual(self.store.delivery(self.rid)[0], "unknown")

    def test_retry_uses_immutable_answers_after_definite_rejection(self):
        def reject(method, params):
            if method == "tasks.task.add":
                raise BitrixError(method, "ACCESS_DENIED", "test", {})
            return self.call(method, params)
        self.r.call = reject
        self.assertEqual(self.r.deliver_task(self.s), "")
        self.assertEqual(self.store.delivery(self.rid)[0], "rejected")
        graph.state(self.s)["answers"]["description"] = "Изменённое значение"
        self.r.call = self.call
        self.assertEqual(self.r.deliver_task(self.s), "321")
        self.assertIn("Ответ сохранён", self.adds[0]["DESCRIPTION"])
        self.assertNotIn("Изменённое значение", self.adds[0]["DESCRIPTION"])

    def test_wrong_lookup_result_does_not_allow_add(self):
        self.rows = [{"id": "12", "xmlId": "unrelated"}]
        self.assertEqual(self.r.deliver_task(self.s), "")
        self.assertEqual(self.adds, [])

    def test_only_one_sender_claims_request(self):
        self.store.begin_delivery(self.key, self.s, None)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.claim_delivery(self.rid), range(12)))
        self.assertEqual(results.count(True), 1)

    def test_hot_publication_rollback_and_draft_ownership(self):
        editor = EditorStore(self.db.with_name("editor.sqlite3"), 17)
        self.r.internal_editor = SimpleNamespace(store=editor)
        first = editor.save(17, simple(), 1, publish=True)
        saved = self.r.restart_session("chat17", 17)
        graph.text(saved, "Старый ответ")
        click(saved, "Сохранить черновик")
        self.store.put_session(self.key, saved)
        changed = simple()
        changed["nodes"][0]["title"] = "Новое сообщение"
        second = editor.save(17, changed, first["revision"], publish=True)
        self.assertIs(self.r.refresh_idle(saved), saved)
        self.assertIsNone(self.store.request_for_user(saved["request_id"], 18, "draft"))
        fresh = self.r.restart_session("chat17", 17)
        self.assertEqual(view(fresh)["text"], "Новое сообщение")
        self.assertEqual(graph.state(fresh)["version"], second["active_version"])
        resumed = self.r.resume_request(fresh, {"status": "draft_resume_requested", "request_id": saved["request_id"]})
        self.assertEqual(graph.state(resumed)["version"], first["active_version"])
        self.assertEqual(graph.state(resumed)["answers"]["description"], "Старый ответ")
        with self.assertRaises(RevisionConflict):
            editor.restore(17, first["active_version"], first["revision"], lambda _: None)
        restored = editor.restore(17, first["active_version"], second["revision"], lambda _: None)
        self.assertGreater(restored["active_version"], second["active_version"])
        self.assertEqual(editor.active()[1], simple())

    def test_legacy_database_and_draft_survive_opening(self):
        old = self.db.with_name("legacy.sqlite3")
        initial = SessionStore(str(old))
        legacy = new_session(17, "chat17")
        initial.put_session(self.key, legacy)
        initial.set_meta("event_offset", "42")
        initial.set_meta("initiative_draft:17", "existing draft")
        upgraded = FlowStore(old)
        self.assertEqual(upgraded.get_session(self.key), legacy)
        self.assertEqual(upgraded.get_meta("event_offset"), "42")
        self.assertEqual(upgraded.get_meta("initiative_draft:17"), "existing draft")

    def test_journal_retains_transition_and_confirmation(self):
        self.store.shown(self.key, self.s, "Вопрос")
        self.store.shown(self.key, self.s, "Вопрос")
        self.store.put_session(self.key, self.s, ("message:8", "text", "review", {"text": "Ответ из события"}))
        self.assertEqual(self.store.journal("Ответ из события")["count"], 1)
        self.r.deliver_task(self.s)
        data = self.store.journal_detail(self.rid)
        self.assertEqual(data["answers"], [("question", "Ответ сохранён")])
        self.assertEqual(data["task_id"], "321")
        self.assertEqual([e["kind"] for e in data["events"]].count("screen"), 1)

    def test_file_event_keeps_disk_id_for_task_and_is_processed_once(self):
        doc = document(screen("materials", "files", "review", bind="document", limits={"max_file_mb": 1}),
                       screen("review", "review", "done"), screen("done", "end"))
        session = start(doc)
        self.store.put_session(self.key, session)
        self.r.call = Mock(return_value={"ID": "71", "NAME": "Материалы.pdf", "SIZE": "12", "DELETED_TYPE": "0"})
        event = {"user": {"id": 17}, "chat": {"dialogId": "chat17"},
                 "message": {"id": 100, "text": "Комментарий", "params": {"FILE_ID": [71]}}}
        self.r.handle_message(event)
        self.r.handle_message(event)
        self.r.call.assert_called_once_with("disk.file.get", {"id": 71})
        saved = self.store.get_session(self.key)
        click(saved, "Продолжить")
        self.assertEqual(self.r.task_fields(saved)["UF_TASK_WEBDAV_FILES"], ["n71"])
        self.assertEqual([item["kind"] for item in self.store.journal_detail(session["request_id"])["events"]], ["files"])

    def test_protected_resource_rejects_forwarded_and_stale_links(self):
        s = start(document(screen("info", "info", "done", options=[button("file", action="download", link="/disk/file/21")]), screen("done", "end")))
        self.store.put_session(self.key, s)
        links = ResourceLinks(b"x" * 32, "https://portal.example", self.store)
        token = parse_qs(urlsplit(links.issue(s, "file")).query)["t"][0]
        self.assertIsNone(links.resolve(18, token))
        self.assertIsNone(links.resolve(17, token[:-1] + ("1" if token[-1] == "0" else "0")))
        self.assertEqual(links.resolve(17, token), "/disk/file/21")
        self.assertEqual(self.store.journal_detail(s["request_id"])["events"][0]["kind"], "download_requested")
        click(s, "Продолжить")
        self.store.put_session(self.key, s)
        self.assertIsNone(links.resolve(17, token))

    def test_bad_resource_targets(self):
        for link in ("//external.test", "/%2fexternal.test", "/disk/../rest/1/key", "https://test@host.test", "https://host.test/?access_token=x", "/disk/x%0a", "/local/tools/deskflow-admin/index.php"):
            with self.subTest(link=link), self.assertRaises(ValueError):
                validated_link(link)

    def test_worker_lock_releases_after_exit(self):
        with worker_lock(self.db):
            with self.assertRaises(OSError), worker_lock(self.db):
                self.fail("Acquired a lock held by another worker")
        with worker_lock(self.db):
            self.assertTrue(self.db.exists())

    def test_task_add_never_retries_or_follows_redirects(self):
        client = BitrixClient("https://portal.example/rest/17/fake/", retries=5, delay=0)
        for code, body in ((429, {"error": "QUERY_LIMIT_EXCEEDED"}), (302, {}), (503, {})):
            response = Mock(status_code=code, content=b"{}")
            response.json.return_value = body
            with patch.object(client.session, "post", return_value=response) as post:
                with self.assertRaises((BitrixError, RuntimeError)):
                    client.call("tasks.task.add", {"fields": {"TITLE": "Test"}})
                self.assertEqual(post.call_count, 1)
                self.assertIs(post.call_args.kwargs["allow_redirects"], False)
