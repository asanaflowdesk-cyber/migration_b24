from __future__ import annotations

from eqazyna_bitrix.bitrix_client import BitrixClient


class FakeResponse:
    status_code = 200
    reason = "OK"
    text = ""

    def __init__(self, result):
        self._result = result

    def json(self):
        return {"result": self._result}


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.headers = {}

    def post(self, url, json, timeout, verify):
        self.calls.append((url, json))
        return FakeResponse(self.results.pop(0))


def test_find_lead_falls_back_to_legacy_migrated_origin():
    session = FakeSession(
        [
            [],
            [
                {
                    "ID": "901",
                    "ORIGINATOR_ID": "EQAZYNA",
                    "ORIGIN_ID": "eQazyna|42480-NEA|123456789012",
                }
            ],
        ]
    )
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token",
        polite_delay_seconds=0,
        session=session,
    )

    lead = client.find_lead_by_origin("123456789012")

    assert lead["ID"] == "901"
    assert len(session.calls) == 2
    first_filter = session.calls[0][1]["filter"]
    second_filter = session.calls[1][1]["filter"]
    assert first_filter == {
        "ORIGINATOR_ID": "EQAZYNA_LEAD",
        "ORIGIN_ID": "123456789012",
    }
    assert second_filter == {
        "ORIGINATOR_ID": "EQAZYNA",
        "%ORIGIN_ID": "123456789012",
    }


def test_discover_company_requisite_preset_uses_most_common_existing_value():
    session = FakeSession(
        [[
            {"ID": "1", "PRESET_ID": "1"},
            {"ID": "2", "PRESET_ID": "1"},
            {"ID": "3", "PRESET_ID": "3"},
        ]]
    )
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token",
        polite_delay_seconds=0,
        session=session,
    )

    assert client.discover_company_requisite_preset_id() == 1
    assert session.calls[0][1]["filter"] == {"ENTITY_TYPE_ID": 4}


def test_find_lead_by_application_prefers_exact_application_origin():
    session = FakeSession(
        [[
            {
                "ID": "902",
                "ORIGINATOR_ID": "EQAZYNA_LEAD",
                "ORIGIN_ID": "47408-NEA",
            }
        ]]
    )
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token",
        polite_delay_seconds=0,
        session=session,
    )

    lead = client.find_lead_by_application("47408-NEA", "123456789012")

    assert lead["ID"] == "902"
    assert len(session.calls) == 1
    assert session.calls[0][1]["filter"] == {
        "ORIGINATOR_ID": "EQAZYNA_LEAD",
        "ORIGIN_ID": "47408-NEA",
    }


def test_find_lead_by_application_falls_back_to_legacy_comment():
    session = FakeSession(
        [
            [],
            [],
            [],
            [],
            [
                {
                    "ID": "903",
                    "COMMENTS": "Номер заявки: 47408-NEA",
                }
            ],
        ]
    )
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token",
        polite_delay_seconds=0,
        session=session,
    )

    lead = client.find_lead_by_application("47408-NEA", "123456789012")

    assert lead["ID"] == "903"
    assert session.calls[-1][1]["filter"] == {
        "%COMMENTS": "Номер заявки: 47408-NEA",
    }



def test_find_company_by_origin_falls_back_to_migrated_origin_without_originator():
    session = FakeSession(
        [
            [],
            [
                {
                    "ID": "120",
                    "TITLE": "ТОО Тест Недра",
                    "ORIGIN_ID": "123456789012",
                    "ORIGINATOR_ID": "",
                }
            ],
        ]
    )
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token",
        polite_delay_seconds=0,
        session=session,
    )

    company = client.find_company_by_origin("123456789012")

    assert company["ID"] == "120"
    assert len(session.calls) == 2
    assert session.calls[0][1]["filter"] == {
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    assert session.calls[1][1]["filter"] == {
        "=ORIGIN_ID": "123456789012",
    }


def test_ensure_contact_company_link_keeps_existing_primary_and_adds_secondary():
    session = FakeSession(
        [
            [{"COMPANY_ID": "555", "IS_PRIMARY": "Y"}],
            True,
            [
                {"COMPANY_ID": "555", "IS_PRIMARY": "Y"},
                {"COMPANY_ID": "601", "IS_PRIMARY": "N"},
            ],
        ]
    )
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token",
        polite_delay_seconds=0,
        session=session,
    )

    changed = client.ensure_contact_company_link("801", "601")

    assert changed is True
    assert session.calls[1][1] == {
        "id": 801,
        "fields": {
            "COMPANY_ID": 601,
            "IS_PRIMARY": "N",
        },
    }


