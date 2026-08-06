from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "run_from_env.py"
spec = spec_from_file_location("patched_run_from_env", MODULE_PATH)
run_from_env = module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(run_from_env)


def test_consecutive_pages_are_not_silently_reduced_to_one(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INPUT_MODE", "dry_run")
    monkeypatch.setenv("INPUT_PAGES", "3")
    monkeypatch.setenv("INPUT_PAGE_START", "1")
    monkeypatch.delenv("INPUT_PAGE_LIST", raising=False)

    captured = {}

    def fake_run(args, check=False):
        captured["args"] = args
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_from_env.subprocess, "run", fake_run)

    assert run_from_env.main() == 0
    args = captured["args"]
    assert args[args.index("--pages") + 1] == "3"
    assert args[args.index("--page-start") + 1] == "1"
    assert args[args.index("--max-consecutive-page-errors") + 1] == "0"
    assert "resolved_pages=1-3" in capsys.readouterr().out


def test_explicit_page_list_is_passed_without_changing_pages(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INPUT_MODE", "dry_run")
    monkeypatch.setenv("INPUT_PAGES", "3")
    monkeypatch.setenv("INPUT_PAGE_START", "5")
    monkeypatch.setenv("INPUT_PAGE_LIST", "2,7-8")

    captured = {}

    def fake_run(args, check=False):
        captured["args"] = args
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_from_env.subprocess, "run", fake_run)

    assert run_from_env.main() == 0
    args = captured["args"]
    assert args[args.index("--pages") + 1] == "3"
    assert args[args.index("--page-list") + 1] == "2,7-8"
