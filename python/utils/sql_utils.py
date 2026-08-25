"""
sql_utils.py — parameterized query helpers.

Carried over from ocms-llm-wiki's security hardening: never build SQL by
string-concatenating user input. Schema/table names below come from
config.ProjectConfig (developer-controlled, never user input); the values
that vary by request always go through bind params.
"""

from typing import Any, Iterable, List


class SQLBuilder:
    @staticmethod
    def build_merge_raw_document(qualified_schema: str) -> str:
        """MERGE for idempotent document upsert, keyed on SOURCE_HASH."""
        return f"""
            MERGE INTO {qualified_schema}.RAW_DOCUMENTS AS tgt
            USING (SELECT ? AS FILE_NAME, ? AS STAGE_PATH, ? AS SOURCE_TYPE,
                          ? AS SHAREPOINT_ITEM_ID, ? AS DOCUMENT_DATE,
                          ? AS RAW_TEXT, ? AS SOURCE_HASH) AS src
            ON tgt.SOURCE_HASH = src.SOURCE_HASH
            WHEN NOT MATCHED THEN INSERT
                (FILE_NAME, STAGE_PATH, SOURCE_TYPE, SHAREPOINT_ITEM_ID,
                 DOCUMENT_DATE, RAW_TEXT, SOURCE_HASH, PARSED_AT)
                VALUES (src.FILE_NAME, src.STAGE_PATH, src.SOURCE_TYPE,
                        src.SHAREPOINT_ITEM_ID, src.DOCUMENT_DATE, src.RAW_TEXT,
                        src.SOURCE_HASH, CURRENT_TIMESTAMP())
        """

    @staticmethod
    def build_insert_index_node(qualified_schema: str) -> str:
        return f"""
            INSERT INTO {qualified_schema}.DOCUMENT_INDEX
                (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF)
            SELECT ?, ?, ?, ?, ?, ?
        """

    @staticmethod
    def in_clause(values: Iterable[Any]) -> tuple[str, List[Any]]:
        """Returns ('(?, ?, ?)', [v1, v2, v3]) for a safe dynamic IN clause."""
        values = list(values)
        placeholders = ", ".join(["?"] * len(values))
        return f"({placeholders})", values
