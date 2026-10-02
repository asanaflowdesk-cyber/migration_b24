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
    path = Path.home() / ".flowdesk-chatbot"
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


def worker_command_line(pid: int) -> str:
    if pid <= 0:
        return ""

    command = (
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = "
        f"{int(pid)}\" -ErrorAction SilentlyContinue; "
        "if ($p) { [Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
        "$p.CommandLine }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (result.stdout or "").strip()


def is_expected_worker(pid: int) -> bool:
    command_line = worker_command_line(pid).casefold()
    return bool(
        command_line
        and "processes.flowdesk_chatbot.bitrix_worker" in command_line
    )


def rotate_log(path: Path, *, max_bytes: int = 5_000_000) -> None:
    if not path.exists():
        return
    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return

    backup = path.with_suffix(path.suffix + ".1")
    backup.unlink(missing_ok=True)
    path.replace(backup)


def tail_text(path: Path, *, max_bytes: int = 12_000) -> str:
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def stop_worker() -> int:
    pid = read_pid()
    if not pid:
        print("Активный DeskFlow worker не найден")
        pid_file().unlink(missing_ok=True)
        return 0

    if not is_expected_worker(pid):
        print(f"PID={pid} не принадлежит DeskFlow worker; удаляю устаревший PID file")
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
        print(f"DeskFlow worker остановлен: PID={pid}")
    else:
        print(f"DeskFlow worker PID={pid} уже не запущен или недоступен")

    pid_file().unlink(missing_ok=True)
    return 0


def deploy_runtime(source_root: Path) -> Path:
    """Copy only the chatbot runtime outside the GitHub Actions workspace."""
    target = runtime_dir()

    if target.exists():
        shutil.rmtree(target)

    (target / "processes").mkdir(parents=True, exist_ok=True)

    ignore = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        ".venv",
        "*.sqlite3",
        "*.sqlite3-wal",
        "*.sqlite3-shm",
    )

    shutil.copytree(
        source_root / "common",
        target / "common",
        ignore=ignore,
    )
    shutil.copytree(
        source_root / "processes" / "flowdesk_chatbot",
        target / "processes" / "flowdesk_chatbot",
        ignore=ignore,
    )

    return target


def ensure_persistent_venv(source_root: Path) -> Path:
    """Create/update a venv that is NOT inside actions-runner\_work."""
    venv = venv_dir()
    python = venv / "Scripts" / "python.exe"

    if not python.exists():
        base_python = Path(getattr(sys, "_base_executable", "") or sys.executable)
        print(f"Создаю постоянное окружение: {venv}")
        subprocess.run(
            [str(base_python), "-m", "venv", str(venv)],
            check=True,
        )

    requirements = source_root / "processes" / "flowdesk" / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = base_dir() / "requirements.sha256"
    previous = marker.read_text(encoding="ascii").strip() if marker.exists() else ""

    if digest != previous:
        print("Обновляю зависимости DeskFlow worker")
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(requirements)],
            check=True,
        )
        marker.write_text(digest, encoding="ascii")

    return venv


def start_worker() -> int:
    existing_pid = read_pid()
    if existing_pid:
        if is_expected_worker(existing_pid):
            print(f"DeskFlow worker уже запущен: PID={existing_pid}")
            return 0
        print(f"Удаляю устаревший PID file: PID={existing_pid}")
        pid_file().unlink(missing_ok=True)

    webhook = os.environ.get("TARGET_BITRIX_WEBHOOK_URL", "").strip()
    if not webhook:
        print("ERROR: не задан TARGET_BITRIX_WEBHOOK_URL", file=sys.stderr)
        return 2

    source_root = Path(
        os.environ.get("FLOWDESK_SOURCE_ROOT")
        or os.environ["GITHUB_WORKSPACE"]
    ).resolve()

    # Critical: the live worker must not execute from actions-runner\_work.
    # Other workflows legitimately clean that folder during checkout.
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
    db_path = base / "flowdesk_state.sqlite3"

    rotate_log(stdout_path)
    rotate_log(stderr_path)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(runtime)
    env["PYTHONUNBUFFERED"] = "1"
    env["FLOWDESK_CHATBOT_DB"] = str(db_path)
    env["RUNNER_TRACKING_ID"] = ""

    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

    with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
        proc = subprocess.Popen(
            [str(python), "-u", "-m", "processes.flowdesk_chatbot.bitrix_worker"],
            cwd=str(runtime),
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
        print("ERROR: DeskFlow worker завершился сразу после запуска", file=sys.stderr)
        if stdout_path.exists():
            print("\n--- worker.log ---")
            print(tail_text(stdout_path, max_bytes=5000))
        if stderr_path.exists():
            print("\n--- worker.err.log ---", file=sys.stderr)
            print(tail_text(stderr_path, max_bytes=5000), file=sys.stderr)
        pid_file().unlink(missing_ok=True)
        return 1

    print(f"DeskFlow worker запущен отдельно от Actions: PID={proc.pid}")
    print(f"Runtime: {runtime}")
    print("GitHub workspace больше не используется живым worker.")

    tail = tail_text(stdout_path, max_bytes=5000)
    if tail.strip():
        print("\n--- worker.log ---")
        print(tail)

    tail = tail_text(stderr_path, max_bytes=5000)
    if tail.strip():
        print("\n--- worker.err.log ---", file=sys.stderr)
        print(tail, file=sys.stderr)

    return 0


def status_worker() -> int:
    base = base_dir()
    pid = read_pid()

    print(f"PID file: {pid if pid else 'нет'}")
    print(f"Runtime: {runtime_dir()}")
    print(f"Venv: {venv_dir()}")

    if pid:
        command_line = worker_command_line(pid)
        alive = is_expected_worker(pid)
        print(f"Worker process: {'RUNNING' if alive else 'NOT RUNNING / STALE PID'}")
        if command_line:
            print(command_line)

    for name in ("worker.log", "worker.err.log"):
        path = base / name
        print(f"\n--- {name} ---")
        if not path.exists():
            print("(файл отсутствует)")
            continue
        tail = tail_text(path, max_bytes=12_000)
        print(tail if tail else "(пусто)")

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
