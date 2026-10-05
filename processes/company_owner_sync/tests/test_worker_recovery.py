import persistent_worker
import queue_founder_packages


def test_recovery_waits_for_empty_unlocked_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_QUEUE_URL", "https://script.google.com/macros/s/test/exec")
    monkeypatch.setenv("GOOGLE_QUEUE_KEY", "x" * 32)
    monkeypatch.setenv("TARGET_BITRIX_WEBHOOK_URL", "configured")
    monkeypatch.setenv("OWNER_SYNC_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(persistent_worker, "build_client", lambda: object())
    states = iter([
        {"drained": False, "busy": True, "items": 0},
        {"drained": False, "items": 20},
        {"drained": True, "busy": False, "items": 0},
    ])
    phases = []
    recovered = []

    def process(*args):
        phases.append(len(phases) + 1)
        return next(states)

    def sleep(seconds):
        if len(phases) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(persistent_worker, "process_queue", process)
    monkeypatch.setattr(persistent_worker, "repair_package_residuals", lambda *args: recovered.append(phases[-1]))
    monkeypatch.setattr(persistent_worker.time, "sleep", sleep)
    persistent_worker.run()
    assert recovered == [3]


def test_batch_limit_does_not_report_queue_as_drained(monkeypatch, tmp_path):
    monkeypatch.setattr(queue_founder_packages, "queue_call", lambda *args, **kwargs: {"items": [{"contact_id": 837, "version": 1}], "claim_id": "test"} if args[2] == "claim" else {})
    monkeypatch.setattr(queue_founder_packages, "process_claim", lambda *args: ([{"success": True, "outcome": "processed"}], 0))
    summary = queue_founder_packages.process_queue(object(), "url", "key", tmp_path, max_batches=1)
    assert summary["items"] == 1
    assert summary["drained"] is False
