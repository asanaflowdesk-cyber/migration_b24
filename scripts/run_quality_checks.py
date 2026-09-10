from __future__ import annotations

import compileall
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEST_SUITES = [
    ("cloud_export", ROOT / "processes" / "cloud_export"),
    ("cloud_to_box", ROOT / "processes" / "cloud_to_box"),
    ("company_owner_sync", ROOT / "processes" / "company_owner_sync"),
    ("eqazyna_leads", ROOT / "processes" / "eqazyna_leads"),
    ("lead_recovery", ROOT / "processes" / "lead_recovery"),
    ("flowdesk", ROOT / "processes" / "flowdesk"),
    ("departments", ROOT / "processes" / "departments"),
    ("user_registration", ROOT / "processes" / "user_registration"),
]


def run_suite(name: str, cwd: Path) -> None:
    env = os.environ.copy()
    paths = [str(ROOT)]
    if name == "company_owner_sync":
        paths.extend(
            [
                str(ROOT / "processes" / "company_owner_sync"),
                str(ROOT / "processes" / "eqazyna_leads"),
            ]
        )
    else:
        paths.append(str(cwd))
    existing = env.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    print(f"\n=== pytest: {name} ===", flush=True)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=cwd,
        env=env,
        check=True,
    )




def check_windows_runner_python_contract() -> None:
    """Keep Windows self-hosted jobs on the workstation's existing Python.

    These jobs must not install/bootstrap Python through GitHub Actions or
    PowerShell. They create only a job-local .venv via scripts/prepare-python.cmd.
    """
    failures: list[str] = []
    workflows = ROOT / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "runs-on: [self-hosted, Windows, X64]" not in text:
            continue
        forbidden = {
            "actions/setup-python": "actions/setup-python",
            "bootstrap-python": "bootstrap-python",
            "shell: powershell": "PowerShell shell",
        }
        for needle, label in forbidden.items():
            if needle in text:
                failures.append(f"{path.relative_to(ROOT)}: forbidden {label}")
        if "prepare-python.cmd" not in text:
            failures.append(f"{path.relative_to(ROOT)}: prepare-python.cmd is missing")

    prepare = ROOT / "scripts" / "prepare-python.cmd"
    prepare_text = prepare.read_text(encoding="utf-8-sig")
    expected = r"C:\Users\Alyona.Sachyova\AppData\Local\Programs\Python\Python312\python.exe"
    if expected not in prepare_text:
        failures.append("scripts/prepare-python.cmd: workstation Python 3.12 path changed")
    if "powershell" in prepare_text.lower() or "bootstrap-python" in prepare_text.lower():
        failures.append("scripts/prepare-python.cmd: bootstrap/PowerShell must not be used")
    if (ROOT / "scripts" / "bootstrap-python.ps1").exists():
        failures.append("scripts/bootstrap-python.ps1 must not exist")

    if failures:
        raise RuntimeError("Windows runner Python contract violated:\n" + "\n".join(failures))


def main() -> int:
    print("=== Windows self-hosted Python contract ===", flush=True)
    check_windows_runner_python_contract()
    print("Windows self-hosted Python contract: OK", flush=True)
    print("=== compileall ===", flush=True)
    if not compileall.compile_dir(ROOT / "common", quiet=1):
        return 1
    if not compileall.compile_dir(ROOT / "processes", quiet=1):
        return 1
    for name, cwd in TEST_SUITES:
        run_suite(name, cwd)
    print("\nAll quality checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
