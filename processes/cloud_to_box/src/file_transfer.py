from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from common.bitrix import BitrixClient
from .reporting import Report

LOG = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _filename_from_headers(url: str, headers: Mapping[str, str], fallback: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.I)
    if match:
        return unquote(match.group(1)).strip('"')
    match = re.search(r'filename="?([^";]+)', disposition, flags=re.I)
    if match:
        return match.group(1)
    name = Path(urlparse(url).path).name
    return unquote(name) or fallback


class FileTransfer:
    """Copy files from the cloud portal to a folder on the boxed portal.

    Bitrix task fields normally return an *attachment record* ID, while some
    activity/file responses return a Disk object ID or a direct URL. The
    methods below deliberately accept all three forms and keep an idempotent
    source-reference -> target-Disk-object map.
    """

    def __init__(
        self,
        source: BitrixClient,
        target: BitrixClient,
        report: Report,
        *,
        target_folder_id: int = 0,
        folder_name: str = "B24 migration files",
        max_bytes: int = 100 * 1024 * 1024,
    ):
        self.source = source
        self.target = target
        self.report = report
        self.target_folder_id = int(target_folder_id or 0)
        self.folder_name = folder_name
        self.max_bytes = max_bytes
        self._target_file_names: dict[str, int] | None = None

    def ensure_target_folder(self) -> int:
        if self.target_folder_id:
            return self.target_folder_id
        current = self.target.call("user.current") or {}
        current_id = _text(current.get("ID"))
        storages = self.target.list_all("disk.storage.getlist", {})
        storage = next(
            (
                item for item in storages
                if _text(item.get("ENTITY_TYPE")).casefold() == "user"
                and _text(item.get("ENTITY_ID")) == current_id
            ),
            None,
        )
        if storage is None:
            storage = next((item for item in storages if item.get("ROOT_OBJECT_ID")), None)
        if storage is None:
            raise RuntimeError("No writable Bitrix Disk storage was found for the target webhook user")
        root_id = int(storage.get("ROOT_OBJECT_ID") or 0)
        if not root_id:
            raise RuntimeError("Target storage has no ROOT_OBJECT_ID")
        children = self.target.list_all("disk.folder.getchildren", {"id": root_id})
        existing = next(
            (
                item for item in children
                if _text(item.get("TYPE")).casefold() == "folder"
                and _text(item.get("NAME")).strip().casefold() == self.folder_name.casefold()
            ),
            None,
        )
        if existing:
            self.target_folder_id = int(existing["ID"])
            return self.target_folder_id
        created = self.target.call("disk.folder.addsubfolder", {"id": root_id, "data": {"NAME": self.folder_name}}) or {}
        if isinstance(created, dict):
            self.target_folder_id = int(created.get("ID") or created.get("id") or 0)
        else:
            self.target_folder_id = int(created or 0)
        if not self.target_folder_id:
            raise RuntimeError("Bitrix did not return an ID for the migration files folder")
        return self.target_folder_id


    def _load_target_file_names(self) -> dict[str, int]:
        if self._target_file_names is not None:
            return self._target_file_names
        folder_id = self.ensure_target_folder()
        children = self.target.list_all("disk.folder.getchildren", {"id": folder_id})
        self._target_file_names = {
            _text(item.get("NAME")): int(item.get("ID") or 0)
            for item in children
            if _text(item.get("TYPE")).casefold() == "file" and int(item.get("ID") or 0)
        }
        return self._target_file_names

    @staticmethod
    def _migration_filename(key: str, original_name: str) -> str:
        safe_name = Path(original_name).name or "file.bin"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        return f"B24MIG_{digest}__{safe_name}"

    def _upload(self, key: str, name: str, content: bytes) -> int:
        if len(content) > self.max_bytes:
            raise RuntimeError(f"file is larger than configured limit: {len(content)} bytes")
        folder_id = self.ensure_target_folder()
        upload_name = self._migration_filename(key, name)
        existing = self._load_target_file_names().get(upload_name)
        if existing:
            self.report.maps["files"][key] = existing
            self.report.add("transfer_file", "FILE", key, "DISK_FILE", existing, "SKIP", upload_name)
            return existing
        encoded = base64.b64encode(content).decode("ascii")
        result = self.target.call(
            "disk.folder.uploadfile",
            {
                "id": folder_id,
                "data": {"NAME": upload_name},
                "fileContent": [upload_name, encoded],
                "generateUniqueName": True,
            },
        ) or {}
        target_id = int(result.get("ID") or result.get("id") or 0) if isinstance(result, dict) else int(result or 0)
        if not target_id:
            raise RuntimeError("disk.folder.uploadfile did not return a Disk object ID")
        self.report.maps["files"][key] = target_id
        if self._target_file_names is not None:
            self._target_file_names[upload_name] = target_id
        self.report.add("transfer_file", "FILE", key, "DISK_FILE", target_id, "OK", upload_name)
        return target_id

    def _download_and_upload(self, key: str, url: str, name: str) -> int:
        content, headers = self.source.download(url)
        final_name = name or _filename_from_headers(url, headers, f"file_{key.replace(':', '_')}")
        return self._upload(key, final_name, content)


    def _download_payload(self, key: str, url: str, name: str) -> tuple[str, str, bytes]:
        content, headers = self.source.download(url)
        if len(content) > self.max_bytes:
            raise RuntimeError(f"file is larger than configured limit: {len(content)} bytes")
        final_name = name or _filename_from_headers(url, headers, f"file_{key.replace(':', '_')}")
        upload_name = self._migration_filename(key, final_name)
        return key, upload_name, content

    def activity_payload_reference(
        self, source_id: Any, *, prefer_attached: bool = False
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Build the Base64 fileData payload required by crm.activity.* fields."""
        raw_id = _text(source_id).strip().removeprefix("n")
        if not raw_id.isdigit():
            self.report.add("read_activity_file", "FILE", raw_id, "CRM_FILE", "", "WARN", "invalid numeric file reference")
            return None
        candidates = ("attached", "disk") if prefer_attached else ("disk", "attached")
        errors: list[str] = []
        for kind in candidates:
            key = f"{kind}:{raw_id}"
            try:
                if kind == "attached":
                    meta = self.source.call("disk.attachedObject.get", {"id": int(raw_id)}) or {}
                else:
                    meta = self.source.call("disk.file.get", {"id": int(raw_id)}) or {}
                url = _text(meta.get("DOWNLOAD_URL") or meta.get("downloadUrl") or meta.get("URL_DOWNLOAD"))
                name = _text(meta.get("NAME") or meta.get("name")) or f"file_{raw_id}"
                if not url:
                    raise RuntimeError(f"{kind} metadata returned no download URL")
                resolved_key, upload_name, content = self._download_payload(key, url, name)
                payload = {"fileData": [upload_name, base64.b64encode(content).decode("ascii")]}
                return resolved_key, upload_name, payload
            except Exception as exc:
                errors.append(f"{kind}: {exc}")
        self.report.add("read_activity_file", "FILE", raw_id, "CRM_FILE", "", "WARN", "; ".join(errors))
        return None

    def activity_payload_url(
        self, source_key: str, url: str, name: str = ""
    ) -> tuple[str, str, dict[str, Any]] | None:
        key = f"url:{source_key}"
        try:
            resolved_key, upload_name, content = self._download_payload(key, url, name)
            payload = {"fileData": [upload_name, base64.b64encode(content).decode("ascii")]}
            return resolved_key, upload_name, payload
        except Exception as exc:
            self.report.add("read_activity_file", "FILE", source_key, "CRM_FILE", "", "WARN", str(exc))
            return None

    def transfer_reference(self, source_id: Any, *, prefer_attached: bool = True) -> int | None:
        """Transfer a Bitrix file reference.

        `UF_TASK_WEBDAV_FILES` and comment/checklist attachments usually expose
        an attached-object ID. Some activity responses expose a Disk object ID.
        We try the likely representation first and then the alternative.
        """
        raw_id = _text(source_id).strip().removeprefix("n")
        if not raw_id.isdigit():
            self.report.add("transfer_file", "FILE", raw_id, "DISK_FILE", "", "WARN", "invalid numeric file reference")
            return None
        candidates = ("attached", "disk") if prefer_attached else ("disk", "attached")
        errors: list[str] = []
        for kind in candidates:
            key = f"{kind}:{raw_id}"
            if key in self.report.maps["files"]:
                return self.report.maps["files"][key]
            try:
                if kind == "attached":
                    meta = self.source.call("disk.attachedObject.get", {"id": int(raw_id)}) or {}
                else:
                    meta = self.source.call("disk.file.get", {"id": int(raw_id)}) or {}
                url = _text(meta.get("DOWNLOAD_URL") or meta.get("downloadUrl") or meta.get("URL_DOWNLOAD"))
                name = _text(meta.get("NAME") or meta.get("name")) or f"file_{raw_id}"
                if not url:
                    raise RuntimeError(f"{kind} metadata returned no download URL")
                target_id = self._download_and_upload(key, url, name)
                # Alias both representations when Bitrix tells us the underlying Disk object.
                object_id = _text(meta.get("OBJECT_ID") or meta.get("objectId"))
                if object_id.isdigit():
                    self.report.maps["files"].setdefault(f"disk:{object_id}", target_id)
                return target_id
            except Exception as exc:
                errors.append(f"{kind}: {exc}")
        self.report.add("transfer_file", "FILE", raw_id, "DISK_FILE", "", "WARN", "; ".join(errors))
        return None

    def transfer_disk_file(self, source_file_id: Any) -> int | None:
        """Backward-compatible alias, preferring a Disk object ID."""
        return self.transfer_reference(source_file_id, prefer_attached=False)

    def transfer_attached_object(self, source_attachment_id: Any) -> int | None:
        return self.transfer_reference(source_attachment_id, prefer_attached=True)

    def transfer_url(self, source_key: str, url: str, name: str = "") -> int | None:
        key = f"url:{source_key}"
        if key in self.report.maps["files"]:
            return self.report.maps["files"][key]
        try:
            return self._download_and_upload(key, url, name)
        except Exception as exc:
            self.report.add("transfer_file", "FILE", source_key, "DISK_FILE", "", "WARN", str(exc))
            return None
