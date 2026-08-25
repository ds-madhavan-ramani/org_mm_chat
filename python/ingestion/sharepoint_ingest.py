"""
sharepoint_ingest.py — list & ingest files from a user-supplied SharePoint
folder URL. Two-step, both driven from the Streamlit Data Sources page:

  1. list_sharepoint_folder(folder_url)   -> [DriveItem, ...] for checkboxes
  2. ingest_selected_files(project, items) -> [IngestResult, ...]

Unlike ocms-llm-wiki's 01_sync_sharepoint.py, there is no hardcoded site,
no LY/month folder convention, and no automatic file-priority selection —
the user points at any folder and ticks what they want.
"""

import hashlib
from dataclasses import dataclass
from typing import List

from config import ProjectConfig, GRAPH_TENANT_ID, GRAPH_CLIENT_ID, MIN_PARSED_TEXT_CHARS
from utils import graph_client
from utils.logging_utils import get_logger, log_event
from utils.sql_utils import SQLBuilder

logger = get_logger(__name__)


@dataclass
class IngestResult:
    file_name: str
    status: str
    doc_id: int = None
    error: str = None


def list_sharepoint_folder(session, folder_url: str) -> List[graph_client.DriveItem]:
    """Resolves a pasted SharePoint folder URL and lists its files, recursively."""
    token = graph_client._get_access_token(
        GRAPH_TENANT_ID, GRAPH_CLIENT_ID, graph_client.get_client_secret(session)
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
        GRAPH_TENANT_ID, GRAPH_CLIENT_ID, graph_client.get_client_secret(session)
    )
    drive_id = _get_drive_id(token, folder_url)

    results: List[IngestResult] = []
    schema = project.qualified_schema
    stage = project.qualified_stage

    for item in selected_items:
        try:
            # Dedup on Graph item id — re-running ingest on an unchanged
            # folder should not re-download/re-parse files already ingested.
            existing = session.sql(
                f"SELECT DOC_ID FROM {schema}.RAW_DOCUMENTS WHERE SHAREPOINT_ITEM_ID = ?",
                params=[item.item_id],
            ).collect()
            if existing:
                results.append(IngestResult(item.name, "SKIPPED_DUPLICATE",
                                             doc_id=existing[0]["DOC_ID"]))
                continue

            raw_bytes = graph_client.download_file(token, drive_id, item.item_id)
            stage_path = f"{stage}/{item.name}"
            session.file.put_stream(_to_stream(raw_bytes), stage_path,
                                    auto_compress=False, overwrite=True)

            parsed = session.sql(
                "SELECT AI_PARSE_DOCUMENT(BUILD_SCOPED_FILE_URL(?, ?), "
                "PARSE_JSON('{\"mode\": \"OCR\"}')) AS RESULT",
                params=[stage, item.name],
            ).collect()
            raw_text = _extract_text(parsed[0]["RESULT"])

            if len(raw_text.strip()) < MIN_PARSED_TEXT_CHARS:
                results.append(IngestResult(item.name, "FAILED",
                                             error="Parsed text too short"))
                continue

            source_hash = hashlib.sha256(raw_text.encode()).hexdigest()
            session.sql(
                SQLBuilder.build_merge_raw_document(schema),
                params=[item.name, stage_path, "SHAREPOINT", item.item_id,
                        None, raw_text, source_hash],
            ).collect()

            doc_id = session.sql(
                f"SELECT DOC_ID FROM {schema}.RAW_DOCUMENTS WHERE SOURCE_HASH = ?",
                params=[source_hash],
            ).collect()[0]["DOC_ID"]
            results.append(IngestResult(item.name, "INGESTED", doc_id=doc_id))

            log_event(logger, "INGEST_SHAREPOINT_FILE", project.project_code,
                      file=item.name, status="INGESTED")

        except Exception as e:  # noqa: BLE001
            logger.exception("EVENT=INGEST_SHAREPOINT_ERROR file=%s", item.name)
            results.append(IngestResult(item.name, "FAILED", error=str(e)))

    _log_sync_run(session, project, results)
    return results


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
    ingested = sum(1 for r in results if r.status == "INGESTED")
    skipped = sum(1 for r in results if r.status == "SKIPPED_DUPLICATE")
    failed = sum(1 for r in results if r.status == "FAILED")
    session.sql(
        f"""INSERT INTO {DATABASE}.{CATALOG_SCHEMA}.PROJECT_SYNC_LOG
            (PROJECT_ID, SOURCE_TYPE, FILES_FOUND, FILES_SYNCED, FILES_SKIPPED, FILES_FAILED)
            SELECT (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                    WHERE PROJECT_CODE = ?), 'SHAREPOINT', ?, ?, ?, ?""",
        params=[project.project_code, len(results), ingested, skipped, failed],
    ).collect()
