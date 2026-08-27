"""
query_engine.py — tree-search + answer synthesis, generalized to run against
any project's schema.

Behavioural difference from ocms-llm-wiki: since ingestion is no longer a
mandatory pre-step, search() must degrade gracefully when a project has zero
indexed documents (new project, or ingestion hasn't run yet) rather than
assume that never happens.
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List

from config import ProjectConfig, DATABASE, CATALOG_SCHEMA
from utils.cortex_client import complete, complete_json, CortexError
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

# How much evidence a single question can pull from. Higher = more complete
# answers for broad/thematic questions spanning many meetings, at the cost
# of a longer, slower, more expensive synthesis call per question. Tune
# here rather than as magic numbers buried in the routing prompts below.
MAX_CANDIDATE_DOCS = 10
MAX_CANDIDATE_SECTIONS = 20
MAX_KEYWORD_FALLBACK_DOCS = 10


@dataclass
class AnswerResult:
    answer: str
    # [{"number": 1, "file_name": ..., "url": ...}, ...] — url is None for
    # directly-uploaded documents (no SharePoint source to link to). Older
    # cached rows (from before citations were structured) may still hold
    # plain filename strings; render defensively for both shapes.
    cited_docs: List[Dict] = field(default_factory=list)
    nodes_visited: List[int] = field(default_factory=list)
    from_cache: bool = False


def _normalize_answer_text(text: str) -> str:
    """Some models emit literal backslash-n / backslash-t escape sequences
    in plain-text (non-JSON) responses instead of real whitespace — the
    same unreliable-formatting quirk complete_json() works around for
    structured responses, just showing up here as visible '\\n' text in
    the chat UI instead of a JSON parse error."""
    return (
        text.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
    )


def search(session, project: ProjectConfig, question: str, use_cache: bool = True) -> AnswerResult:
    _validate_question(question)
    schema = project.qualified_schema

    doc_count = session.sql(
        f"SELECT COUNT(*) AS C FROM {schema}.RAW_DOCUMENTS"
    ).collect()[0]["C"]
    if doc_count == 0:
        return AnswerResult(
            answer=("No documents have been added to this project yet. "
                    "Go to the Data Sources page to upload files or connect "
                    "a SharePoint folder, then come back and ask again."),
        )

    query_hash = hashlib.md5(question.strip().lower().encode()).hexdigest()
    if use_cache:
        cached = _check_cache(session, project, query_hash)
        if cached:
            return cached

    start = time.time()

    # Stage 1: pick relevant document(s) from top-level summaries
    doc_nodes = session.sql(
        f"""SELECT DI.NODE_ID, DI.DOC_ID, DI.NODE_TITLE, DI.NODE_SUMMARY
            FROM {schema}.DOCUMENT_INDEX DI
            WHERE DI.PARENT_NODE_ID IS NULL"""
    ).collect()

    if not doc_nodes:
        return AnswerResult(
            answer=("Documents have been added but haven't been indexed yet. "
                    "Trigger a rebuild from the Data Sources page."),
        )

    doc_summary_text = "\n".join(
        f"[doc_id={d['DOC_ID']}] {d['NODE_TITLE']}: {d['NODE_SUMMARY']}" for d in doc_nodes
    )
    routing_prompt = f"""Given this question and list of documents, return the
doc_id values (as a JSON list of integers) of documents likely to contain the
answer. Return at most {MAX_CANDIDATE_DOCS}. If none look relevant, return an
empty list.

QUESTION: {question}

DOCUMENTS:
{doc_summary_text}

