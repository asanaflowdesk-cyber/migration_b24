from pathlib import Path

import pytest

import director_frozen_plan_hotfix as hotfix


def test_eventually_retries_reads(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(hotfix.time, "sleep", lambda _seconds: None)

    def check():
        calls["n"] += 1
        return calls["n"] >= 3

    assert hotfix._eventually(check, delays=(0.0, 0.1, 0.2)) is True
    assert calls["n"] == 3


def test_eventually_does_not_invent_success(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(hotfix.time, "sleep", lambda _seconds: None)

    def check():
        calls["n"] += 1
        return False

    assert hotfix._eventually(check, delays=(0.0, 0.1, 0.2)) is False
    assert calls["n"] == 3


def test_legacy_approved_plan_survives_only_targeted_sha_change(monkeypatch):
    plan = {
        "plan_id": hotfix.LEGACY_COMPATIBLE_PLAN["plan_id"],
        "source_sha": hotfix.LEGACY_COMPATIBLE_PLAN["source_sha"],
    }
    monkeypatch.setattr(
        hotfix,
        "_ORIGINAL_LOAD_FROZEN_PLAN",
        lambda path, expected_plan_id, current_sha: dict(plan),
    )
    loaded = hotfix.load_frozen_plan_compatible(
        Path("unused.json"),
        expected_plan_id=plan["plan_id"],
        current_sha="new-hotfix-sha",
    )
    assert loaded == plan


def test_other_old_plan_is_not_allowed_across_sha(monkeypatch):
    plan = {"plan_id": "DIR-OTHER", "source_sha": "old-sha"}
    monkeypatch.setattr(
        hotfix,
        "_ORIGINAL_LOAD_FROZEN_PLAN",
        lambda path, expected_plan_id, current_sha: dict(plan),
    )
    with pytest.raises(RuntimeError, match="plan_code_sha_mismatch"):
        hotfix.load_frozen_plan_compatible(
            Path("unused.json"),
            expected_plan_id=plan["plan_id"],
            current_sha="new-sha",
        )


class RequisiteClient:
    def __init__(self):
        self.calls = 0

    def call(self, method, payload):
        assert method == "crm.requisite.get"
        self.calls += 1
        if self.calls < 3:
            return {"ID": str(payload["id"]), "RQ_DIRECTOR": ""}
        return {
            "ID": str(payload["id"]),
            "RQ_DIRECTOR": "ЖАЙЛАУБЕКОВ АСКАРБЕК НУРЛАНБЕКОВИЧ",
        }


def test_requisite_verification_can_observe_delayed_bitrix_read(monkeypatch):
    client = RequisiteClient()
    monkeypatch.setattr(hotfix.time, "sleep", lambda _seconds: None)
    assert hotfix._eventually(
        lambda: hotfix._requisite_matches(
            client,
            1361,
            "ЖАЙЛАУБЕКОВ АСКАРБЕК НУРЛАНБЕКОВИЧ",
        ),
        delays=(0.0, 0.1, 0.2),
    )
    assert client.calls == 3
