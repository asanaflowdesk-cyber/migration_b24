import json
from pathlib import Path

from common.bitrix import BitrixClient
from src.dump_reader import DumpReader
from src.file_transfer import FileTransfer
from src.live_source import LiveCloudSource
from src.migration import MigrationProject, migration_marker, normalize_name_tokens, parse_marker, resolve_requisite_preset
from src.reporting import Report

ROOT = Path(__file__).resolve().parents[1]
DUMP = ROOT / "input/bitrix24_dump_20260805_072425.zip"


def project(tmp_path: Path) -> MigrationProject:
    return MigrationProject(
        DUMP,
        ROOT / "config/migration.json",
        ROOT / "config/users.csv",
        tmp_path,
        target_client=None,
    )


def test_dump_dataset_counts() -> None:
    expected = {
        "Companies": 604,
        "Contacts": 444,
        "Leads": 4,
        "Deals": 1243,
        "Requisites": 619,
        "Addresses": 1160,
        "Tasks": 597,
        "CRM_Activities": 134,
    }
    with DumpReader(DUMP) as reader:
        for name, count in expected.items():
            assert len(reader.rows(name)) == count


def test_routing_counts(tmp_path: Path) -> None:
    p = project(tmp_path)
    p.load_source("Deals")
    routes = [p.route_source_deal(row)[0] for row in p._source["Deals"]]
    assert routes.count("lead") == 1233
    assert routes.count("deal") == 10


def test_plan_expected_counts(tmp_path: Path) -> None:
    plan = project(tmp_path).source_plan()
    assert plan["expected_target_leads_total"] == 1233
    assert plan["expected_target_deals_total"] == 10
    assert plan["expected_tasks"] == 589
    assert plan["skipped_tasks"] == 8
    assert plan["skipped_tasks_by_source_user"] == {"10": 5, "28": 3}
    assert plan["expected_activities"] == 134
    assert plan["source_counts"]["Leads"] == 4



def test_context_user_assignment_and_task_exclusions(tmp_path: Path) -> None:
    p = project(tmp_path)
    assert p._context_user_target("36", "crm", {}) == 21
    assert p._context_user_target("36", "task", {}) == 4
    assert p._context_user_target("96", "crm", {}) == 21
    assert p._context_user_target("96", "task", {}) == 4

    p.load_source("Tasks")
    skipped = [row for row in p._source["Tasks"] if p._task_skip_reason(row)]
    assert len(skipped) == 8
    assert {user for row in skipped for user in p._task_skip_users(row)} == {"10", "28"}
    assert not any("36" in p._task_skip_users(row) for row in p._source["Tasks"])

def test_converted_test_lead_aliases(tmp_path: Path) -> None:
    aliases = project(tmp_path)._converted_lead_to_deal()
    assert aliases == {"92": "2386", "94": "2808", "96": "2910", "98": "2812"}


def test_excluded_lead_relation_follows_converted_deal(tmp_path: Path) -> None:
    p = project(tmp_path)
    assert p._map_crm_ref("L_98", {}, {}, {"DEAL:2812:LEAD": 7001}, {}) == "L_7001"
    assert p._map_crm_ref("L_98", {}, {}, {}, {"2812": 8001}) == "D_8001"


def test_name_matching_ignores_order_case_spaces_and_yo() -> None:
    assert normalize_name_tokens("Иван", "Иванов") == normalize_name_tokens("  ИВАНОВ ", "иван")
    assert normalize_name_tokens("Алёна", "Сачёва") == normalize_name_tokens("сачева", "алена")


def test_marker_round_trip() -> None:
    marker = migration_marker("DEAL", 123, "LEAD")
    assert parse_marker("x " + marker + " y") == ("DEAL", "123", "LEAD")


def test_rest_v3_webhook_base() -> None:
    client = BitrixClient("https://example.test/rest/7/test/")
    assert client.api_v3_base == "https://example.test/rest/api/7/test/"


