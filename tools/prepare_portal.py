"""Prepare the portal files and a shared key in a new private local directory."""
import argparse
from pathlib import Path
import re
import secrets
from shutil import copyfile
from urllib.parse import urlsplit


def php_string(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def private_file(path, content):
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(content)
    path.chmod(0o600)


def prepare(stage, owner, upstream, port, ca_file):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", owner):
        raise ValueError("Укажите числовой ID владельца из профиля Б24")
    parsed = urlsplit(upstream)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(char.isspace() or char == "\\" for char in upstream)
            or not (parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname == "127.0.0.1")):
        raise ValueError("Укажите loopback или внутренний HTTPS-адрес обработчика")
    if not 1 <= port <= 65535 or parsed.port == 0 or parsed.scheme == "http" and (parsed.port or 80) != port:
        raise ValueError("Проверьте порт обработчика")
    portal = Path(__file__).resolve().parents[1] / "portal"
    stage.mkdir(mode=0o700)
    private = stage / "private"
    private.mkdir(mode=0o700)
    for name in ("admin", "action"):
        target = stage / "webroot/local/tools" / ("deskflow-" + name)
        target.mkdir(parents=True)
        copyfile(portal / name / "index.php", target / "index.php")
    copyfile(portal / "deskflow-bridge.php", private / "deskflow-bridge.php")
    (private / "deskflow-bridge.php").chmod(0o600)
    key = secrets.token_hex(32)
    private_file(private / "deskflow-admin.php", "<?php\nreturn [\n"
                 f"    'upstream' => {php_string(upstream.rstrip('/'))},\n"
                 f"    'key_hex' => {php_string(key)},\n"
                 f"    'ca_file' => {php_string(ca_file)},\n];\n")
    private_file(private / "worker.env", "TARGET_BITRIX_WEBHOOK_URL=\n"
                 f"FLOWDESK_ADMIN_OWNER_ID={owner}\nFLOWDESK_ADMIN_BRIDGE_KEY={key}\n"
                 f"FLOWDESK_ADMIN_LISTEN_PORT={port}\nFLOWDESK_CHATBOT_DB=/var/lib/deskflow/flowdesk_state.sqlite3\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--upstream", default="http://127.0.0.1:8765")
    parser.add_argument("--listen-port", type=int, default=8765)
    parser.add_argument("--ca-file", default="")
    args = parser.parse_args()
    try:
        prepare(args.stage, args.owner_id, args.upstream, args.listen_port, args.ca_file)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Файлы установки подготовлены в {args.stage.resolve()}")


if __name__ == "__main__":
    main()
