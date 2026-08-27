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

from dataclasses import dataclass, field
from typing import List, Optional

from config import ProjectConfig
from utils.cortex_client import complete_json
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

# Must stay <= DOCUMENT_INDEX.NODE_SUMMARY/NODE_TITLE's actual column
# widths (see sql/00_setup_catalog.sql) — a defensive backstop, not the
# primary control: a segmentation prompt asking for detailed summaries
# (e.g. ORG_MEETING_MINUTES) can produce a response long enough to fail
# the INSERT outright ("... is too long and would be truncated", which
# Snowflake raises as an error rather than silently truncating) instead
# of just losing a few words off the end. Truncating here first means a
# verbose response degrades gracefully instead of failing the document
# entirely.
MAX_NODE_SUMMARY_CHARS = 8000
MAX_NODE_TITLE_CHARS = 500


def _truncate(text, max_chars: int) -> str:
    text = text or ""
    return text[:max_chars]


@dataclass
class IndexResult:
    indexed: int
    failed: int
    errors: List[str] = field(default_factory=list)  # "file_name: error message"

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

    "ORG_MEETING_MINUTES": """You are indexing a set of OCMS Review Group
(ORG) meeting minutes into a navigable tree structure. The source text may
come from a Word/PDF minutes document (prose, headed sections) or from a
serialized spreadsheet (lines beginning "=== Sheet: <name> ===" followed by
"Column: value | Column: value" rows — each such block is one register of
minutes, often one sheet per lease year or per meeting).

Segment by meeting where the text allows it — one section per distinct
meeting date, or one section per "=== Sheet: ... ===" block for spreadsheet
input. Each section's summary should be detailed enough for a reader to
judge relevance without re-reading the source: call out the meeting date,
attendees if listed, and EVERY agenda item, decision, and action item
raised — write a thorough paragraph, not a one-line gloss. Quote specific
reference codes, action IDs, system/asset names, and acronyms VERBATIM
exactly as written in the source (e.g. "Action 194", "A9605", "TCMS",
"AOWP") rather than paraphrasing or omitting them — someone searching for
one of these terms later needs to find it in the summary text itself, not
just its general meaning. Do not force sections that don't exist in the
text; if the document has no clear per-meeting breaks, fall back to its
natural headings/topic breaks instead.

Return ONLY valid JSON in this shape, no commentary:
{{
  "document_summary": "3-5 sentence summary of the whole document (date range covered, overall purpose, and any recurring topics, systems, or codes discussed across meetings)",
  "sections": [
    {{"title": "e.g. 'Meeting — 12 Mar 2024' or a sheet/topic name", "summary": "...", "start": 0, "end": 1234}}
  ]
}}

DOCUMENT TEXT:
{text}
""",
}


def build_index_for_project(session, project: ProjectConfig,
                             doc_ids: Optional[List[int]] = None,
                             rebuild: bool = False) -> IndexResult:
    """
    doc_ids=None -> index every document not yet in DOCUMENT_INDEX (or every
    document if rebuild=True).

    Each document is indexed independently — one document's LLM response
    coming back malformed (e.g. not the requested JSON shape) is recorded
    as a per-document failure, not an exception that aborts every other
    document still queued in the same call.
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

    errors: List[str] = []
    indexed = 0
    for doc in docs:
        try:
            _index_one_document(session, project, doc["DOC_ID"], doc["FILE_NAME"],
                                 doc["RAW_TEXT"], prompt_template, rebuild)
            indexed += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{doc['FILE_NAME']}: {e}")

    return IndexResult(indexed=indexed, failed=len(errors), errors=errors)


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
            params=[doc_id, _truncate(file_name, MAX_NODE_TITLE_CHARS),
                    _truncate(result.get("document_summary", ""), MAX_NODE_SUMMARY_CHARS),
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
                params=[doc_id, root_node_id,
                        _truncate(section.get("title", ""), MAX_NODE_TITLE_CHARS),
                        _truncate(section.get("summary", ""), MAX_NODE_SUMMARY_CHARS),
                        f"{section.get('start', 0)}:{section.get('end', len(text))}"],
            ).collect()

        log_event(logger, "INDEX_BUILD", project.project_code,
                  doc_id=doc_id, sections=len(result.get("sections", [])))

    except Exception:
        logger.exception("EVENT=INDEX_BUILD_ERROR doc_id=%s file=%s", doc_id, file_name)
        raise
