from __future__ import annotations

from eqazyna_bitrix.models import Application
from sync_eqazyna_statuses import (
    DEFAULT_FAILURE_REASON_FIELD,
    StatusSyncError,
    extract_application_number,
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
        filter_ = payload.get("filter", {})
        self.list_filters.append(dict(filter_))
        if "ORIGINATOR_ID" in filter_:
            originator = filter_["ORIGINATOR_ID"]
            return [row for row in self.leads if row.get("ORIGINATOR_ID") == originator]
        if "%TITLE" in filter_:
            needle = str(filter_["%TITLE"]).casefold()
            return [row for row in self.leads if needle in str(row.get("TITLE") or "").casefold()]
        raise AssertionError(f"unexpected filter: {filter_}")

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


def lead(
    lead_id,
    doc_number,
    status_id="NEW",
    *,
    originator="EQAZYNA_LEAD",
    reason=None,
    title=None,
    semantic=None,
):
    if semantic is None:
        semantic = "F" if status_id == "JUNK" else "P"
    return {
        "ID": str(lead_id),
        "TITLE": title if title is not None else f"ТОО Test. e-Qazyna № {doc_number}",
        "COMMENTS": "COMMENTS MUST NOT BE USED FOR THE APPLICATION NUMBER",
        "STATUS_ID": status_id,
        "STATUS_SEMANTIC_ID": semantic,
        "ORIGINATOR_ID": originator,
        "ORIGIN_ID": "ORIGIN_ID_MUST_NOT_BE_USED",
        DEFAULT_FAILURE_REASON_FIELD: reason,
    }


def run_sync(client, scraper, mode="apply"):
    return sync_statuses(
        client=client,
        scraper=scraper,
        mode=mode,
        doc_type="Заявка на разведку ТПИ",
        failure_stage_name="Провал",
        potential_stage_name="Потенциальные сделки",
    )


def test_external_status_mapping():
    assert map_external_status("Отклонено") == "failure"
    assert map_external_status("Отозвано") == "failure"
    assert map_external_status("Аннулировано") == "failure"
    assert map_external_status("Выдана лицензия") == "potential"
    assert map_external_status("Принято") is None
    assert map_external_status("Завершено") is None


def test_application_number_is_everything_after_number_marker_in_title():
    row = lead(1, "ignored", title="ТОО Конор Н.А. e-Qazyna № 49667-NEA")
    assert extract_application_number(row) == "49667-NEA"


def test_application_number_is_not_read_from_origin_or_comments():
    row = {
        "TITLE": "ТОО Test без номера в заголовке",
        "ORIGIN_ID": "47408-NEA",
        "COMMENTS": "Номер заявки: 47408-NEA",
    }
    assert extract_application_number(row) is None


def test_one_lead_one_application_no_regex_split_of_title_suffix():
    row = lead(1, "ignored", title="ТОО Test. e-Qazyna № ABC/2026 77-NEA")
    assert extract_application_number(row) == "ABC/2026 77-NEA"


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


def test_apply_maps_all_four_statuses_to_stage_and_failure_reason_enum():
    client = FakeClient([
        lead(1, "47408-NEA"),
        lead(2, "47409-NEA"),
        lead(3, "47410-NEA"),
        lead(4, "47411-NEA"),
    ])
    scraper = FakeScraper(
        {
            "47408-NEA": "Аннулировано",
            "47409-NEA": "Отозвано",
            "47410-NEA": "Отклонено",
            "47411-NEA": "Выдана лицензия",
        }
    )

    summary, rows = run_sync(client, scraper)

    assert summary.changes_applied == 4
    assert client.updated == [
        ("1", {"STATUS_ID": "JUNK", DEFAULT_FAILURE_REASON_FIELD: "66"}),
        ("2", {"STATUS_ID": "JUNK", DEFAULT_FAILURE_REASON_FIELD: "67"}),
        ("3", {"STATUS_ID": "JUNK", DEFAULT_FAILURE_REASON_FIELD: "68"}),
        ("4", {"STATUS_ID": "UC_POTENTIAL", DEFAULT_FAILURE_REASON_FIELD: "69"}),
    ]
    assert [row.target_failure_reason_name for row in rows] == [
        "Заявка аннулирована на сайте",
        "Заявка отменена на сайте",
        "Заявка отклонена на сайте",
        "По заявке уже выдана лицензия",
    ]


def test_inactive_failed_lead_without_external_reason_is_not_looked_up():
    client = FakeClient([lead(1, "47408-NEA", status_id="JUNK", reason=None)])
    scraper = FakeScraper({"47408-NEA": "Отклонено"})

    summary, rows = run_sync(client, scraper)

    assert summary.skipped_inactive_lead == 1
    assert summary.active_leads_selected == 0
    assert scraper.calls == []
    assert client.updated == []
    assert rows[0].action == "skipped_inactive_lead"


def test_any_of_four_terminal_reasons_skips_portal_even_if_stage_is_wrong():
    client = FakeClient([
        lead(1, "47408-NEA", reason="66"),
        lead(2, "47409-NEA", reason="67"),
        lead(3, "47410-NEA", reason="68"),
        lead(4, "47411-NEA", reason="69"),
    ])
    scraper = FakeScraper({
        "47408-NEA": "Принято",
        "47409-NEA": "Принято",
        "47410-NEA": "Принято",
        "47411-NEA": "Принято",
    })

    summary, rows = run_sync(client, scraper)

    assert summary.skipped_terminal_reason == 4
    assert summary.active_leads_selected == 0
    assert scraper.calls == []
    assert client.updated == []
    assert [row.action for row in rows] == ["skipped_terminal_reason"] * 4


def test_terminal_reason_gate_runs_before_title_number_validation():
    client = FakeClient([
        lead(1, "ignored", reason="68", title="e-Qazyna card without number")
    ])
    scraper = FakeScraper({})

    summary, rows = run_sync(client, scraper)

    assert summary.skipped_terminal_reason == 1
    assert summary.leads_without_title_number == 0
    assert scraper.calls == []
    assert rows[0].action == "skipped_terminal_reason"


def test_dry_run_shows_stage_and_reason_without_writing():
    client = FakeClient([lead(1, "47408-NEA")])
    scraper = FakeScraper({"47408-NEA": "Отозвано"})

    summary, rows = run_sync(client, scraper, mode="dry_run")

    assert summary.changes_planned == 1
    assert client.updated == []
    assert rows[0].target_status_id == "JUNK"
    assert rows[0].target_failure_reason_id == "67"
    assert rows[0].action == "would_update"


def test_unmapped_external_status_changes_nothing():
    client = FakeClient([lead(1, "47408-NEA")])
    scraper = FakeScraper({"47408-NEA": "Принято"})

    summary, rows = run_sync(client, scraper)

    assert summary.no_rule == 1
    assert client.updated == []
    assert rows[0].action == "no_change_for_external_status"


def test_missing_application_changes_nothing():
    client = FakeClient([lead(1, "47408-NEA")])
    scraper = FakeScraper({})

    summary, rows = run_sync(client, scraper)

    assert summary.application_not_found == 1
    assert client.updated == []
    assert rows[0].action == "application_not_found"


def test_lead_without_title_number_is_not_looked_up():
    client = FakeClient([
        lead(
            1,
            "47408-NEA",
            title="ТОО Test. e-Qazyna без символа номера",
        )
    ])
    scraper = FakeScraper({"47408-NEA": "Отклонено"})

    summary, rows = run_sync(client, scraper)

    assert summary.leads_without_title_number == 1
    assert scraper.calls == []
    assert client.updated == []
    assert rows[0].action == "skipped_no_application_number"


def test_title_search_recovers_eqazyna_lead_without_originator_marker():
    row = lead(1, "47408-NEA", originator="")
    client = FakeClient([row])
    scraper = FakeScraper({"47408-NEA": "Отклонено"})

    summary, _ = run_sync(client, scraper, mode="dry_run")

    assert summary.leads_discovered == 1
    assert scraper.calls == [("47408-NEA", "Заявка на разведку ТПИ")]


class RaisingScraper:
    def __init__(self):
        self.calls = []

    def fetch_application_by_number(self, doc_number, doc_type):
        self.calls.append((doc_number, doc_type))
        raise TimeoutError("portal timeout")


def test_portal_failure_is_warning_not_fatal_sync_error():
    client = FakeClient([lead(1, "47408-NEA")])
    scraper = RaisingScraper()

    summary, rows = run_sync(client, scraper)

    assert summary.portal_lookup_warnings == 1
    assert summary.errors == 0
    assert summary.changes_applied == 0
    assert rows[0].action == "portal_warning"
    assert client.updated == []


def test_max_items_limits_active_candidates_not_terminal_or_closed_leads():
    client = FakeClient([
        lead(1, "47401-NEA", reason="66"),
        lead(2, "47402-NEA", status_id="JUNK", reason=None),
        lead(3, "47403-NEA"),
        lead(4, "47404-NEA"),
    ])
    scraper = FakeScraper({
        "47403-NEA": "Принято",
        "47404-NEA": "Принято",
    })

    summary, _ = sync_statuses(
        client=client,
        scraper=scraper,
        mode="dry_run",
        doc_type="Заявка на разведку ТПИ",
        failure_stage_name="Провал",
        potential_stage_name="Потенциальные сделки",
        max_items=1,
    )

    assert summary.leads_discovered == 4
    assert summary.skipped_terminal_reason == 1
    assert summary.skipped_inactive_lead == 1
    assert summary.active_leads_candidates == 2
    assert summary.active_leads_selected == 1
    assert scraper.calls == [("47403-NEA", "Заявка на разведку ТПИ")]
