from __future__ import annotations

import json
import logging
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

LOG = logging.getLogger(__name__)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF_RE = re.compile(r"([A-Z]+)")


def _column_index(cell_ref: str) -> int:
    match = _CELL_REF_RE.match(cell_ref or "")
    if not match:
        return 0
    value = 0
    for char in match.group(1):
        value = value * 26 + (ord(char) - 64)
    return max(0, value - 1)


def _json_or_text(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if text == "":
        return None
    stripped = text.strip()
    if (stripped.startswith("[") and stripped.endswith("]")) or (
        stripped.startswith("{") and stripped.endswith("}")
    ):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return text


class _XlsxRegistry:
    """Minimal read-only XLSX reader for exporter sheets.

    The exporter serializes arrays and dictionaries as compact JSON strings.
    They are restored to Python values here, so the migration receives the same
    structure as from json/datasets while the Excel workbook remains the main
    transfer registry.
    """

    def __init__(self, source: bytes | Path):
        self._zip = zipfile.ZipFile(BytesIO(source) if isinstance(source, bytes) else source)
        self._shared_strings = self._read_shared_strings()
        self._sheet_paths = self._read_sheet_paths()
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def close(self) -> None:
        self._zip.close()

    def has_sheet(self, name: str) -> bool:
        return name in self._sheet_paths

    def _read_shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self._zip.namelist():
            return []
        root = ET.fromstring(self._zip.read("xl/sharedStrings.xml"))
        values: list[str] = []
        for item in root.findall(f"{{{_MAIN_NS}}}si"):
            values.append("".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t")))
        return values

    def _read_sheet_paths(self) -> dict[str, str]:
        workbook = ET.fromstring(self._zip.read("xl/workbook.xml"))
        relationships = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
        relation_targets = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
        }
        result: dict[str, str] = {}
        for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
            relation_id = sheet.attrib.get(f"{{{_REL_NS}}}id", "")
            target = relation_targets.get(relation_id, "")
            if target:
                result[sheet.attrib.get("name", "")] = "xl/" + target.lstrip("/")
        return result

    def _cell_value(self, cell: ET.Element) -> Any:
        cell_type = cell.attrib.get("t")
        if cell_type == "inlineStr":
            return "".join(node.text or "" for node in cell.iter(f"{{{_MAIN_NS}}}t"))
        value_node = cell.find(f"{{{_MAIN_NS}}}v")
        if value_node is None:
            return None
        raw = value_node.text or ""
        if cell_type == "s":
            try:
                return self._shared_strings[int(raw)]
            except (ValueError, IndexError):
                return raw
        if cell_type == "b":
            return raw == "1"
        return raw

    def rows(self, sheet_name: str) -> list[dict[str, Any]]:
        if sheet_name in self._cache:
            return [dict(row) for row in self._cache[sheet_name]]
        path = self._sheet_paths.get(sheet_name)
        if not path:
            return []
        root = ET.fromstring(self._zip.read(path))
        xml_rows = root.findall(f".//{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row")
        if not xml_rows:
            self._cache[sheet_name] = []
            return []

        matrix: list[list[Any]] = []
        max_col = 0
        for xml_row in xml_rows:
            values: dict[int, Any] = {}
            for cell in xml_row.findall(f"{{{_MAIN_NS}}}c"):
                index = _column_index(cell.attrib.get("r", ""))
                values[index] = self._cell_value(cell)
                max_col = max(max_col, index)
            matrix.append([values.get(index) for index in range(max_col + 1)])

        headers = [str(value or "").strip() for value in matrix[0]]
        result: list[dict[str, Any]] = []
        for values in matrix[1:]:
            row: dict[str, Any] = {}
            for index, header in enumerate(headers):
                if not header:
                    continue
                value = values[index] if index < len(values) else None
                row[header] = _json_or_text(value)
            if any(value not in (None, "", [], {}) for value in row.values()):
                result.append(row)
        self._cache[sheet_name] = result
        return [dict(row) for row in result]


class DumpReader:
    """Read a Bitrix24 dump, preferring its Excel workbook as the registry.

    JSON datasets remain a technical fallback and are still used when a sheet
    is absent. The Excel workbook is the primary row registry because it is the
    user-verifiable source included in the dump.
    """

    def __init__(self, source: str | Path, *, prefer_excel: bool = True):
        self.path = Path(source)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._zip: zipfile.ZipFile | None = None
        self._prefix = ""
        self._root: Path | None = None
        self._excel: _XlsxRegistry | None = None
        self.registry_source = "json"

        if self.path.is_file():
            self._zip = zipfile.ZipFile(self.path)
            names = self._zip.namelist()
            manifest_candidates = [name for name in names if name.endswith("manifest.json")]
            if not manifest_candidates:
                raise ValueError(f"manifest.json not found in dump ZIP: {self.path}")
            manifest_name = min(manifest_candidates, key=len)
            self._prefix = manifest_name[: -len("manifest.json")]
            if prefer_excel:
                xlsx_candidates = [
                    name for name in names
                    if name.startswith(self._prefix) and name.lower().endswith(".xlsx")
                ]
                if xlsx_candidates:
                    xlsx_name = min(xlsx_candidates, key=len)
                    self._excel = _XlsxRegistry(self._zip.read(xlsx_name))
                    self.registry_source = "excel"
        else:
            manifest_candidates = list(self.path.rglob("manifest.json"))
            if not manifest_candidates:
                raise ValueError(f"manifest.json not found in dump directory: {self.path}")
            manifest = min(manifest_candidates, key=lambda p: len(p.parts))
            self._root = manifest.parent
            if prefer_excel:
                xlsx_candidates = list(self._root.glob("*.xlsx"))
                if xlsx_candidates:
                    self._excel = _XlsxRegistry(min(xlsx_candidates, key=lambda p: len(p.name)))
                    self.registry_source = "excel"

    def close(self) -> None:
        if self._excel:
            self._excel.close()
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
        if self._root is None:
            raise FileNotFoundError(relative)
        path = self._root / relative
        if not path.exists():
            raise FileNotFoundError(path)
        return path.read_text(encoding="utf-8")

    def manifest(self) -> dict[str, Any]:
        value = json.loads(self._read_text("manifest.json"))
        if not isinstance(value, dict):
            raise ValueError("Invalid manifest format")
        value = dict(value)
        value["registry_source"] = self.registry_source
        return value

    def dataset_names(self) -> list[str]:
        return [str(item.get("dataset")) for item in self.manifest().get("datasets", []) if item.get("dataset")]

    def _json_rows(self, dataset: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(self._read_text(f"json/datasets/{dataset}.json"))
        except FileNotFoundError:
            return []
        if not isinstance(value, list):
            raise ValueError(f"Dataset {dataset} must contain a JSON array")
        return [dict(item) for item in value if isinstance(item, dict)]

    def rows(self, dataset: str) -> list[dict[str, Any]]:
        if self._excel and self._excel.has_sheet(dataset):
            excel_rows = self._excel.rows(dataset)
            # Empty sheets are valid; use the manifest count to distinguish them
            # from a damaged/unreadable sheet before falling back to JSON.
            expected = next(
                (
                    int(item.get("rows") or 0)
                    for item in self.manifest().get("datasets", [])
                    if str(item.get("dataset")) == dataset
                ),
                None,
            )
            if expected is None or len(excel_rows) == expected:
                return excel_rows
            LOG.warning(
                "Excel registry row count differs for %s: excel=%s expected=%s; JSON fallback is used",
                dataset,
                len(excel_rows),
                expected,
            )
        return self._json_rows(dataset)
