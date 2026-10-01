from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def base_dir() -> Path:
    path = Path.home() / ".flowdesk-chatbot"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        print("Активный DeskFlow worker не найден")
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


def start_worker() -> int:
    webhook = os.environ.get("TARGET_BITRIX_WEBHOOK_URL", "").strip()
    if not webhook:
        print("ERROR: не задан TARGET_BITRIX_WEBHOOK_URL", file=sys.stderr)
        return 2

    root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    python = root / "processes" / "flowdesk_chatbot" / ".venv" / "Scripts" / "python.exe"

    if not python.exists():
        print(f"ERROR: Python не найден: {python}", file=sys.stderr)
        return 3

    base = base_dir()
    stdout_path = base / "worker.log"
    stderr_path = base / "worker.err.log"
    db_path = base / "flowdesk_state.sqlite3"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env["PYTHONUNBUFFERED"] = "1"
    env["FLOWDESK_CHATBOT_DB"] = str(db_path)

    # GitHub Runner uses this marker to clean up child processes after a job.
    # Empty value prevents the detached worker from being treated as a job child.
    env["RUNNER_TRACKING_ID"] = ""

    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        # Break out of the GitHub Runner Windows Job Object. Without this,
        # the worker can start successfully and then be killed when the Action ends.
        creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

    with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
        proc = subprocess.Popen(
            [str(python), "-u", "-m", "processes.flowdesk_chatbot.bitrix_worker"],
            cwd=str(root),
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
            print(stdout_path.read_text(encoding="utf-8", errors="replace")[-5000:])
        if stderr_path.exists():
            print("\n--- worker.err.log ---", file=sys.stderr)
            print(stderr_path.read_text(encoding="utf-8", errors="replace")[-5000:], file=sys.stderr)
        pid_file().unlink(missing_ok=True)
        return 1

    print(f"DeskFlow worker запущен отдельно от Actions: PID={proc.pid}")
    print("GitHub job сейчас завершится и освободит runner.")

    if stdout_path.exists():
        tail = stdout_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        if tail.strip():
            print("\n--- worker.log ---")
            print(tail)

    if stderr_path.exists():
        tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        if tail.strip():
            print("\n--- worker.err.log ---", file=sys.stderr)
            print(tail, file=sys.stderr)

    return 0


def status_worker() -> int:
    base = base_dir()
    pid = read_pid()

    print(f"PID file: {pid if pid else 'нет'}")

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
        text = path.read_text(encoding="utf-8", errors="replace")
        print(text[-8000:] if text else "(пусто)")

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