def test_global_founder_search_accepts_founder_director_and_case_insensitive_marker():
    for extra in (
        {"POST": "Учредитель"},
        {"POST": "Генеральный директор"},
        {"POST": "Руководитель"},
        {"COMMENTS": "[[eqazyna_director:123456789012]]"},
    ):
        session = FakeSession([[
            {"ID": "1", "POST": "Менеджер", "ASSIGNED_BY_ID": "99"},
            {"ID": "2", "ASSIGNED_BY_ID": "13", **extra},
        ]])
        client = BitrixClient(
            "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
        )

        contact = client.find_director_contact_global("Иванов", "Иван", "Иванович")

        assert contact["ID"] == "2"
        assert contact["ASSIGNED_BY_ID"] == "13"


def test_missing_fio_uses_only_marked_company_founder_contact():
    session = FakeSession([[
        {"ID": "1", "POST": "Бухгалтер", "ASSIGNED_BY_ID": "99"},
        {"ID": "2", "POST": "Учредитель", "NAME": "Иван", "ASSIGNED_BY_ID": "13"},
    ]])
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
    )

    contact = client.find_company_director_contact("601")

    assert contact["ID"] == "2"
    assert session.calls[0][1]["filter"] == {"COMPANY_ID": 601}


def test_missing_fio_does_not_guess_between_conflicting_founders():
    import pytest
    from eqazyna_bitrix.bitrix_client import BitrixError

    session = FakeSession([[
        {"ID": "1", "POST": "Учредитель", "NAME": "Иван", "ASSIGNED_BY_ID": "13"},
        {"ID": "2", "POST": "Директор", "NAME": "Петр", "ASSIGNED_BY_ID": "88"},
    ]])
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
    )

    with pytest.raises(BitrixError, match="несколько контактов"):
        client.find_company_director_contact("601")


def test_global_founder_search_prefers_real_manager_over_technical_contact():
    session = FakeSession([[
        {"ID": "1", "POST": "Учредитель", "ASSIGNED_BY_ID": "4"},
        {"ID": "2", "POST": "Учредитель", "ASSIGNED_BY_ID": "13"},
    ]])
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
    )

    assert client.find_director_contact_global("Иванов", "Иван")["ASSIGNED_BY_ID"] == "13"


def _director(item_id, owner, second_name="Иванович"):
    return {
        "ID": str(item_id), "ASSIGNED_BY_ID": str(owner), "POST": "Учредитель",
        "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": second_name,
    }


def test_global_duplicate_uses_lead_linked_contact_instead_of_old_rop():
    session = FakeSession([
        [_director(10, 72), _director(20, 13)],
        [],
        [{"ID": "99", "CONTACT_ID": "20", "ASSIGNED_BY_ID": "13"}],
    ])
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
    )

    contact = client.find_director_contact_global("Иванов", "Иван", "Иванович")

    assert contact["ID"] == "20"
    assert contact["ASSIGNED_BY_ID"] == "13"


def test_global_duplicate_with_two_linked_owners_is_not_arbitrarily_assigned():
    import pytest
    from eqazyna_bitrix.bitrix_client import BitrixError

    session = FakeSession([
        [_director(10, 72), _director(20, 13)],
        [{"ID": "98", "CONTACT_ID": "10"}],
        [{"ID": "99", "CONTACT_ID": "20"}],
    ])
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
    )

    with pytest.raises(BitrixError, match="разные ответственные"):
        client.find_director_contact_global("Иванов", "Иван", "Иванович")


def test_incomplete_fio_does_not_merge_people_with_different_patronymics():
    import pytest
    from eqazyna_bitrix.bitrix_client import BitrixError

    session = FakeSession([[_director(10, 72), _director(20, 13, "Петрович")]])
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
    )

    with pytest.raises(BitrixError, match="ФИО"):
        client.find_director_contact_global("Иванов", "Иван")


def test_company_specific_duplicate_uses_lead_linked_founder_owner():
    session = FakeSession([
        [_director(10, 72), _director(20, 13)],
        [],
        [{"ID": "99", "CONTACT_ID": "20"}],
    ])
    client = BitrixClient(
        "https://box.example.invalid/rest/1/token", polite_delay_seconds=0, session=session,
    )

    contact = client.find_director_contact("601", "Иванов", "Иван", "Иванович")

    assert contact["ID"] == "20"
