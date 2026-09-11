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
