from __future__ import annotations

from eqazyna_bitrix.models import Application
from sync_eqazyna_statuses import (
    StatusSyncError,
    extract_application_numbers,
    map_external_status,
    resolve_lead_stage,
    sync_statuses,
)


STATUSES = [
    {"STATUS_ID": "NEW", "NAME": "Новый", "SEMANTICS": ""},
    {"STATUS_ID": "UC_POTENTIAL", "NAME": "Потенциальная сделка", "SEMANTICS": ""},
    {"STATUS_ID": "JUNK", "NAME": "Провал", "SEMANTICS": "F"},
]


class FakeClient:
    def __init__(self, leads):
        self.leads = list(leads)
        self.updated = []
        self.list_filters = []

    def list_lead_statuses(self):
        return list(STATUSES)

    def list_all(self, method, payload):
        assert method == "crm.lead.list"
        originator = payload["filter"]["ORIGINATOR_ID"]
        self.list_filters.append(originator)
        return [row for row in self.leads if row.get("ORIGINATOR_ID") == originator]

    def update_lead(self, lead_id, fields):
        self.updated.append((str(lead_id), dict(fields)))


class FakeScraper:
    def __init__(self, statuses):
        self.statuses = dict(statuses)
        self.calls = []

    def fetch_application_by_number(self, doc_number, doc_type):
        self.calls.append((doc_number, doc_type))
        status = self.statuses.get(doc_number)
        if status is None:
            return None
        return Application(
            created_at_raw="10.09.2026 10:00:00",
            doc_number=doc_number,
            bin="123456789012",
            applicant_name="Test LLP",
            doc_type=doc_type,
            status=status,
            source_url="https://example.test",
        )


def lead(lead_id, doc_number, status_id="NEW", *, originator="EQAZYNA_LEAD"):
    return {
        "ID": str(lead_id),
        "TITLE": f"ТОО Test. e-Qazyna № {doc_number}",
        "COMMENTS": f"Номер заявки: {doc_number}",
        "STATUS_ID": status_id,
        "ORIGINATOR_ID": originator,
        "ORIGIN_ID": doc_number,
    }


def test_external_status_mapping():
    assert map_external_status("Отклонено") == "failure"
    assert map_external_status("Отозвано") == "failure"
    assert map_external_status("Аннулировано") == "failure"
    assert map_external_status("Выдана лицензия") == "potential"
    assert map_external_status("Принято") is None
    assert map_external_status("Завершено") is None


def test_extract_application_number_from_canonical_origin():
    assert extract_application_numbers(lead(1, "47408-NEA")) == ["47408-NEA"]


def test_extract_application_number_from_legacy_composite_origin():
    row = {
        "ORIGIN_ID": "eQazyna|47408-NEA|123456789012",
        "TITLE": "Legacy lead",
        "COMMENTS": "",
    }
    assert extract_application_numbers(row) == ["47408-NEA"]


def test_extract_multiple_application_numbers_is_detectable():
    row = {
        "ORIGIN_ID": "123456789012",
        "TITLE": "Old consolidated",
        "COMMENTS": "Номер заявки: 47408-NEA\nНомер заявки: 47409-NEA",
    }
    assert extract_application_numbers(row) == ["47408-NEA", "47409-NEA"]


def test_resolve_potential_stage_uses_singular_alias():
    stage_id, name = resolve_lead_stage(
        STATUSES,
        "Потенциальные сделки",
        aliases=("Потенциальная сделка",),
    )
    assert stage_id == "UC_POTENTIAL"
    assert name == "Потенциальная сделка"


def test_resolve_missing_stage_fails_closed():
    try:
        resolve_lead_stage(STATUSES, "Нет такой стадии")
    except StatusSyncError as exc:
        assert "Не найдена стадия" in str(exc)
    else:
        raise AssertionError("missing stage must fail")


def test_dry_run_plans_failure_and_potential_without_writing():
    client = FakeClient([
        lead(1, "47408-NEA"),
        lead(2, "47409-NEA"),
        lead(3, "47410-NEA"),
    ])
    scraper = FakeScraper(
        {
            "47408-NEA": "Отозвано",
            "47409-NEA": "Выдана лицензия",
            "47410-NEA": "Принято",
        }
    )

    summary, rows = sync_statuses(
        client=client,
        scraper=scraper,
        mode="dry_run",
        doc_type="Заявка на разведку ТПИ",
        failure_stage_name="Провал",
        potential_stage_name="Потенциальные сделки",
    )

    assert summary.changes_planned == 2
    assert summary.no_rule == 1
    assert summary.changes_applied == 0
    assert client.updated == []
    assert [row.action for row in rows] == [
        "would_update",
        "would_update",
        "no_change_for_external_status",
    ]


def test_apply_updates_exact_target_stages():
    client = FakeClient([
        lead(1, "47408-NEA"),
        lead(2, "47409-NEA"),
    ])
    scraper = FakeScraper(
        {
            "47408-NEA": "Аннулировано",
            "47409-NEA": "Выдана лицензия",
        }
    )

    summary, rows = sync_statuses(
        client=client,
        scraper=scraper,
        mode="apply",
        doc_type="Заявка на разведку ТПИ",
        failure_stage_name="Провал",
        potential_stage_name="Потенциальные сделки",
    )

    assert summary.changes_applied == 2
    assert client.updated == [
        ("1", {"STATUS_ID": "JUNK"}),
        ("2", {"STATUS_ID": "UC_POTENTIAL"}),
    ]
    assert [row.action for row in rows] == ["updated", "updated"]


def test_same_target_stage_is_not_rewritten():
    client = FakeClient([lead(1, "47408-NEA", status_id="JUNK")])
    scraper = FakeScraper({"47408-NEA": "Отклонено"})

    summary, rows = sync_statuses(
        client=client,
        scraper=scraper,
        mode="apply",
        doc_type="Заявка на разведку ТПИ",
        failure_stage_name="Провал",
        potential_stage_name="Потенциальные сделки",
    )

    assert summary.already_in_target_stage == 1
    assert client.updated == []
    assert rows[0].action == "already_in_target_stage"


def test_missing_application_does_not_change_lead():
    client = FakeClient([lead(1, "47408-NEA")])
    scraper = FakeScraper({})

    summary, rows = sync_statuses(
        client=client,
        scraper=scraper,
        mode="apply",
        doc_type="Заявка на разведку ТПИ",
        failure_stage_name="Провал",
        potential_stage_name="Потенциальные сделки",
    )

    assert summary.application_not_found == 1
    assert client.updated == []
    assert rows[0].action == "application_not_found"


def test_duplicate_application_number_uses_one_eqazyna_lookup():
    client = FakeClient([
        lead(1, "47408-NEA"),
        lead(2, "47408-NEA", originator="EQAZYNA"),
    ])
    scraper = FakeScraper({"47408-NEA": "Отозвано"})

    summary, _ = sync_statuses(
        client=client,
        scraper=scraper,
        mode="dry_run",
        doc_type="Заявка на разведку ТПИ",
        failure_stage_name="Провал",
        potential_stage_name="Потенциальные сделки",
    )

    assert summary.leads_discovered == 2
    assert scraper.calls == [("47408-NEA", "Заявка на разведку ТПИ")]
