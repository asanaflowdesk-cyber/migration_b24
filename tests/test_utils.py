import json

from bitrix_export import (
    RawRecorder,
    build_entity_select,
    collect_communications,
    field_codes_from_payload,
    flatten_field_catalog,
    query_pairs,
    write_json_bundle,
    ExportLog,
)


def test_flatten_field_catalog():
    rows = flatten_field_catalog({"ID": {"type": "integer", "title": "ID"}})
    assert rows == [{"FIELD_CODE": "ID", "type": "integer", "title": "ID"}]


def test_query_pairs():
    pairs = query_pairs("select", ["ID", "TITLE"])
    assert pairs == [("select[0]", "ID"), ("select[1]", "TITLE")]


def test_collect_communications():
    rows = collect_communications(
        "CONTACT",
        [{"ID": 10, "PHONE": [{"VALUE": "+77000000000", "VALUE_TYPE": "WORK"}]}],
    )
    assert rows[0]["ENTITY_ID"] == 10
    assert rows[0]["VALUE_TYPE"] == "WORK"


def test_dynamic_select_includes_multiple_fields():
    payload = {"ID": {"type": "integer"}, "UF_CRM_X": {"type": "string"}}
    assert field_codes_from_payload(payload) == ["ID", "UF_CRM_X"]
    assert build_entity_select(payload, ["PHONE", "EMAIL"]) == ["ID", "UF_CRM_X", "PHONE", "EMAIL"]


def test_raw_recorder_and_json_bundle(tmp_path):
    recorder = RawRecorder(tmp_path / "json" / "raw_api")
    recorder.write("crm.deal.list", {"start": 0}, {"result": [{"ID": "1"}], "total": 1})
    recorder.close()
    assert json.loads((tmp_path / "json" / "raw_api" / "index.json").read_text())[0]["method"] == "crm.deal.list"

    log = ExportLog()
    log.add("Deals", "crm.deal.list", "OK", 1)
    write_json_bundle(
        tmp_path,
        {"Deals": [{"ID": "1", "TITLE": "Test"}]},
        log,
        generated_at="2026-07-31T18:00:00+05:00",
        portal_hint="https://example.bitrix24.kz",
        config={"export_relations": True},
    )
    assert json.loads((tmp_path / "json" / "datasets" / "Deals.json").read_text())[0]["TITLE"] == "Test"
    assert (tmp_path / "manifest.json").exists()


class _FakePagingClient:
    """Минимальный объект для проверки защитной пагинации без сети."""

    max_pages = 10
    progress_every_pages = 5

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def call(self, method, params):
        self.calls += 1
        index = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[index]


def test_list_all_stops_on_repeated_page_without_duplicates():
    from bitrix_export import BitrixClient

    page = [{"ID": str(i)} for i in range(1, 51)]
    fake = _FakePagingClient([
        {"result": page},
        {"result": page},  # метод проигнорировал start
    ])
    rows = BitrixClient.list_all(fake, "methods", {})
    assert len(rows) == 50
    assert fake.calls == 2


def test_list_all_stops_by_total_on_full_page():
    from bitrix_export import BitrixClient

    page = [{"ID": str(i)} for i in range(1, 51)]
    fake = _FakePagingClient([{"result": page, "total": 50}])
    rows = BitrixClient.list_all(fake, "crm.status.list", {})
    assert len(rows) == 50
    assert fake.calls == 1
