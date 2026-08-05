from pathlib import Path

from common.bitrix import BitrixClient
from src.dump_reader import DumpReader
from src.file_transfer import FileTransfer
from src.migration import MigrationProject, migration_marker, normalize_name_tokens, parse_marker

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
