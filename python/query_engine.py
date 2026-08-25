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
from typing import List

from config import ProjectConfig, DATABASE, CATALOG_SCHEMA
from utils.cortex_client import complete, complete_json
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)


@dataclass
class AnswerResult:
    answer: str
    cited_docs: List[str] = field(default_factory=list)
    nodes_visited: List[int] = field(default_factory=list)
    from_cache: bool = False


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
answer. Return at most 5. If none look relevant, return an empty list.

QUESTION: {question}

DOCUMENTS:
{doc_summary_text}

Return ONLY JSON: {{"doc_ids": [1, 2]}}"""

    routing = complete_json(session, project.active_model, routing_prompt)
    candidate_doc_ids = routing.get("doc_ids", [])[:5]

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
contain the answer. Return at most 8.

QUESTION: {question}

SECTIONS:
{section_summary_text}

Return ONLY JSON: {{"node_ids": [1, 2]}}"""

    section_routing = complete_json(session, project.active_model, section_prompt)
    node_ids = section_routing.get("node_ids", [])[:8]

    if not node_ids:
        return AnswerResult(answer="I found relevant documents but no specific section answers that question.")

    # Stage 3: pull raw text for selected sections and synthesize
    placeholders = ", ".join(["?"] * len(node_ids))
    selected = session.sql(
        f"""SELECT DI.NODE_ID, DI.NODE_TITLE, DI.NODE_TEXT_REF, RD.FILE_NAME, RD.RAW_TEXT
            FROM {schema}.DOCUMENT_INDEX DI
            JOIN {schema}.RAW_DOCUMENTS RD ON DI.DOC_ID = RD.DOC_ID
            WHERE DI.NODE_ID IN ({placeholders})""",
        params=node_ids,
    ).collect()

    context_chunks = []
    cited_docs = set()
    for s in selected:
        start_off, end_off = (int(x) for x in s["NODE_TEXT_REF"].split(":"))
        excerpt = s["RAW_TEXT"][start_off:end_off][: project.max_section_chars]
        context_chunks.append(f"[{s['FILE_NAME']} — {s['NODE_TITLE']}]\n{excerpt}")
        cited_docs.add(s["FILE_NAME"])

    synthesis_prompt = f"""Answer the question using ONLY the excerpts below.
Cite the source file name for each claim. If the excerpts don't fully answer
the question, say so explicitly rather than guessing.

QUESTION: {question}

EXCERPTS:
{chr(10).join(context_chunks)}
"""
    answer_text = complete(session, project.active_model, synthesis_prompt)
    latency_ms = int((time.time() - start) * 1000)

    result = AnswerResult(
        answer=answer_text,
        cited_docs=sorted(cited_docs),
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