def test_file_name_is_deterministic() -> None:
    first = FileTransfer._migration_filename("attached:123", "document.pdf")
    second = FileTransfer._migration_filename("attached:123", "document.pdf")
    other = FileTransfer._migration_filename("attached:124", "document.pdf")
    assert first == second
    assert first != other
    assert first.endswith("__document.pdf")


def test_product_field_configuration(tmp_path: Path) -> None:
    p = project(tmp_path)
    assert p.config["product_field"] == {
        "lead_code": "UF_CRM_1785917145255",
        "deal_code": "UF_CRM_6A73073A5405E",
        "value": "ГПО недропользователя",
    }


def test_chat_files_are_merged_into_matching_message() -> None:
    payload = {
        "messages": [
            {
                "id": 101,
                "author_id": 7,
                "date": "2026-08-05T10:00:02+00:00",
                "text": "Документ",
                "params": {"FILE_ID": [5255]},
            },
            {
                "id": 100,
                "author_id": 8,
                "date": "2026-08-05T09:59:00+00:00",
                "text": "Без файла",
                "params": {},
            },
        ],
        "files": [
            {
                "id": 5255,
                "authorId": 7,
                "date": "2026-08-05T10:00:00+00:00",
                "name": "document.pdf",
                "urlDownload": "https://example.test/document.pdf",
            }
        ],
    }
    rows = MigrationProject._merge_chat_page_files(payload)
    assert rows[0]["files"][0]["id"] == 5255
    assert "files" not in rows[1]


def test_chat_file_fallback_uses_author_and_nearest_time() -> None:
    payload = {
        "messages": [
            {"id": 101, "author_id": 7, "date": "2026-08-05T10:00:02+00:00", "text": "A", "params": {}},
            {"id": 100, "author_id": 7, "date": "2026-08-05T09:50:00+00:00", "text": "B", "params": {}},
        ],
        "files": [
            {
                "id": 5255,
                "authorId": 7,
                "date": "2026-08-05T10:00:00+00:00",
                "name": "document.pdf",
                "urlDownload": "https://example.test/document.pdf",
            }
        ],
    }
    rows = MigrationProject._merge_chat_page_files(payload)
    assert rows[0]["files"][0]["id"] == 5255
    assert "files" not in rows[1]


class _TaskChatClient:
    def __init__(self) -> None:
        self.classic_calls = 0
        self.v3_calls = 0

    def call(self, method, params=None):
        assert method == "tasks.task.get"
        self.classic_calls += 1
        return {"task": {"id": "2", "chatId": None}}

    def call_v3(self, method, params=None):
        assert method == "tasks.task.get"
        self.v3_calls += 1
        return {"item": {"id": 2, "chat": {"id": 777, "entityId": 2, "entityType": "TASKS_TASK"}}}


def test_task_chat_id_falls_back_to_rest_v3() -> None:
    client = _TaskChatClient()
    assert MigrationProject._task_chat_id(client, 2) == 777
    assert client.classic_calls == 1
    assert client.v3_calls == 1


class _ClassicTaskChatClient:
    def call(self, method, params=None):
        assert method == "tasks.task.get"
        return {"task": {"id": "2", "chatId": 123}}

    def call_v3(self, method, params=None):
        raise AssertionError("REST 3.0 should not be called when classic REST returns chatId")


def test_task_chat_id_prefers_classic_rest() -> None:
    assert MigrationProject._task_chat_id(_ClassicTaskChatClient(), 2) == 123


class _UnreadableCommentsSourceClient:
    def call(self, method, params=None):
        if method == "user.current":
            return {"ID": "1"}
        if method == "tasks.task.get":
            return {"task": {"id": str((params or {}).get("taskId", 2)), "chatId": 3918}}
        if method == "task.commentitem.getlist":
            raise RuntimeError("old comments API unavailable")
        if method == "im.dialog.messages.get":
            return {"messages": [], "files": []}
        if method == "disk.attachedObject.get":
            return {"ID": str((params or {}).get("id", 1)), "OBJECT_ID": "10"}
        if method == "crm.activity.get":
            return {"ID": str((params or {}).get("id", 1))}
        if method == "crm.activity.binding.list":
            return []
        raise AssertionError(f"Unexpected method: {method}")

    def call_v3(self, method, params=None):
        if method == "tasks.task.get":
            return {"item": {"id": (params or {}).get("id", 2), "chat": {"id": 3918}}}
        raise AssertionError(f"Unexpected v3 method: {method}")


