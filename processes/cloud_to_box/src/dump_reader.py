from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any


class DumpReader:
    """Read exported Bitrix24 JSON datasets from a directory or ZIP archive.

    The exporter may wrap all files in one top-level directory. This reader
    locates manifest.json and json/datasets automatically, so the migration
    does not depend on the archive folder name.
    """

    _validated_files: set[tuple[str, int, int]] = set()

    def __init__(self, source: str | Path):
        self.path = Path(source)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._zip: zipfile.ZipFile | None = None
        self._prefix = ""
        if self.path.is_file():
            self._zip = zipfile.ZipFile(self.path)
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

    def close(self) -> None:
        if self._zip:
            self._zip.close()

    def __enter__(self) -> "DumpReader":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _read_text(self, relative: str) -> str:
        if self._zip:
            name = self._prefix + relative.replace("\\", "/")
            try:
                return self._zip.read(name).decode("utf-8")
            except KeyError as exc:
                raise FileNotFoundError(name) from exc
        path = self._root / relative
        if not path.exists():
            raise FileNotFoundError(path)
        return path.read_text(encoding="utf-8")

    def manifest(self) -> dict[str, Any]:
        value = json.loads(self._read_text("manifest.json"))
        if not isinstance(value, dict):
            raise ValueError("Invalid manifest format")
        return value

    def validate(self) -> dict[str, int]:
        """Validate every dataset declared in the export manifest."""
        if self.path.is_file():
            stat = self.path.stat()
            cache_key = (str(self.path.resolve()), stat.st_size, stat.st_mtime_ns)
            if cache_key in self._validated_files:
                return {"datasets_checked": len(self.manifest().get("datasets", []))}
        else:
            cache_key = None
        checked = 0
        for item in self.manifest().get("datasets", []):
            if not isinstance(item, dict):
                raise ValueError("Invalid dataset entry in manifest")
            relative = str(item.get("file") or "")
            if not relative:
                raise ValueError("Dataset manifest entry has no file")
            raw = self._read_text(relative).encode("utf-8")
            expected_hash = str(item.get("sha256") or "")
            if expected_hash and hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError(f"Checksum mismatch for {relative}")
            value = json.loads(raw)
            if not isinstance(value, list):
                raise ValueError(f"Dataset {relative} must contain a JSON array")
            expected_rows = item.get("rows")
            if expected_rows is not None and len(value) != int(expected_rows):
                raise ValueError(f"Row count mismatch for {relative}: {len(value)} != {expected_rows}")
            checked += 1
        if cache_key is not None:
            self._validated_files.add(cache_key)
        return {"datasets_checked": checked}

    def dataset_names(self) -> list[str]:
        return [str(item.get("dataset")) for item in self.manifest().get("datasets", []) if item.get("dataset")]

    def rows(self, dataset: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(self._read_text(f"json/datasets/{dataset}.json"))
        except FileNotFoundError:
            return []
        if not isinstance(value, list):
            raise ValueError(f"Dataset {dataset} must contain a JSON array")
        return [dict(item) for item in value if isinstance(item, dict)]
