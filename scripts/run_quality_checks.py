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



def check_powershell_interpolation() -> None:
    """Catch PowerShell's `$name:` parser trap inside expandable strings.

    Scope-qualified variables such as $env:PATH are valid. Other `$name:`
    sequences inside double-quoted strings are parsed as scoped variables and
    can fail before the script executes.
    """
    allowed_scopes = {"env", "global", "script", "local", "private", "using"}
    pattern = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*):")
    failures: list[str] = []
    for path in ROOT.rglob("*.ps1"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            # Only expandable string content is relevant to this parser pitfall.
            if '"' not in line:
                continue
            for match in pattern.finditer(line):
                if match.group(1).lower() not in allowed_scopes:
                    failures.append(f"{path.relative_to(ROOT)}:{lineno}: {match.group(0)}")
    if failures:
        raise RuntimeError("Unsafe PowerShell variable interpolation found:\n" + "\n".join(failures))


def main() -> int:
    print("=== PowerShell interpolation check ===", flush=True)
    check_powershell_interpolation()
    print("PowerShell interpolation check: OK", flush=True)
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