Return ONLY JSON: {{"doc_ids": [1, 2]}}"""

    routing = complete_json(session, project.active_model, routing_prompt)
    candidate_doc_ids = routing.get("doc_ids", [])[:MAX_CANDIDATE_DOCS]

    if not candidate_doc_ids:
        # Document-level summaries are broad glosses of a whole meeting —
        # a specific code/ID/acronym (e.g. "A9605") will rarely appear in
        # one even when it's right there in the raw text. Fall back to a
        # literal keyword search instead of giving up outright.
        candidate_doc_ids = _keyword_fallback_doc_ids(session, project, question, schema)
        if not candidate_doc_ids:
            return AnswerResult(answer="I couldn't find a document relevant to that question.")

    # Stage 2: within selected documents, pick relevant section(s)
    placeholders = ", ".join(["?"] * len(candidate_doc_ids))
    section_nodes = session.sql(
        f"""SELECT NODE_ID, DOC_ID, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF
            FROM {schema}.DOCUMENT_INDEX
            WHERE DOC_ID IN ({placeholders}) AND NODE_LEVEL = 'section'""",
        params=candidate_doc_ids,
    ).collect()

    section_summary_text = "\n".join(
        f"[node_id={s['NODE_ID']}] {s['NODE_TITLE']}: {s['NODE_SUMMARY']}" for s in section_nodes
    )
    section_prompt = f"""Given this question and list of document sections,
return the node_id values (JSON list of integers) of sections likely to
contain the answer. Return at most {MAX_CANDIDATE_SECTIONS}.

QUESTION: {question}

SECTIONS:
{section_summary_text}

Return ONLY JSON: {{"node_ids": [1, 2]}}"""

    section_routing = complete_json(session, project.active_model, section_prompt)
    node_ids = section_routing.get("node_ids", [])[:MAX_CANDIDATE_SECTIONS]

    if not node_ids:
        # Section routing found nothing specific within otherwise-relevant
        # documents — rather than give up, fall back to every section in
        # those documents (still bounded by the section cap) instead of
        # depending on the routing model having correctly picked among
        # them; an over-conservative section pick shouldn't produce an
        # empty answer when the document itself was judged relevant.
        node_ids = [s["NODE_ID"] for s in section_nodes][:MAX_CANDIDATE_SECTIONS]
        if not node_ids:
            return AnswerResult(answer="I found relevant documents but no specific section answers that question.")

    # Stage 3: pull raw text for selected sections and synthesize
    placeholders = ", ".join(["?"] * len(node_ids))
    selected = session.sql(
        f"""SELECT DI.NODE_ID, DI.NODE_TITLE, DI.NODE_TEXT_REF,
                   RD.FILE_NAME, RD.RAW_TEXT, RD.SOURCE_URL
            FROM {schema}.DOCUMENT_INDEX DI
            JOIN {schema}.RAW_DOCUMENTS RD ON DI.DOC_ID = RD.DOC_ID
            WHERE DI.NODE_ID IN ({placeholders})""",
        params=node_ids,
    ).collect()

    # Number sources deterministically in code (not left to the model) —
    # this is what actually appears in the Sources list and any [n]
    # markers the model uses are just following along with it, so the
    # citation list is always correct even if the model's inline markers
    # aren't.
    doc_urls = {}
    for s in selected:
        doc_urls.setdefault(s["FILE_NAME"], s["SOURCE_URL"])
    file_names_sorted = sorted(doc_urls)
    doc_numbers = {name: i + 1 for i, name in enumerate(file_names_sorted)}

    context_chunks = []
    for s in selected:
        start_off, end_off = (int(x) for x in s["NODE_TEXT_REF"].split(":"))
        excerpt = s["RAW_TEXT"][start_off:end_off][: project.max_section_chars]
        n = doc_numbers[s["FILE_NAME"]]
        context_chunks.append(f"[{n}] {s['FILE_NAME']} — {s['NODE_TITLE']}\n{excerpt}")

    synthesis_prompt = f"""Answer the question using ONLY the excerpts below.
Format the answer as a bulleted list, one point per bullet, with a blank
line between bullets — use real line breaks, never the literal characters
backslash-n. Cite sources inline using the bracketed number shown before
each excerpt, e.g. [1]. If the excerpts don't fully answer the question,
say so explicitly rather than guessing.

QUESTION: {question}

