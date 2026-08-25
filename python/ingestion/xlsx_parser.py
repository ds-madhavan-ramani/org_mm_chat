"""
xlsx_parser.py — native XLSX parsing for spreadsheet-sourced documents (e.g.
BIS_ORG_Meeting_Minutes.xlsx), bypassing AI_PARSE_DOCUMENT.

AI_PARSE_DOCUMENT is OCR/layout-oriented and not built for tabular data — the
sibling ocms_compliance_assistant_snowflake project explicitly marks
XLSX/XLS/MSG files as SKIPPED for that function. A spreadsheet's meeting
minutes are structured rows, not a scanned page, so read them directly with
pandas/openpyxl and serialize to plain text instead. The serialized text
still lands in RAW_DOCUMENTS.RAW_TEXT and stays addressable by the character
offsets DOCUMENT_INDEX.NODE_TEXT_REF already uses, so no schema change is
needed downstream.
"""

import io

import pandas as pd

XLSX_EXTENSIONS = (".xlsx", ".xlsm")


def is_xlsx(file_name: str) -> bool:
    return file_name.lower().endswith(XLSX_EXTENSIONS)


def parse_xlsx_to_text(raw_bytes: bytes) -> str:
    """
    Reads every sheet and serializes it to offset-addressable plain text: one
    '=== Sheet: <name> ===' header per sheet (a natural section boundary for
    a workbook with one sheet per lease year or meeting), then one line per
    row as 'Column: value | Column: value | ...'. Fully blank rows/columns
    are dropped so the segmentation prompt sees dense text.
    """
    sheets = pd.read_excel(io.BytesIO(raw_bytes), sheet_name=None, dtype=str)
    parts: list[str] = []
    for sheet_name, df in sheets.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        parts.append(f"=== Sheet: {sheet_name} ===")
        for _, row in df.iterrows():
            cells = [
                f"{col}: {val}"
                for col, val in row.items()
                if pd.notna(val) and str(val).strip()
            ]
            if cells:
                parts.append(" | ".join(cells))
        parts.append("")
    return "\n".join(parts)
