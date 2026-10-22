import persistent_worker
import queue_founder_packages


def test_recovery_waits_for_empty_unlocked_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_QUEUE_URL", "https://script.google.com/macros/s/test/exec")
    monkeypatch.setenv("GOOGLE_QUEUE_KEY", "x" * 32)
    monkeypatch.setenv("TARGET_BITRIX_WEBHOOK_URL", "configured")
    monkeypatch.setenv("OWNER_SYNC_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(persistent_worker, "build_client", lambda: object())
    monkeypatch.setattr(persistent_worker, "receive_owner_events", lambda *args: 0)
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


def _runner_contract_checker():
    import runpy
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    return runpy.run_path(str(root / "scripts" / "run_quality_checks.py"))["check_windows_runner_python_contract"]


def test_runner_contract_accepts_current_managed_persistent_workers():
    _runner_contract_checker()()


def test_runner_contract_still_requires_environment_setup_for_ordinary_jobs(tmp_path):
    import pytest

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    python_path = r"C:\Users\Alyona.Sachyova\AppData\Local\Programs\Python\Python312\python.exe"
    (scripts / "prepare-python.cmd").write_text(python_path, encoding="utf-8")
    (workflows / "unmanaged.yml").write_text(
        'runs-on: [self-hosted, Windows, X64]\nset "PYTHON_EXE=' + python_path + '"\n',
        encoding="utf-8",
    )
    checker = _runner_contract_checker()
    checker.__globals__["ROOT"] = tmp_path

    with pytest.raises(RuntimeError, match="prepare-python.cmd is missing"):
        checker()


def test_runner_contract_does_not_allow_python_bootstrap_for_managed_workers(tmp_path):
    import pytest

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    python_path = r"C:\Users\Alyona.Sachyova\AppData\Local\Programs\Python\Python312\python.exe"
    (scripts / "prepare-python.cmd").write_text(python_path, encoding="utf-8")
    (workflows / "managed.yml").write_text(
        'runs-on: [self-hosted, Windows, X64]\n'
        'uses: actions/setup-python\n'
        'set "PYTHON_EXE=' + python_path + '"\n'
        r'"%PYTHON_EXE%" -u "_owner_sync_control\processes\company_owner_sync\worker_control.py"',
        encoding="utf-8",
    )
    checker = _runner_contract_checker()
    checker.__globals__["ROOT"] = tmp_path

    with pytest.raises(RuntimeError, match="forbidden actions/setup-python"):
        checker()