EXCERPTS:
{chr(10).join(context_chunks)}
"""
    answer_text = complete(session, project.active_model, synthesis_prompt)
    answer_text = _normalize_answer_text(answer_text)
    latency_ms = int((time.time() - start) * 1000)

    citations = [
        {"number": doc_numbers[name], "file_name": name, "url": doc_urls[name]}
        for name in file_names_sorted
    ]

    result = AnswerResult(
        answer=answer_text,
        cited_docs=citations,
        nodes_visited=node_ids,
    )

    _log_query(session, project, question, query_hash, result, latency_ms)
    log_event(logger, "QUERY", project.project_code,
              nodes=len(node_ids), latency_ms=latency_ms)

    return result


def _validate_question(question: str):
    if not question or not question.strip():
        raise ValueError("Question cannot be empty")
    if len(question) > 2000:
        raise ValueError("Question too long (max 2000 chars)")


def _keyword_fallback_doc_ids(session, project: ProjectConfig, question: str, schema: str) -> List[int]:
    """
    Extracts literal search terms (codes, IDs, acronyms, quoted phrases)
    from the question and searches RAW_TEXT for them directly — a
    complement to summary-based routing, not a replacement: a document
    summary is a compression that can't enumerate every code/ID it
    contains, so a question asking for one by name needs an exact-text
    search, not a "does this summary sound relevant" judgment. Returns []
    on any failure (extraction or search) so the caller's existing
    "couldn't find a relevant document" message still applies.
    """
    extract_prompt = f"""Extract up to 3 short literal search terms from this
question — specific codes, IDs, acronyms, or quoted phrases someone would
search for verbatim in a document. Skip generic/common words. If there are
no such specific terms, return an empty list.

QUESTION: {question}

Return ONLY JSON: {{"terms": ["term1", "term2"]}}"""
    try:
        extracted = complete_json(session, project.active_model, extract_prompt)
    except CortexError:
        logger.warning("EVENT=KEYWORD_FALLBACK_EXTRACT_FAILED question=%r", question)
        return []

    terms = [t.strip() for t in extracted.get("terms", []) if t and t.strip()]
    if not terms:
        return []

    conditions = " OR ".join(["RAW_TEXT ILIKE ?"] * len(terms))
    params = [f"%{t}%" for t in terms]
    rows = session.sql(
        f"""SELECT DISTINCT DOC_ID FROM {schema}.RAW_DOCUMENTS
            WHERE {conditions} LIMIT {MAX_KEYWORD_FALLBACK_DOCS}""",
        params=params,
    ).collect()
    doc_ids = [r["DOC_ID"] for r in rows]
    if doc_ids:
        log_event(logger, "KEYWORD_FALLBACK_HIT", project.project_code,
                  terms=terms, doc_count=len(doc_ids))
    return doc_ids


def _check_cache(session, project: ProjectConfig, query_hash: str) -> AnswerResult | None:
    rows = session.sql(
        f"""SELECT FINAL_ANSWER, CITED_DOCS, NODES_VISITED
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECT_QUERY_LOG
            WHERE PROJECT_ID = (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                                 WHERE PROJECT_CODE = ?)
              AND QUERY_HASH = ?
              AND CREATED_AT > DATEADD(HOUR, -?, CURRENT_TIMESTAMP())
            ORDER BY CREATED_AT DESC LIMIT 1""",
        params=[project.project_code, query_hash, project.query_cache_ttl_hours],
    ).collect()
    if not rows:
        return None
    r = rows[0]
    import json
    return AnswerResult(
        answer=r["FINAL_ANSWER"],
        cited_docs=json.loads(r["CITED_DOCS"]) if r["CITED_DOCS"] else [],
        nodes_visited=json.loads(r["NODES_VISITED"]) if r["NODES_VISITED"] else [],
        from_cache=True,
    )


def _log_query(session, project: ProjectConfig, question: str, query_hash: str,
              result: AnswerResult, latency_ms: int):
    import json
    session.sql(
        f"""INSERT INTO {DATABASE}.{CATALOG_SCHEMA}.PROJECT_QUERY_LOG
            (PROJECT_ID, USER_QUESTION, QUERY_HASH, NODES_VISITED, FINAL_ANSWER,
             CITED_DOCS, LATENCY_MS)
            SELECT (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                    WHERE PROJECT_CODE = ?), ?, ?, PARSE_JSON(?), ?, PARSE_JSON(?), ?""",
        params=[project.project_code, question, query_hash,
                json.dumps(result.nodes_visited), result.answer,
                json.dumps(result.cited_docs), latency_ms],
    ).collect()
