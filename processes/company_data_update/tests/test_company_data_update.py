from __future__ import annotations

import company_data_update as app


def test_spreadsheet_id_from_url() -> None:
    assert app.normalize_spreadsheet_id("https://docs.google.com/spreadsheets/d/abc_DEF-123/edit#gid=0") == "abc_DEF-123"


def test_split_values_semicolon_and_lines() -> None:
    assert app.split_values("+7 111; +7 222\n+7 111") == ["+7 111", "+7 222"]


def test_normalize_bin_restores_one_lost_leading_zero() -> None:
    assert app.normalize_bin("250740900736") == "250740900736"
    assert app.normalize_bin("25 0740 900 736") == "250740900736"
    assert app.normalize_bin("10540005723") == "010540005723"
    assert app.normalize_bin("123") == ""


def test_plan_multifields_adds_only_new_values() -> None:
    company = {
        "PHONE": [{"ID": "1", "VALUE": "+7 700 111 22 33", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"ID": "2", "VALUE": "old@example.kz", "VALUE_TYPE": "WORK"}],
        "WEB": [],
    }
    row = app.SheetRow(
        row_number=2,
        company_id="96",
        origin_id="250740900736",
        name="Test",
        phone="+7 700 111 22 33; +7 701 222 33 44",
        email="old@example.kz; new@example.kz",
        web="https://example.kz",
        phone_type="WORK",
        email_type="WORK",
        web_type="WORK",
        custom_fields={},
    )
    fm, changes, errors = app.plan_multifields(company, row)
    assert not errors
    assert len(fm) == 3
    assert {x["value"] for x in fm} == {"+7 701 222 33 44", "new@example.kz", "https://example.kz"}
    assert len(changes) == 3


def test_parse_rows_understands_russian_headers_and_custom_field() -> None:
    rows = [
        ["COMPANY_ID", "БИН", "Название", "Телефон", "Почта", "UF_CRM_123"],
        ["96", "250740900736", "A", "+77001112233", "a@b.kz", "42"],
    ]
    parsed, errors = app.parse_rows(rows, {"UF_CRM_123": {"type": "string", "isMultiple": False}})
    assert not errors
    assert parsed[0].company_id == "96"
    assert parsed[0].origin_id == "250740900736"
    assert parsed[0].custom_fields == {"UF_CRM_123": "42"}


def test_parse_rows_ignores_web_search_status_column() -> None:
    rows = [
        ["COMPANY_ID", "ORIGIN_ID", "NAME", "PHONE", "EMAIL", "WEB"],
        ["96", "250740900736", "A", "+77001112233", "a@b.kz", "Найдено"],
        ["108", "231040024610", "B", "—", "—", "Не найдено"],
    ]
    parsed, errors = app.parse_rows(rows, {})
    assert not errors
    assert [row.web for row in parsed] == ["", ""]


def test_build_fm_payload_uses_bitrix_new_value_keys() -> None:
    items = [
        {"typeId": "PHONE", "valueType": "WORK", "value": "+77001112233"},
        {"typeId": "EMAIL", "valueType": "WORK", "value": "a@b.kz"},
    ]
    assert app.build_fm_payload(items) == {
        "n0": items[0],
        "n1": items[1],
    }