def test_live_source_unreadable_comments_are_non_blocking(tmp_path: Path) -> None:
    p = project(tmp_path)
    p.source_client = _UnreadableCommentsSourceClient()
    result = p.validate_live_source()
    assert result["ok"] is True
    assert not result["errors"]
    assert any("source_task_comments" in warning for warning in result["warnings"])
    assert str(result["checks"]["source_task_comments"]).startswith("WARN:")


def test_missing_task_comments_are_reported_as_warning(tmp_path: Path) -> None:
    p = project(tmp_path)
    p.source_client = object()
    p.client = object()
    p._source["Tasks"] = [
        {"id": "2", "commentsCount": 3, "serviceCommentsCount": 1}
    ]
    p._fetch_task_comments = lambda client, task_id: []  # type: ignore[method-assign]
    p._import_task_comments("2", 2002, {})
    row = p.report.actions[-1]
    assert row["operation"] == "copy_task_comments"
    assert row["status"] == "WARN"
    assert "2 comments reported" in row["message"]


def test_invalid_file_reference_is_non_blocking_warning(tmp_path: Path) -> None:
    p = project(tmp_path)
    transfer = FileTransfer(object(), object(), p.report)
    assert transfer.transfer_reference("not-a-file") is None
    row = p.report.actions[-1]
    assert row["operation"] == "transfer_file"
    assert row["status"] == "WARN"


def test_unresolved_activity_communications_are_omitted_with_note(tmp_path: Path) -> None:
    p = project(tmp_path)
    rows = [
        {
            "TYPE": "PHONE",
            "VALUE": "+77011110329",
            "ENTITY_TYPE_ID": "4",
            "ENTITY_ID": "730",
        },
        {
            "TYPE": "PHONE",
            "VALUE": "+77010000000",
            "ENTITY_TYPE_ID": "3",
            "ENTITY_ID": "20",
        },
    ]
    mapped, unresolved = p._map_activity_communications(
        rows,
        company_map={},
        contact_map={"20": 2000},
        lead_map={},
        deal_map={},
    )
    assert unresolved == ["4:730"]
    assert len(mapped) == 1
    assert mapped[0]["ENTITY_TYPE_ID"] == 3
    assert mapped[0]["ENTITY_ID"] == 2000
    note = p._unresolved_communications_note(rows, unresolved)
    assert "+77011110329" in note
    assert "4:730" in note


def test_report_writes_skipped_and_warning_files(tmp_path: Path) -> None:
    report = Report(tmp_path)
    report.add("create_task", "TASK", "1", "TASK", "", "SKIP", "not processed")
    report.add("copy_comment", "TASK", "1", "TASK", "", "WARN", "comment unavailable")
    report.add("system", "SYSTEM", "", "SYSTEM", "", "FATAL", "global failure")
    report.save()

    skipped = (tmp_path / "skipped.csv").read_text(encoding="utf-8-sig")
    warnings = (tmp_path / "warnings.csv").read_text(encoding="utf-8-sig")
    errors = (tmp_path / "errors.csv").read_text(encoding="utf-8-sig")
    assert "not processed" in skipped
    assert "comment unavailable" in warnings
    assert "global failure" in errors
    assert "not processed" not in errors


