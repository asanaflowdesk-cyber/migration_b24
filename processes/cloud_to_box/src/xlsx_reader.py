from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column_index(ref: str) -> int:
    match = re.match(r"([A-Z]+)", ref)
    if not match:
        return 0
    value = 0
    for ch in match.group(1):
        value = value * 26 + ord(ch) - 64
    return value - 1


def decode_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


class XlsxReader:
    """Small read-only XLSX reader using only Python stdlib.

    It is intentionally limited to values exported by this migration project.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._zip = zipfile.ZipFile(self.path)
        self.shared_strings = self._load_shared_strings()
        self.sheets = self._load_sheet_map()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "XlsxReader":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _load_shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self._zip.namelist():
            return []
        root = ET.fromstring(self._zip.read("xl/sharedStrings.xml"))
        return ["".join(t.text or "" for t in si.iter(f"{{{MAIN_NS}}}t")) for si in root.findall(f"{{{MAIN_NS}}}si")]

    def _load_sheet_map(self) -> dict[str, str]:
        workbook = ET.fromstring(self._zip.read("xl/workbook.xml"))
        rels = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall(f"{{{PKG_REL_NS}}}Relationship")}
        result: dict[str, str] = {}
        sheets = workbook.find(f"{{{MAIN_NS}}}sheets")
        if sheets is None:
            return result
        for sheet in sheets:
            rid = sheet.attrib[f"{{{REL_NS}}}id"]
            result[sheet.attrib["name"]] = "xl/" + rel_map[rid]
        return result

    def sheet_names(self) -> list[str]:
        return list(self.sheets)

    def iter_raw_rows(self, sheet_name: str) -> Iterator[list[str]]:
        if sheet_name not in self.sheets:
            return
        root = ET.fromstring(self._zip.read(self.sheets[sheet_name]))
        sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
        if sheet_data is None:
            return
        for row in sheet_data:
            values: dict[int, str] = {}
            for cell in row:
                ref = cell.attrib.get("r", "A1")
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{{{MAIN_NS}}}v")
                inline_node = cell.find(f"{{{MAIN_NS}}}is")
                value = ""
                if cell_type == "s" and value_node is not None:
                    value = self.shared_strings[int(value_node.text or 0)]
                elif cell_type == "inlineStr" and inline_node is not None:
                    value = "".join(t.text or "" for t in inline_node.iter(f"{{{MAIN_NS}}}t"))
                elif value_node is not None:
                    value = value_node.text or ""
                values[_column_index(ref)] = value
            if values:
                row_values = [""] * (max(values) + 1)
                for index, value in values.items():
                    row_values[index] = value
                yield row_values

    def rows(self, sheet_name: str, *, decode_json: bool = True) -> list[dict[str, Any]]:
        iterator = self.iter_raw_rows(sheet_name)
        try:
            header = next(iterator)
        except StopIteration:
            return []
        result: list[dict[str, Any]] = []
        for raw in iterator:
            row: dict[str, Any] = {}
            for index, key in enumerate(header):
                if not key:
                    continue
                value: Any = raw[index] if index < len(raw) else ""
                if decode_json:
                    value = decode_jsonish(value)
                row[key] = value
            result.append(row)
        return result
