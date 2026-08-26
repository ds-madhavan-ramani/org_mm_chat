"""
xlsx_parser.py — native XLSX parsing for spreadsheet-sourced documents (e.g.
BIS_ORG_Meeting_Minutes.xlsx), bypassing AI_PARSE_DOCUMENT.

AI_PARSE_DOCUMENT is OCR/layout-oriented and not built for tabular data — the
sibling ocms_compliance_assistant_snowflake project explicitly marks
XLSX/XLS/MSG files as SKIPPED for that function. A spreadsheet's meeting
minutes are structured rows, not a scanned page, so read them directly.

Deliberately stdlib-only (zipfile + xml.etree.ElementTree) rather than
pandas/openpyxl: adding `openpyxl` to environment.yml broke the deployed
Streamlit app outright — it failed at load, before any page code ran, with
Snowflake's generic sandbox-bootstrap error ("Python Interpreter Error:
TypeError: bad argument type for built-in operation"), because openpyxl
isn't resolvable in this account's Anaconda channel snapshot for
warehouse-runtime Streamlit apps. An .xlsx file is just a zip of XML parts
(worksheets + a shared-string table), so no third-party package is actually
needed to read one.

The serialized text still lands in RAW_DOCUMENTS.RAW_TEXT and stays
addressable by the character offsets DOCUMENT_INDEX.NODE_TEXT_REF already
uses, so no schema change is needed downstream.
"""

import io
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

XLSX_EXTENSIONS = (".xlsx", ".xlsm")

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def is_xlsx(file_name: str) -> bool:
    return file_name.lower().endswith(XLSX_EXTENSIONS)


def normalize_token(s: str) -> str:
    """Lowercases and strips everything but letters/digits, so 'File Name',
    'File_Name', 'FILE-NAME' and 'filename' all compare equal — used for
    matching both column headers and register/actual file names, which are
    typed inconsistently across a workbook maintained by hand."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _col_index(cell_ref: str) -> int:
    """'C7' -> 2 (0-based column index)."""
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx - 1


def _load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings = []
    for si in root.findall(f"{{{_MAIN_NS}}}si"):
        # Concatenate every <t> run so rich-text cells (multiple <r><t>) still
        # come through as one string.
        strings.append("".join(t.text or "" for t in si.iter(f"{{{_MAIN_NS}}}t")))
    return strings


def _load_sheets(zf: zipfile.ZipFile) -> Dict[str, str]:
    """Returns {sheet_name: 'xl/worksheets/sheetN.xml'} in workbook order."""
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {
        rel.get("Id"): rel.get("Target")
        for rel in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    sheets = {}
    for sheet in wb_root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet"):
        name = sheet.get("name") or ""
        rid = sheet.get(f"{{{_DOC_REL_NS}}}id")
        target = rid_to_target.get(rid, "")
        # Targets come as either an absolute in-archive path ("/xl/worksheets/
        # sheet1.xml") or a path relative to xl/ ("worksheets/sheet1.xml") —
        # normalize both to the zip member name.
        if target.startswith("/"):
            target = target.lstrip("/")
        elif target and not target.startswith("xl/"):
            target = f"xl/{target}"
        if target in zf.namelist():
            sheets[name] = target
    return sheets


def _cell_value(cell: ET.Element, shared: List[str]) -> Optional[str]:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        is_el = cell.find(f"{{{_MAIN_NS}}}is")
        if is_el is None:
            return None
        text = "".join(t.text or "" for t in is_el.iter(f"{{{_MAIN_NS}}}t"))
        return text or None
    v_el = cell.find(f"{{{_MAIN_NS}}}v")
    if v_el is None or v_el.text is None:
        return None
    if cell_type == "s":
        idx = int(v_el.text)
        return shared[idx] if 0 <= idx < len(shared) else None
    return v_el.text


def _parse_rows(zf: zipfile.ZipFile, sheet_path: str, shared: List[str]) -> List[List[Optional[str]]]:
    root = ET.fromstring(zf.read(sheet_path))
    rows: List[List[Optional[str]]] = []
    for row_el in root.findall(f"{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row"):
        by_col: Dict[int, str] = {}
        max_col = -1
        next_col = 0
        for cell in row_el.findall(f"{{{_MAIN_NS}}}c"):
            ref = cell.get("r", "")
            col = _col_index(ref) if ref else next_col
            next_col = col + 1
            value = _cell_value(cell, shared)
            if value not in (None, ""):
                by_col[col] = value
                max_col = max(max_col, col)
        if max_col >= 0:
            rows.append([by_col.get(i) for i in range(max_col + 1)])
    return rows


def parse_xlsx_to_text(raw_bytes: bytes) -> str:
    """
    Reads every sheet and serializes it to offset-addressable plain text: one
    '=== Sheet: <name> ===' header per sheet (a natural section boundary for
    a workbook with one sheet per lease year or meeting), then one line per
    data row as 'Header: value | Header: value | ...' using row 1 as the
    header row. Fully blank rows/cells are dropped so the segmentation
    prompt sees dense text.
    """
    parts: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        shared = _load_shared_strings(zf)
        for sheet_name, sheet_path in _load_sheets(zf).items():
            rows = _parse_rows(zf, sheet_path, shared)
            if not rows:
                continue
            header = rows[0]
            parts.append(f"=== Sheet: {sheet_name} ===")
            for row in rows[1:]:
                cells = []
                for i, value in enumerate(row):
                    if value in (None, ""):
                        continue
                    col_name = header[i] if i < len(header) and header[i] else f"Column {i + 1}"
                    cells.append(f"{col_name}: {value}")
                if cells:
                    parts.append(" | ".join(cells))
            parts.append("")
    return "\n".join(parts)


def extract_column_values(raw_bytes: bytes, header_names) -> List[str]:
    """
    Reads every sheet's row-1 header and returns every non-blank value found
    under any column whose header matches one of header_names (compared via
    normalize_token, so header spelling/spacing variance doesn't matter).
    Unlike parse_xlsx_to_text (full-document serialization for indexing),
    this reads one specific column out of a register/index workbook — e.g.
    BIS_ORG_Meeting_Minutes.xlsx's FileName column, used to identify which
    single file per meeting is the canonical version.
    """
    wanted = {normalize_token(h) for h in header_names}
    values: List[str] = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        shared = _load_shared_strings(zf)
        for _, sheet_path in _load_sheets(zf).items():
            rows = _parse_rows(zf, sheet_path, shared)
            if not rows:
                continue
            header = rows[0]
            col_idxs = [i for i, h in enumerate(header) if h and normalize_token(h) in wanted]
            for row in rows[1:]:
                for i in col_idxs:
                    if i < len(row) and row[i]:
                        values.append(row[i].strip())
    return values
