from pathlib import Path


def test_newer_queue_version_always_wins_over_old_completion():
    root = Path(__file__).resolve().parents[3]
    text = (root / "integrations" / "bitrix_owner_sync_queue" / "Code.gs").read_text(encoding="utf-8")
    complete = text[text.index("function complete_(body)"):text.index("function logOperation_(body)")]

    newer = complete.index("if (hasNewerVersion)")
    success = complete.index("else if (result.success)")
    retry = complete.index("else if (retryable")
    manual = complete.index("MANUAL_REVIEW")

    assert newer < success < retry < manual
    assert "row[2] = 'PENDING';" in complete[newer:success]
    assert "row[7] = 0;" in complete[newer:success]
    assert "supersededFailures" in complete
