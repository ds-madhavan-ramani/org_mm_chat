"""
sharepoint_ingest.py — list & ingest files from a user-supplied SharePoint
folder URL. Two-step, both driven from the Streamlit Data Sources page:

  1. list_sharepoint_folder(folder_url)   -> [DriveItem, ...] for checkboxes
  2. ingest_selected_files(project, items) -> [IngestResult, ...]

Unlike ocms-llm-wiki's 01_sync_sharepoint.py, there is no hardcoded site,
no LY/month folder convention, and no automatic file-priority selection —
the user points at any folder and ticks what they want.

Every selected item is re-downloaded and re-parsed on each run (not just
new ones) so that an edit to an already-ingested SharePoint file is
detected — the per-item SHAREPOINT_ITEM_ID identifies the same logical
document across edits, and ingest_selected_files updates its existing
RAW_DOCUMENTS row in place (status "UPDATED") when the freshly-parsed
content's hash differs from what's stored, rather than treating every
run as either brand-new or an unchanged duplicate.
"""

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Tuple

from config import ProjectConfig, GRAPH_TENANT_ID, GRAPH_CLIENT_ID, MIN_PARSED_TEXT_CHARS
from ingestion.xlsx_parser import (
    is_xlsx, parse_xlsx_to_text, extract_column_values, list_headers, normalize_token,
)
from utils import graph_client
from utils.logging_utils import get_logger, log_event
from utils.sql_utils import SQLBuilder

logger = get_logger(__name__)

# BIS_ORG_Meeting_Minutes.xlsx is a hand-maintained register, not raw minutes
# content: for each meeting it names the one canonical file out of however
# many Draft/Initial/Final/Updated/Revised versions exist in the folder.
# Matched by normalized substring so spacing/case/punctuation drift in the
# actual file name (e.g. "BIS ORG Meeting Minutes - Register.xlsx") doesn't
# break the match.
_REGISTER_NAME_TOKEN = normalize_token("BIS_ORG_Meeting_Minutes")
_REGISTER_FILENAME_HEADERS = ("FileName", "File Name")


@dataclass
class IngestResult:
    file_name: str
    status: str          # 'INGESTED' | 'UPDATED' | 'SKIPPED_DUPLICATE' | 'FAILED'
    doc_id: int = None
    error: str = None


def list_sharepoint_folder(session, folder_url: str) -> List[graph_client.DriveItem]:
    """Resolves a pasted SharePoint folder URL and lists its files, recursively."""
    token = graph_client._get_access_token(
        GRAPH_TENANT_ID, GRAPH_CLIENT_ID, graph_client.get_client_secret()
    )
    root = graph_client.resolve_folder(token, folder_url)
    if not root.is_folder:
        raise graph_client.GraphError(f"'{root.name}' is a file, not a folder")

    # drive_id is embedded in the resolved item's parentReference in the raw
    # Graph response; resolve_folder currently exposes item_id only, so we
    # re-resolve via the shares endpoint to also capture driveId.
    drive_id = _get_drive_id(token, folder_url)
    return graph_client.list_folder(token, drive_id, root.item_id, recursive=True)


def ingest_selected_files(session, project: ProjectConfig, folder_url: str,
                          selected_items: List[graph_client.DriveItem]) -> List[IngestResult]:
    token = graph_client._get_access_token(
        GRAPH_TENANT_ID, GRAPH_CLIENT_ID, graph_client.get_client_secret()
    )
    drive_id = _get_drive_id(token, folder_url)

    results: List[IngestResult] = []
    schema = project.qualified_schema
    stage = project.qualified_stage

    for item in selected_items:
        try:
            # Keyed on Graph item id (a stable per-file identity, not a
            # content hash) so a file that's been edited since it was last
            # ingested is detected and updated in place, rather than
            # skipped as an unchanged duplicate.
            existing = session.sql(
                f"SELECT DOC_ID, SOURCE_HASH FROM {schema}.RAW_DOCUMENTS WHERE SHAREPOINT_ITEM_ID = ?",
                params=[item.item_id],
            ).collect()

            raw_bytes = graph_client.download_file(token, drive_id, item.item_id)
            stage_path = f"{stage}/{item.name}"
            session.file.put_stream(_to_stream(raw_bytes), stage_path,
                                    auto_compress=False, overwrite=True)

            if is_xlsx(item.name):
                raw_text = parse_xlsx_to_text(raw_bytes)
            else:
                # AI_PARSE_DOCUMENT takes a FILE-typed argument, built via
                # TO_FILE('@stage', 'relative_path') — not the VARCHAR URL
                # BUILD_SCOPED_FILE_URL returns ("Invalid argument types
                # for function 'AI_PARSE_DOCUMENT$V4': (VARCHAR, VARIANT)"
                # when given one). Unlike BUILD_SCOPED_FILE_URL, TO_FILE's
                # stage argument is an ordinary quoted string, so both
                # arguments can be bind parameters here.
                parsed = session.sql(
                    "SELECT AI_PARSE_DOCUMENT(TO_FILE(?, ?), "
                    "PARSE_JSON('{\"mode\": \"OCR\"}')) AS RESULT",
                    params=[f"@{stage}", item.name],
                ).collect()
                raw_text = _extract_text(parsed[0]["RESULT"])

            if len(raw_text.strip()) < MIN_PARSED_TEXT_CHARS:
                results.append(IngestResult(item.name, "FAILED",
                                             error="Parsed text too short"))
                continue

            source_hash = hashlib.sha256(raw_text.encode()).hexdigest()

            if existing and existing[0]["SOURCE_HASH"] == source_hash:
                results.append(IngestResult(item.name, "SKIPPED_DUPLICATE",
                                             doc_id=existing[0]["DOC_ID"]))
                continue

            session.sql(
                SQLBuilder.build_merge_raw_document_by_sharepoint_item(schema),
                params=[item.name, stage_path, "SHAREPOINT", item.item_id,
                        None, raw_text, source_hash],
            ).collect()

            doc_id = session.sql(
                f"SELECT DOC_ID FROM {schema}.RAW_DOCUMENTS WHERE SHAREPOINT_ITEM_ID = ?",
                params=[item.item_id],
            ).collect()[0]["DOC_ID"]
            status = "UPDATED" if existing else "INGESTED"
            results.append(IngestResult(item.name, status, doc_id=doc_id))

            log_event(logger, "INGEST_SHAREPOINT_FILE", project.project_code,
                      file=item.name, status=status)

        except Exception as e:  # noqa: BLE001
            logger.exception("EVENT=INGEST_SHAREPOINT_ERROR file=%s", item.name)
            results.append(IngestResult(item.name, "FAILED", error=str(e)))

    _log_sync_run(session, project, results)
    return results


