from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def base_dir() -> Path:
    path = Path.home() / ".company-owner-sync"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    return base_dir() / "runtime"


def venv_dir() -> Path:
    return base_dir() / "venv"


def pid_file() -> Path:
    return base_dir() / "worker.pid"


def read_pid() -> int | None:
    path = pid_file()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def stop_worker() -> int:
    pid = read_pid()
    if not pid:
        print("Активный 31A worker не найден")
        pid_file().unlink(missing_ok=True)
        return 0

    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode == 0:
        print(f"31A worker остановлен: PID={pid}")
    else:
        print(f"31A worker PID={pid} уже не запущен или недоступен")

    pid_file().unlink(missing_ok=True)
    return 0


def deploy_runtime(source_root: Path) -> Path:
    target = runtime_dir()
    if target.exists():
        shutil.rmtree(target)

    processes = target / "processes"
    processes.mkdir(parents=True, exist_ok=True)

    ignore = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        ".venv",
        "output_event",
        "*.sqlite3",
        "*.sqlite3-wal",
        "*.sqlite3-shm",
    )

    shutil.copytree(
        source_root / "processes" / "company_owner_sync",
        processes / "company_owner_sync",
        ignore=ignore,
    )
    shutil.copytree(
        source_root / "processes" / "eqazyna_leads",
        processes / "eqazyna_leads",
        ignore=ignore,
    )

    return target


def ensure_persistent_venv(source_root: Path) -> Path:
    venv = venv_dir()
    python = venv / "Scripts" / "python.exe"

    if not python.exists():
        base_python = Path(getattr(sys, "_base_executable", "") or sys.executable)
        print(f"Создаю постоянное окружение 31A: {venv}")
        subprocess.run([str(base_python), "-m", "venv", str(venv)], check=True)

    requirements = source_root / "processes" / "company_owner_sync" / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = base_dir() / "requirements.sha256"
    previous = marker.read_text(encoding="ascii").strip() if marker.exists() else ""

    if digest != previous:
        print("Обновляю зависимости 31A worker")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)],
            check=True,
        )
        marker.write_text(digest, encoding="ascii")

    return venv


def start_worker() -> int:
    required = (
        "TARGET_BITRIX_WEBHOOK_URL",
        "GOOGLE_QUEUE_URL",
        "GOOGLE_QUEUE_KEY",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        print("ERROR: не заданы " + ", ".join(missing), file=sys.stderr)
        return 2

    source_root = Path(
        os.environ.get("OWNER_SYNC_SOURCE_ROOT")
        or os.environ["GITHUB_WORKSPACE"]
    ).resolve()

    runtime = deploy_runtime(source_root)
    venv = ensure_persistent_venv(source_root)

    scripts_dir = venv / "Scripts"
    python = scripts_dir / "pythonw.exe"
    if not python.exists():
        python = scripts_dir / "python.exe"

    if not python.exists():
        print(f"ERROR: Python не найден: {python}", file=sys.stderr)
        return 3

    base = base_dir()
    stdout_path = base / "worker.log"
    stderr_path = base / "worker.err.log"
    output_dir = base / "output"

    company_dir = runtime / "processes" / "company_owner_sync"
    eqazyna_dir = runtime / "processes" / "eqazyna_leads"

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(company_dir), str(eqazyna_dir)])
    env["PYTHONUNBUFFERED"] = "1"
    env["OWNER_SYNC_OUTPUT_DIR"] = str(output_dir)
    env["RUNNER_TRACKING_ID"] = ""

    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

    with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
        proc = subprocess.Popen(
            [str(python), "-u", str(company_dir / "persistent_worker.py")],
            cwd=str(company_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            close_fds=True,
            creationflags=creationflags,
        )

    pid_file().write_text(str(proc.pid), encoding="ascii")
    time.sleep(4)

    if proc.poll() is not None:
        print("ERROR: 31A worker завершился сразу после запуска", file=sys.stderr)
        if stdout_path.exists():
            print("\n--- worker.log ---")
            print(stdout_path.read_text(encoding="utf-8", errors="replace")[-8000:])
        if stderr_path.exists():
            print("\n--- worker.err.log ---", file=sys.stderr)
            print(stderr_path.read_text(encoding="utf-8", errors="replace")[-8000:], file=sys.stderr)
        pid_file().unlink(missing_ok=True)
        return 1

    print(f"31A worker запущен отдельно от Actions: PID={proc.pid}")
    print(f"Runtime: {runtime}")
    print(f"Idle poll: {env.get('OWNER_SYNC_POLL_SECONDS', '5')} sec")
    print("Перенос пакетов больше не ждёт GitHub Action.")

    return 0


def status_worker() -> int:
    base = base_dir()
    pid = read_pid()

    print(f"PID file: {pid if pid else 'нет'}")
    print(f"Runtime: {runtime_dir()}")
    print(f"Venv: {venv_dir()}")

    if pid:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "").strip()
        alive = bool(output and "No tasks are running" not in output and "INFO:" not in output)
        print(f"Worker process: {'RUNNING' if alive else 'NOT RUNNING'}")
        if output:
            print(output)

    for name in ("worker.log", "worker.err.log"):
        path = base / name
        print(f"\n--- {name} ---")
        if not path.exists():
            print("(файл отсутствует)")
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        print(content[-12000:] if content else "(пусто)")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "restart", "status"])
    args = parser.parse_args()

    if args.action == "status":
        return status_worker()

    if args.action in {"stop", "restart"}:
        code = stop_worker()
        if code != 0:
            return code

    if args.action in {"start", "restart"}:
        return start_worker()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