def test_dry_run_dependency_problem_is_skip_not_error(tmp_path: Path) -> None:
    p = project(tmp_path)
    p.source_client = object()
    p._source["Tasks"] = [
        {
            "id": "999",
            "title": "Broken dependency",
            "createdBy": "9999",
            "responsibleId": "9999",
            "accomplices": [],
            "auditors": [],
            "parentId": "0",
            "ufCrmTask": [],
            "ufTaskWebdavFiles": [],
            "commentsCount": 0,
            "serviceCommentsCount": 0,
        }
    ]
    p._source["CRM_Activities"] = []
    p._dry_run_tasks_activities({}, {}, {}, {}, {})
    row = p.report.actions[-1]
    assert row["status"] == "SKIP"
    assert row["message"].startswith("Пропущено:")


def test_skip_and_log_policy_has_no_blocking_import_exit() -> None:
    source = (ROOT / "migrate.py").read_text(encoding="utf-8")
    assert "exit_code = 4" not in source
    assert 'row["status"] = "SKIP"' in source
    assert "exit_code = 0" in source


def test_report_writes_full_payload_previews(tmp_path: Path) -> None:
    report = Report(tmp_path)
    report.add_transfer(
        operation="create_contact",
        source_type="CONTACT",
        source_id="44",
        target_type="CONTACT",
        target_id=-1,
        status="DRY_RUN",
        route="CONTACT",
        payload={
            "NAME": "Иван",
            "LAST_NAME": "Иванов",
            "PHONE": [{"VALUE": "+77010000000", "VALUE_TYPE": "WORK"}],
            "EMAIL": [{"VALUE": "ivan@example.kz", "VALUE_TYPE": "WORK"}],
            "ADDRESS": "Алматы",
        },
    )
    report.save()

    wide = (tmp_path / "preview_contact.csv").read_text(encoding="utf-8-sig")
    assert "+77010000000" in wide
    assert "ivan@example.kz" in wide
    assert "Алматы" in wide

    full = json.loads((tmp_path / "full_transfer_preview.json").read_text(encoding="utf-8"))
    assert full[0]["payload"]["PHONE"][0]["VALUE"] == "+77010000000"
    assert full[0]["payload"]["ADDRESS"] == "Алматы"

    index = (tmp_path / "preview_index.csv").read_text(encoding="utf-8-sig")
    assert "CONTACT" in index
    assert "preview_contact.csv" in index


def test_dump_is_checkpoint_not_excel_primary() -> None:
    with DumpReader(DUMP) as reader:
        assert len(reader.rows("Companies")) == 604
        company = next(row for row in reader.rows("Companies") if row.get("PHONE"))
        assert isinstance(company["PHONE"], list)
        assert company["PHONE"][0]["VALUE"]


class _LiveCompanyClient:
    base = "https://source.example/rest/1/token/"

    def call(self, method, params=None):
        assert method == "crm.company.fields"
        return {"ID": {}, "TITLE": {}, "PHONE": {}, "EMAIL": {}}

    def list_all(self, method, params=None):
        assert method == "crm.company.list"
        return [{
            "ID": "9",
            "TITLE": "Live company",
            "PHONE": [{"VALUE": "+77010000000", "VALUE_TYPE": "WORK"}],
            "EMAIL": [{"VALUE": "live@example.kz", "VALUE_TYPE": "WORK"}],
        }]


def test_live_cloud_source_returns_full_card_fields() -> None:
    reader = LiveCloudSource(_LiveCompanyClient())
    rows = reader.rows("Companies")
    assert reader.manifest()["source_mode"] == "direct_cloud_api"
    assert rows[0]["TITLE"] == "Live company"
    assert rows[0]["PHONE"][0]["VALUE"] == "+77010000000"
    assert rows[0]["EMAIL"][0]["VALUE"] == "live@example.kz"




def test_migration_project_prefers_live_source_over_dump(tmp_path: Path) -> None:
    p = MigrationProject(
        DUMP,
        ROOT / "config/migration.json",
        ROOT / "config/users.csv",
        tmp_path,
        target_client=None,
        source_client=_LiveCompanyClient(),
    )
    p.load_source("Companies")
    assert p._source["Companies"][0]["TITLE"] == "Live company"
    assert p.report.extra["source_dataset_origins"]["Companies"] == "live_cloud_api"


