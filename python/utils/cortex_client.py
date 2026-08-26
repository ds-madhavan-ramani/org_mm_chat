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


def complete_json(session, model: str, prompt: str, max_tokens: int = 4096) -> dict:
    """Call complete() and parse the response as JSON, with a clear error on failure."""
    raw = complete(session, model, prompt, max_tokens=max_tokens)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("EVENT=JSON_PARSE_ERROR response_preview=%r", raw[:500])
        raise CortexError(f"Cortex response was not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        # json.loads() happily parses a bare quoted string, number, or list
        # as valid JSON — e.g. the model responding with a plain refusal
        # message like "Unable to process this document" instead of the
        # requested {...} object. Treat that the same as a parse failure
        # rather than returning something callers' .get() calls will blow
        # up on downstream with a much less clear AttributeError.
        logger.error("EVENT=JSON_PARSE_ERROR response_preview=%r", raw[:500])
        raise CortexError(
            f"Cortex response was valid JSON but not a JSON object (got {type(parsed).__name__})"
        )
    return parsed
