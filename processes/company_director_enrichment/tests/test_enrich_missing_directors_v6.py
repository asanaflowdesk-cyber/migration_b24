from __future__ import annotations

from typing import Any

import enrich_missing_directors_v6 as v6


def _company(company_id: int, title: str = "C", owner: int = 72) -> dict[str, Any]:
    return {
        "ID": str(company_id),
        "TITLE": title,
        "ASSIGNED_BY_ID": str(owner),
        "ORIGIN_ID": "",
    }


def _req(company_id: int, req_id: int, director: str = "", bin_: str = "123456789012") -> dict[str, Any]:
    return {
        "ID": str(req_id),
        "ENTITY_ID": str(company_id),
        "RQ_INN": bin_,
        "RQ_DIRECTOR": director,
    }


def _contact(company_id: int, contact_id: int, fio: str, comments: str = "") -> dict[str, Any]:
    last, first, *rest = fio.split()
    return {
        "ID": str(contact_id),
        "COMPANY_ID": str(company_id),
        "LAST_NAME": last,
        "NAME": first,
        "SECOND_NAME": " ".join(rest),
        "POST": "Руководитель",
        "COMMENTS": comments,
        "ASSIGNED_BY_ID": "72",
    }


def test_existing_req_only_is_known_but_not_expanded(monkeypatch):
    monkeypatch.setattr(v6, "_secondary_directors_for_missing", lambda *a, **k: {})
    snapshot = {
        "companies": [_company(1)],
        "requisites": [_req(1, 10, "ИВАНОВ ИВАН ИВАНОВИЧ")],
        "contacts": [],
    }
    rows, handled, conflicts = v6.build_internal_repair_rows(object(), snapshot, 2)
    assert handled == {1}
    assert conflicts == set()
    assert rows == []


def test_partial_managed_contact_is_repaired(monkeypatch):
    monkeypatch.setattr(v6, "_secondary_directors_for_missing", lambda *a, **k: {})
    snapshot = {
        "companies": [_company(1)],
        "requisites": [_req(1, 10, "")],
        "contacts": [
            _contact(
                1,
                100,
                "ИВАНОВ ИВАН ИВАНОВИЧ",
                "DIRECTOR_PLAN_ID: OLD-RUN",
            )
        ],
    }
    rows, handled, conflicts = v6.build_internal_repair_rows(object(), snapshot, 2)
    assert handled == {1}
    assert conflicts == set()
    assert len(rows) == 1
    assert rows[0]["confidence"] == "internal_repair"


def test_complete_managed_record_may_be_rechecked_for_missing_links(monkeypatch):
    monkeypatch.setattr(v6, "_secondary_directors_for_missing", lambda *a, **k: {})
    snapshot = {
        "companies": [_company(1)],
        "requisites": [_req(1, 10, "ИВАНОВ ИВАН ИВАНОВИЧ")],
        "contacts": [
            _contact(
                1,
                100,
                "ИВАНОВ ИВАН ИВАНОВИЧ",
                "EQAZYNA_DIRECTOR: adata",
            )
        ],
    }
    rows, handled, conflicts = v6.build_internal_repair_rows(object(), snapshot, 2)
    assert handled == {1}
    assert conflicts == set()
    assert len(rows) == 1


def test_complete_manual_record_is_not_touched(monkeypatch):
    monkeypatch.setattr(v6, "_secondary_directors_for_missing", lambda *a, **k: {})
    snapshot = {
        "companies": [_company(1)],
        "requisites": [_req(1, 10, "ИВАНОВ ИВАН ИВАНОВИЧ")],
        "contacts": [_contact(1, 100, "ИВАНОВ ИВАН ИВАНОВИЧ")],
    }
    rows, handled, conflicts = v6.build_internal_repair_rows(object(), snapshot, 2)
    assert handled == {1}
    assert conflicts == set()
    assert rows == []


def test_unmanaged_contact_only_is_not_touched(monkeypatch):
    monkeypatch.setattr(v6, "_secondary_directors_for_missing", lambda *a, **k: {})
    snapshot = {
        "companies": [_company(1)],
        "requisites": [_req(1, 10, "")],
        "contacts": [_contact(1, 100, "ИВАНОВ ИВАН ИВАНОВИЧ")],
    }
    rows, handled, conflicts = v6.build_internal_repair_rows(object(), snapshot, 2)
    assert handled == {1}
    assert conflicts == set()
    assert rows == []


def test_internal_conflict_is_never_sent_to_external(monkeypatch):
    monkeypatch.setattr(v6, "_secondary_directors_for_missing", lambda *a, **k: {})
    snapshot = {
        "companies": [_company(1)],
        "requisites": [_req(1, 10, "ИВАНОВ ИВАН ИВАНОВИЧ")],
        "contacts": [_contact(1, 100, "ПЕТРОВ ПЕТР ПЕТРОВИЧ")],
    }
    rows, handled, conflicts = v6.build_internal_repair_rows(object(), snapshot, 2)
    assert handled == {1}
    assert conflicts == {1}
    assert rows[0]["status"] == "source_conflict"
    assert "ИВАНОВ" in rows[0]["evidence"]
    assert "ПЕТРОВ" in rows[0]["evidence"]


def test_apply_continues_after_independent_group_error(monkeypatch):
    rows = [
        {"company_id": 1, "plan_status": "READY", "director": "A A"},
        {"company_id": 2, "plan_status": "READY", "director": "B B"},
    ]
    plan = {"plan_id": "LIVE-1", "rows": rows}
    monkeypatch.setattr(v6.frozen, "preflight_plan", lambda client, plan: (True, [], {}))
    monkeypatch.setattr(
        v6.frozen,
        "_group_rows",
        lambda ready: {"a a": [ready[0]], "b b": [ready[1]]},
    )

    def fake_apply(client, group_rows, plan_id):
        if group_rows[0]["company_id"] == 1:
            raise RuntimeError("first failed")
        result = dict(group_rows[0])
        result["verification_status"] = "VERIFIED"
        return [result]

    monkeypatch.setattr(v6.reliable, "_apply_group_reliable", fake_apply)
    final_rows, errors, preflight_ok = v6.apply_live_plan_resilient(object(), plan)
    assert preflight_ok is True
    assert len(errors) == 1
    assert final_rows[1]["verification_status"] == "VERIFIED"