def test_same_code_user_field_is_included_in_target_payload(tmp_path: Path) -> None:
    p = project(tmp_path)
    p._target_fields["company"] = {
        "TITLE": {},
        "UF_CRM_SHARED": {},
    }
    fields = p._copy_standard_fields(
        "company",
        {"TITLE": "Company", "UF_CRM_SHARED": "value", "UF_CRM_MISSING": "drop"},
    )
    assert fields == {"TITLE": "Company", "UF_CRM_SHARED": "value"}


def test_dry_run_builds_requisites_relations_tasks_and_activities(tmp_path: Path) -> None:
    p = object.__new__(MigrationProject)
    p.client = object()
    p.source_client = object()
    p.report = Report(tmp_path)
    p.file_transfer = None

    p.discover_target = lambda: None
    p.validate_target = lambda: {"ok": True}
    p.validate_live_source = lambda: {"ok": True}
    p.build_user_map = lambda strict=True: {"1": 101}
    p.prepare_companies = lambda user_map: [("COMPANY:1:COMPANY", {"TITLE": "C"})]
    p.prepare_contacts = lambda user_map, company_map: [("CONTACT:2:CONTACT", {"NAME": "N"})]
    p.prepare_original_leads = lambda user_map, company_map, contact_map: []
    p.prepare_routed_deal_leads = lambda user_map, company_map, contact_map: [("DEAL:3:LEAD", {"TITLE": "L"})]
    p.prepare_deals = lambda user_map, company_map, contact_map: [("DEAL:4:DEAL", {"TITLE": "D"})]

    def fake_batch(entity, prepared, *, dry_run, max_items=0):
        assert dry_run is True
        return {key: -(index + 1) for index, (key, _fields) in enumerate(prepared)}

    p._batch_create = fake_batch
    calls: list[str] = []
    p._dry_run_requisites_and_addresses = lambda company_map, contact_map, max_items=0: calls.append("requisites")
    p._record_crm_relation_registry = lambda company_map, contact_map, lead_map, deal_map, status: calls.append("relations")
    p._dry_run_tasks_activities = lambda user_map, company_map, contact_map, lead_map, deal_map, max_items=0: calls.append("tasks_activities")

    p.import_all(dry_run=True, max_items=0)
    assert calls == ["requisites", "relations", "tasks_activities"]


def test_requisite_preset_maps_legal_entity_alias() -> None:
    target = [
        {"ID": "1", "NAME": "Организация", "ENTITY_TYPE_ID": "4", "XML_ID": ""},
        {"ID": "3", "NAME": "Физ. лицо", "ENTITY_TYPE_ID": "3", "XML_ID": ""},
    ]
    preset_id, reason = resolve_requisite_preset(
        {"NAME": "Юр. лицо", "XML_ID": ""},
        "4",
        target,
    )
    assert preset_id == 1
    assert reason == "semantic alias"


def test_requisite_preset_uses_unique_owner_type() -> None:
    target = [
        {"ID": "7", "NAME": "Контрагент KZ", "ENTITY_TYPE_ID": "4", "XML_ID": ""},
        {"ID": "3", "NAME": "Физ. лицо", "ENTITY_TYPE_ID": "3", "XML_ID": ""},
    ]
    preset_id, reason = resolve_requisite_preset(
        {"NAME": "Неизвестное название", "XML_ID": ""},
        "4",
        target,
    )
    assert preset_id == 7
    assert reason == "unique owner type"


def test_requisite_preset_does_not_guess_multiple_company_presets() -> None:
    target = [
        {"ID": "1", "NAME": "Организация", "ENTITY_TYPE_ID": "4", "XML_ID": ""},
        {"ID": "2", "NAME": "ИП", "ENTITY_TYPE_ID": "4", "XML_ID": ""},
    ]
    preset_id, reason = resolve_requisite_preset(
        {"NAME": "Неизвестное название", "XML_ID": ""},
        "4",
        target,
    )
    assert preset_id is None
    assert "ambiguous target presets" in reason
