from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


class DumpReader:
    """Read and verify exported Bitrix24 JSON datasets from a directory or ZIP."""

    def __init__(self, source: str | Path):
        self.path = Path(source)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._zip: zipfile.ZipFile | None = None
        self._prefix = ""
        self._root = self.path
        if self.path.is_file():
            self._zip = zipfile.ZipFile(self.path)
            self._validate_zip_members()
            names = self._zip.namelist()
            manifest_candidates = [name for name in names if name.endswith("manifest.json")]
            if not manifest_candidates:
                raise ValueError(f"manifest.json not found in dump ZIP: {self.path}")
            manifest_name = min(manifest_candidates, key=len)
            self._prefix = manifest_name[: -len("manifest.json")]
        else:
            manifest_candidates = list(self.path.rglob("manifest.json"))
            if not manifest_candidates:
                raise ValueError(f"manifest.json not found in dump directory: {self.path}")
            manifest = min(manifest_candidates, key=lambda p: len(p.parts))
            self._root = manifest.parent

    def _validate_zip_members(self) -> None:
        assert self._zip is not None
        for info in self._zip.infolist():
            name = info.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe path in dump ZIP: {name}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"symlink is not allowed in dump ZIP: {name}")

    def close(self) -> None:
        if self._zip:
            self._zip.close()

    def __enter__(self) -> "DumpReader":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _read_bytes(self, relative: str) -> bytes:
        if self._zip:
            name = self._prefix + relative.replace("\\", "/")
            try:
                return self._zip.read(name)
            except KeyError as exc:
                raise FileNotFoundError(name) from exc
        path = self._root / relative
        if not path.exists():
            raise FileNotFoundError(path)
        return path.read_bytes()

    def _read_text(self, relative: str) -> str:
        return self._read_bytes(relative).decode("utf-8")

    def manifest(self) -> dict[str, Any]:
        value = json.loads(self._read_text("manifest.json"))
        if not isinstance(value, dict):
            raise ValueError("Invalid manifest format")
        return value

    def validate_manifest(self) -> dict[str, Any]:
        manifest = self.manifest()
        datasets = manifest.get("datasets")
        if not isinstance(datasets, list):
            raise ValueError("manifest.datasets must be an array")
        checked = 0
        for item in datasets:
            if not isinstance(item, dict):
                raise ValueError("invalid dataset entry in manifest")
            dataset = str(item.get("dataset") or "").strip()
            relative = str(item.get("file") or f"json/datasets/{dataset}.json").strip()
            expected_sha = str(item.get("sha256") or "").lower().strip()
            expected_rows = item.get("rows")
            if not dataset or not expected_sha or expected_rows is None:
                raise ValueError(f"incomplete manifest entry: {item!r}")
            raw = self._read_bytes(relative)
            actual_sha = hashlib.sha256(raw).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"SHA-256 mismatch for {dataset}: expected {expected_sha}, got {actual_sha}"
                )
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, list):
                raise ValueError(f"Dataset {dataset} must contain a JSON array")
            if len(value) != int(expected_rows):
                raise ValueError(
                    f"row count mismatch for {dataset}: expected {expected_rows}, got {len(value)}"
                )
            checked += 1
        return {"ok": True, "datasets_checked": checked}

    def dataset_names(self) -> list[str]:
        return [
            str(item.get("dataset"))
            for item in self.manifest().get("datasets", [])
            if isinstance(item, dict) and item.get("dataset")
        ]

    def rows(self, dataset: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(self._read_text(f"json/datasets/{dataset}.json"))
        except FileNotFoundError:
            return []
        if not isinstance(value, list):
            raise ValueError(f"Dataset {dataset} must contain a JSON array")
        return [dict(item) for item in value if isinstance(item, dict)]
