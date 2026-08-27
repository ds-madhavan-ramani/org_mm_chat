"""
cortex_client.py — thin wrapper around SNOWFLAKE.CORTEX.COMPLETE / AI_COMPLETE.

Difference from ocms-llm-wiki: model is passed in per call (from
ProjectConfig.active_model) rather than read from a module-level
ACTIVE_MODEL constant, so two projects running in the same process can use
different models.
"""

import json
import time
from typing import Optional

from utils.logging_utils import get_logger

logger = get_logger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2


class CortexError(Exception):
    pass


def complete(session, model: str, prompt: str, max_tokens: int = 4096,
             temperature: float = 0.0) -> str:
    """Call Cortex AI_COMPLETE with retry + basic validation. Returns text."""
    if not prompt or not prompt.strip():
        raise CortexError("Empty prompt passed to complete()")

    options = json.dumps({"max_tokens": max_tokens, "temperature": temperature})

    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        start = time.time()
        try:
            result = session.sql(
                "SELECT AI_COMPLETE(?, ?, PARSE_JSON(?)) AS RESPONSE",
                params=[model, prompt, options],
            ).collect()
            latency_ms = int((time.time() - start) * 1000)
            text = result[0]["RESPONSE"]
            logger.info("EVENT=CORTEX_CALL model=%s latency_ms=%d attempt=%d",
                        model, latency_ms, attempt)
            return text
        except Exception as e:  # noqa: BLE001 — deliberately broad, retried below
            last_err = e
            logger.warning("EVENT=CORTEX_RETRY model=%s attempt=%d error=%s",
                           model, attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY_SECONDS ** attempt)

    raise CortexError(f"Cortex call failed after {MAX_RETRIES} attempts: {last_err}")


def _strip_markdown_fence(text: str) -> str:
    """Strips a ```json ... ``` (or bare ```...```) code fence wrapping the
    text, if present. A no-op if there's no fence."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def complete_json(session, model: str, prompt: str, max_tokens: int = 4096) -> dict:
    """Call complete() and parse the response as JSON, with a clear error on failure."""
    raw = complete(session, model, prompt, max_tokens=max_tokens)

    # Some models, asked to "return ONLY valid JSON", escape their entire
    # fenced answer as a JSON *string* instead of emitting the object
    # directly — the raw text is '"```json\n{...}\n```"' (a JSON string
    # whose content is itself the fenced JSON block), not '{...}' or even
    # '```json\n{...}\n```' directly. A single fence-strip-then-parse pass
    # can't handle this: the fence markers only become visible *after*
    # unwrapping the outer string, so each unwrap needs its own fence-strip
    # before the next parse attempt, not just the first one.
    candidate = raw
    last_error: Optional[json.JSONDecodeError] = None
    parsed = None
    for _ in range(3):  # bounded: a couple of unwrap levels is plenty
        cleaned = _strip_markdown_fence(candidate)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            last_error = e
            parsed = None
            break
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            candidate = parsed  # one level of JSON-string-encoding unwrapped; retry
            continue
        break  # some other non-dict, non-str JSON value (e.g. a list) — give up

    # Either the final json.loads() failed outright, or it kept resolving
    # to something that's never a dict (e.g. a genuine refusal message).
    # Include a raw preview in the raised message itself (not just the
    # server-side log) so it's visible directly in the Streamlit error
    # card, not just Snowsight's server-side logs.
    logger.error("EVENT=JSON_PARSE_ERROR response_preview=%r", raw[:500])
    if last_error is not None:
        raise CortexError(
            f"Cortex response was not valid JSON: {last_error} "
            f"— raw response preview: {raw[:300]!r}"
        ) from last_error
    raise CortexError(
        f"Cortex response was valid JSON but not a JSON object (got "
        f"{type(parsed).__name__}) — raw response preview: {raw[:300]!r}"
    )