def find_register_items(listing: List[graph_client.DriveItem]) -> List[graph_client.DriveItem]:
    """Items in `listing` whose name matches the BIS_ORG_Meeting_Minutes
    register naming pattern — split out from reading them so callers (the
    Data Sources page) can show *whether a register was found at all*, not
    just the (possibly silently empty) end result."""
    return [
        item for item in listing
        if _REGISTER_NAME_TOKEN in normalize_token(item.name) and is_xlsx(item.name)
    ]


def read_register(
    session, folder_url: str, register_items: List[graph_client.DriveItem]
) -> Tuple[List[str], Dict[str, Dict[str, List[str]]]]:
    """
    Downloads each item in register_items once and both (a) reads its
    FileName column across every sheet and (b) records each sheet's row-1
    headers — so a caller can show *why* zero names were found (wrong
    column header text, wrong sheet, ...) without a second download.

    Returns (names, headers_by_item); headers_by_item is keyed by each
    workbook's display path (falls back to its name if path is blank), so
    two register workbooks that happen to share a file name are still
    distinguishable in diagnostics.
    """
    if not register_items:
        return [], {}

    token = graph_client._get_access_token(
        GRAPH_TENANT_ID, GRAPH_CLIENT_ID, graph_client.get_client_secret()
    )
    drive_id = _get_drive_id(token, folder_url)

    names: List[str] = []
    headers_by_item: Dict[str, Dict[str, List[str]]] = {}
    for item in register_items:
        raw_bytes = graph_client.download_file(token, drive_id, item.item_id)
        names.extend(extract_column_values(raw_bytes, _REGISTER_FILENAME_HEADERS))
        headers_by_item[item.path or item.name] = list_headers(raw_bytes)
    return names, headers_by_item


def filename_match_keys(name: str) -> set:
    """A file name's comparison keys: itself and its extension-stripped stem
    (both lowercased), so a register entry stored without an extension
    ('Final Minutes - March 2024') still matches the real file name with
    one ('Final Minutes - March 2024.pdf')."""
    n = name.strip().lower()
    stem = n.rsplit(".", 1)[0] if "." in n else n
    return {n, stem}


def _get_drive_id(token: str, folder_url: str) -> str:
    import base64
    encoded = base64.urlsafe_b64encode(folder_url.encode()).decode().rstrip("=")
    data = graph_client._graph_get(
        f"{graph_client.GRAPH_BASE}/shares/u!{encoded}/driveItem?$select=parentReference", token
    )
    return data["parentReference"]["driveId"]


def _to_stream(raw_bytes: bytes):
    import io
    return io.BytesIO(raw_bytes)


def _extract_text(parse_result) -> str:
    import json
    data = json.loads(parse_result) if isinstance(parse_result, str) else parse_result
    return data.get("content", "") if isinstance(data, dict) else str(data)


def _log_sync_run(session, project: ProjectConfig, results: List[IngestResult]):
    from config import DATABASE, CATALOG_SCHEMA
    ingested = sum(1 for r in results if r.status in ("INGESTED", "UPDATED"))
    skipped = sum(1 for r in results if r.status == "SKIPPED_DUPLICATE")
    failed = sum(1 for r in results if r.status == "FAILED")
    session.sql(
        f"""INSERT INTO {DATABASE}.{CATALOG_SCHEMA}.PROJECT_SYNC_LOG
            (PROJECT_ID, SOURCE_TYPE, FILES_FOUND, FILES_SYNCED, FILES_SKIPPED, FILES_FAILED)
            SELECT (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                    WHERE PROJECT_CODE = ?), 'SHAREPOINT', ?, ?, ?, ?""",
        params=[project.project_code, len(results), ingested, skipped, failed],
    ).collect()
