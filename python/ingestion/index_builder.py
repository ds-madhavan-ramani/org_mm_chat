"""
index_builder.py — builds the hierarchical DOCUMENT_INDEX tree for newly
ingested documents.

ocms-llm-wiki's 02_build_index_tree.py used one segmentation prompt written
for meeting minutes (attendees / agenda items / actions & decisions). This
version keys the prompt off ProjectConfig.segmentation_profile so a new
project either reuses 'GENERIC' or a project owner adds a specialized entry
to PROMPTS without touching the calling code.

Callable from Streamlit (after ingest) or from a notebook/CLI for a manual
rebuild.
"""

from typing import List, Optional

from config import ProjectConfig
from utils.cortex_client import complete_json
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

PROMPTS = {
    "GENERIC": """You are indexing a document into a navigable tree structure.
Read the document text below and identify its natural sections (headings,
topic breaks, or logical divisions — do not force a fixed set of section
names). For each section, produce a concise 2-3 sentence summary plus the
character offsets (start, end) of that section within the original text.

Return ONLY valid JSON in this shape, no commentary:
{{
  "document_summary": "2-3 sentence summary of the whole document",
  "sections": [
    {{"title": "...", "summary": "...", "start": 0, "end": 1234}}
  ]
}}

DOCUMENT TEXT:
{text}
""",
    # Add project-specific profiles here, e.g. 'MEETING_MINUTES', 'CONTRACT',
    # and set PROJECTS.SEGMENTATION_PROFILE to match. Keeping the OCMS minutes
    # variant out of the template on purpose — it belongs to that project's
    # config row, not to shared code.
}


def build_index_for_project(session, project: ProjectConfig,
                             doc_ids: Optional[List[int]] = None,
                             rebuild: bool = False):
    """
    doc_ids=None -> index every document not yet in DOCUMENT_INDEX (or every
    document if rebuild=True).
    """
    schema = project.qualified_schema
    prompt_template = PROMPTS.get(project.segmentation_profile, PROMPTS["GENERIC"])

    if doc_ids:
        placeholders = ", ".join(["?"] * len(doc_ids))
        where = f"DOC_ID IN ({placeholders})"
        params = doc_ids
    elif rebuild:
        where = "1=1"
        params = []
    else:
        where = f"""DOC_ID NOT IN (
            SELECT DISTINCT DOC_ID FROM {schema}.DOCUMENT_INDEX
        )"""
        params = []

    docs = session.sql(
        f"SELECT DOC_ID, FILE_NAME, RAW_TEXT FROM {schema}.RAW_DOCUMENTS WHERE {where}",
        params=params,
    ).collect()

    for doc in docs:
        _index_one_document(session, project, doc["DOC_ID"], doc["FILE_NAME"],
                             doc["RAW_TEXT"], prompt_template, rebuild)

    return len(docs)


def _index_one_document(session, project: ProjectConfig, doc_id: int, file_name: str,
                         raw_text: str, prompt_template: str, rebuild: bool):
    schema = project.qualified_schema
    text = raw_text[: project.max_document_chars]

    if rebuild:
        session.sql(f"DELETE FROM {schema}.DOCUMENT_INDEX WHERE DOC_ID = ?",
                    params=[doc_id]).collect()

    try:
        result = complete_json(session, project.active_model,
                                prompt_template.format(text=text))

        root_id = session.sql(
            f"""INSERT INTO {schema}.DOCUMENT_INDEX
                (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF)
                SELECT ?, NULL, 'document', ?, ?, ?""",
            params=[doc_id, file_name, result.get("document_summary", ""),
                    f"0:{len(text)}"],
        ).collect()
        root_node_id = session.sql(
            f"""SELECT NODE_ID FROM {schema}.DOCUMENT_INDEX
                WHERE DOC_ID = ? AND PARENT_NODE_ID IS NULL
                ORDER BY NODE_ID DESC LIMIT 1""",
            params=[doc_id],
        ).collect()[0]["NODE_ID"]

        for section in result.get("sections", []):
            session.sql(
                f"""INSERT INTO {schema}.DOCUMENT_INDEX
                    (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF)
                    SELECT ?, ?, 'section', ?, ?, ?""",
                params=[doc_id, root_node_id, section.get("title", ""),
                        section.get("summary", ""),
                        f"{section.get('start', 0)}:{section.get('end', len(text))}"],
            ).collect()

        log_event(logger, "INDEX_BUILD", project.project_code,
                  doc_id=doc_id, sections=len(result.get("sections", [])))

    except Exception:
        logger.exception("EVENT=INDEX_BUILD_ERROR doc_id=%s file=%s", doc_id, file_name)
        raise
