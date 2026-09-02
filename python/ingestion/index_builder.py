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

# Text embedding model for DOCUMENT_INDEX.NODE_EMBEDDING (vector/semantic
# search — see query_engine.py's hybrid retrieval). AI_EMBED is the
# forward-looking function (EMBED_TEXT_768/1024 are the legacy names,
# slated for deprecation); 'snowflake-arctic-embed-m' returns a 768-dim
# vector, matching DOCUMENT_INDEX.NODE_EMBEDDING's declared width.
EMBED_MODEL = "snowflake-arctic-embed-m"


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
names). For each section, produce a concise 2-3 sentence summary plus a
"start_text" anchor: the exact first 8-15 words of that section, COPIED
VERBATIM character-for-character from the document text below (same
spelling, punctuation, and capitalization) — do not paraphrase or
summarize it, it is used to locate the section programmatically by exact
text search.
{granularity_instruction}
Return ONLY valid JSON in this shape, no commentary:
{{
  "document_summary": "2-3 sentence summary of the whole document",
  "sections": [
    {{"title": "...", "summary": "...", "start_text": "exact opening words of this section, copied verbatim"}}
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

For each section, also give a "start_text" anchor: the exact first 8-15
words of that section, COPIED VERBATIM character-for-character from the
document text below (same spelling, punctuation, and capitalization) — do
not paraphrase or summarize it, it is used to locate the section
programmatically by exact text search.
{granularity_instruction}
Return ONLY valid JSON in this shape, no commentary:
{{
  "document_summary": "3-5 sentence summary of the whole document (date range covered, overall purpose, and any recurring topics, systems, or codes discussed across meetings)",
  "sections": [
    {{"title": "e.g. 'Meeting — 12 Mar 2024' or a sheet/topic name", "summary": "...", "start_text": "exact opening words of this section, copied verbatim"}}
  ]
}}

DOCUMENT TEXT:
{text}
""",
}

# Optional per-project knob (PROJECTS.SEGMENTATION_GRANULARITY): pushes the
# indexing prompt to split each natural break further into per-topic /
# per-agenda-item sections instead of one section per meeting/sheet. Both
# PROMPTS templates above take a {granularity_instruction} placeholder so
# any segmentation profile can run at either granularity.
SEGMENTATION_GRANULARITY_INSTRUCTIONS = {
    "STANDARD": "",
    "DETAILED": (
        "\nGo finer-grained than one section per meeting/sheet: within each "
        "meeting or sheet, further split into one section per distinct "
        "topic, agenda item, or action/decision discussed, so each section "
        "covers a single subject rather than an entire meeting.\n"
    ),
}


def _granularity_instruction(project: ProjectConfig) -> str:
    return SEGMENTATION_GRANULARITY_INSTRUCTIONS.get(
        project.segmentation_granularity, ""
    )


# The indexing response's JSON grows with how many sections a document
# gets split into — complete_json()'s 4096-token default (sized for
# shorter, more conversational chat responses) is routinely too small
# once a document has many meetings/sheets, and far too small under
# DETAILED granularity, which deliberately asks for more, finer-grained
# sections. Indexing is a batch job, not a latency-sensitive per-question
# call, so there's no real cost to budgeting generously here — complete_json()
# still retries with an even larger budget on its own if a response gets
# truncated anyway.
INDEXING_MAX_TOKENS = {
    "STANDARD": 8192,
    "DETAILED": 12000,
}


def _indexing_max_tokens(project: ProjectConfig) -> int:
    return INDEXING_MAX_TOKENS.get(project.segmentation_granularity, 8192)


def _locate_section_offsets(text: str, sections: List[dict]) -> List[tuple]:
    """
    Turns each section's "start_text" anchor into a concrete (start, end)
    character range, by locating the anchor with a literal string search
    rather than trusting a numeric offset from the model.

    This replaces an earlier design where the model was asked to return
    "start"/"end" character positions directly — verified against real
    indexed output to be unreliable: a section correctly titled/summarized
    could still carry offsets pointing at unrelated text elsewhere in the
    document (the model estimates positions in long text rather than
    counting them), and in one observed case the offsets for an entire
    14-section document only spanned the first ~3450 characters when the
    actual text was 5000+ characters long. LLMs are far more reliable at
    copying a short exact phrase than at reporting a character count, so
    locating that phrase deterministically in Python removes the guess
    entirely for wherever the anchor is found verbatim.

    Sections are treated as contiguous and sequential: each one's end is
    simply where the next one's anchor was found (or end-of-text for the
    last section) — no separate "end" anchor is needed. Anchors are
    searched for in document order, each starting from where the previous
    one was found, so an anchor phrase that happens to repeat earlier in
    the document doesn't get matched against the wrong occurrence.

    If an anchor is missing, blank, or can't be found verbatim (e.g. the
    model paraphrased instead of copying exactly), that section falls
    back to starting exactly where the previous one ended — the section
    still gets indexed with its own title/summary, just without a
    precisely-located excerpt boundary of its own.
    """
    cursor = 0
    starts = []
    for section in sections:
        anchor = (section.get("start_text") or "").strip()
        idx = text.find(anchor, cursor) if anchor else -1
        if idx == -1:
            idx = cursor
        starts.append(idx)
        cursor = idx + len(anchor) if anchor else idx + 1

    offsets = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        if end <= start:
            end = min(start + 1, len(text))
        offsets.append((start, end))
    return offsets


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

    # Shared across the whole run (not reset per-document): if AI_EMBED
    # fails once — e.g. embeddings aren't enabled on this account — there's
    # no reason to retry and fail identically on every remaining document.
    # A single mutable cell so _index_one_document can flip it off and have
    # that take effect for the rest of this call. Embeddings are an
    # additive retrieval signal (see query_engine.py), not a hard
    # requirement, so disabling them never fails the document itself.
    embed_enabled = [True]

    errors: List[str] = []
    indexed = 0
    for doc in docs:
        try:
            _index_one_document(session, project, doc["DOC_ID"], doc["FILE_NAME"],
                                 doc["RAW_TEXT"], prompt_template, rebuild, embed_enabled)
            indexed += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{doc['FILE_NAME']}: {e}")

    return IndexResult(indexed=indexed, failed=len(errors), errors=errors)


def _index_one_document(session, project: ProjectConfig, doc_id: int, file_name: str,
                         raw_text: str, prompt_template: str, rebuild: bool,
                         embed_enabled: List[bool]):
    schema = project.qualified_schema
    text = raw_text[: project.max_document_chars]

    if rebuild:
        session.sql(f"DELETE FROM {schema}.DOCUMENT_INDEX WHERE DOC_ID = ?",
                    params=[doc_id]).collect()

    try:
        result = complete_json(session, project.active_model,
                                prompt_template.format(
                                    text=text,
                                    granularity_instruction=_granularity_instruction(project),
                                ),
                                max_tokens=_indexing_max_tokens(project))

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

        sections = result.get("sections", [])
        offsets = _locate_section_offsets(text, sections)

        for section, (start_off, end_off) in zip(sections, offsets):
            title = _truncate(section.get("title", ""), MAX_NODE_TITLE_CHARS)
            summary = _truncate(section.get("summary", ""), MAX_NODE_SUMMARY_CHARS)
            text_ref = f"{start_off}:{end_off}"

            inserted = False
            if project.enable_vector_search and embed_enabled[0]:
                try:
                    session.sql(
                        f"""INSERT INTO {schema}.DOCUMENT_INDEX
                            (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY,
                             NODE_TEXT_REF, NODE_EMBEDDING)
                            SELECT ?, ?, 'section', ?, ?, ?, AI_EMBED(?, ?)""",
                        params=[doc_id, root_node_id, title, summary, text_ref,
                                EMBED_MODEL, f"{title}: {summary}"],
                    ).collect()
                    inserted = True
                except Exception:
                    logger.warning(
                        "EVENT=EMBED_UNAVAILABLE — disabling embeddings for the "
                        "rest of this run, indexing continues without them",
                        exc_info=True,
                    )
                    embed_enabled[0] = False

            if not inserted:
                session.sql(
                    f"""INSERT INTO {schema}.DOCUMENT_INDEX
                        (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF)
                        SELECT ?, ?, 'section', ?, ?, ?""",
                    params=[doc_id, root_node_id, title, summary, text_ref],
                ).collect()

        log_event(logger, "INDEX_BUILD", project.project_code,
                  doc_id=doc_id, sections=len(sections))

    except Exception:
        logger.exception("EVENT=INDEX_BUILD_ERROR doc_id=%s file=%s", doc_id, file_name)
        raise
