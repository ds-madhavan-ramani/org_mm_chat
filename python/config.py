"""
config.py — infra constants + per-project config loader.

The old ocms-llm-wiki config.py held one project's settings as module-level
constants (TENANT_ID, ACTIVE_MODEL, MAX_DOCUMENT_CHARS, ...). In the template
those become a row in MEDSOCMS.APP_CATALOG.PROJECTS, fetched at runtime for
whichever project is active. Only true infra constants (shared across every
project) stay as constants here.
"""

import os
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Infra constants — these are truly account-wide, not per-project. Warehouse
# and compute pool are NOT here anymore — they're per-project columns on
# PROJECTS (QUERY_WAREHOUSE, COMPUTE_POOL), since different projects may
# want different compute. MTMWH02 below is only the *fallback default* a new
# project gets if none is specified at creation time.
# ---------------------------------------------------------------------------
WAREHOUSE_NAME = "MTMWH02"  # default only — see ProjectConfig.query_warehouse
DATABASE = "MEDSOCMS"
ROLE = "ADVANCEDANALYTICS"
CATALOG_SCHEMA = "APP_CATALOG"

# Graph API app registration — tenant-level, shared by every project's
# SharePoint ingestion. Client secret is fetched from the Snowflake SECRET
# object referenced in test_graph_connectivity.sql, never stored here.
GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")
GRAPH_SECRET_NAME = f"{DATABASE}.{CATALOG_SCHEMA}.GRAPH_API_SECRET"

# Model fallback if a project row doesn't specify one
FREE_MODEL = "llama3.1-70b"

MIN_PARSED_TEXT_CHARS = 100


@dataclass
class ProjectConfig:
    """One row of MEDSOCMS.APP_CATALOG.PROJECTS, typed."""
    project_id: int
    project_code: str
    project_name: str
    description: Optional[str]
    data_schema: str
    stage_name: str
    streamlit_app_name: str
    streamlit_stage_name: str
    query_warehouse: str
    compute_pool: Optional[str]
    sharepoint_site_url: Optional[str]
    sharepoint_default_folder: Optional[str]
    active_model: str
    max_document_chars: int
    max_section_chars: int
    query_cache_ttl_hours: int
    max_citations_display: int
    segmentation_profile: str
    status: str

    @property
    def qualified_schema(self) -> str:
        return f"{DATABASE}.{self.data_schema}"

    @property
    def qualified_stage(self) -> str:
        return f"{self.qualified_schema}.{self.stage_name}"

    @property
    def qualified_streamlit_app(self) -> str:
        return f"{DATABASE}.{CATALOG_SCHEMA}.{self.streamlit_app_name}"

    @property
    def qualified_streamlit_stage(self) -> str:
        return f"{DATABASE}.{CATALOG_SCHEMA}.{self.streamlit_stage_name}"

    @property
    def is_container_runtime(self) -> bool:
        return bool(self.compute_pool)


def load_project(session, project_code: str) -> ProjectConfig:
    """Fetch a project's config row. Raises ValueError if not found/archived."""
    rows = session.sql(
        f"""SELECT PROJECT_ID, PROJECT_CODE, PROJECT_NAME, DESCRIPTION, DATA_SCHEMA,
                   STAGE_NAME, STREAMLIT_APP_NAME, STREAMLIT_STAGE_NAME,
                   QUERY_WAREHOUSE, COMPUTE_POOL,
                   SHAREPOINT_SITE_URL, SHAREPOINT_DEFAULT_FOLDER,
                   ACTIVE_MODEL, MAX_DOCUMENT_CHARS, MAX_SECTION_CHARS,
                   QUERY_CACHE_TTL_HOURS, MAX_CITATIONS_DISPLAY,
                   SEGMENTATION_PROFILE, STATUS
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
            WHERE PROJECT_CODE = ?""",
        params=[project_code.strip().upper()],
    ).collect()

    if not rows:
        raise ValueError(f"No project found with code '{project_code}'")

    r = rows[0]
    return ProjectConfig(
        project_id=r["PROJECT_ID"],
        project_code=r["PROJECT_CODE"],
        project_name=r["PROJECT_NAME"],
        description=r["DESCRIPTION"],
        data_schema=r["DATA_SCHEMA"],
        stage_name=r["STAGE_NAME"],
        streamlit_app_name=r["STREAMLIT_APP_NAME"],
        streamlit_stage_name=r["STREAMLIT_STAGE_NAME"],
        query_warehouse=r["QUERY_WAREHOUSE"],
        compute_pool=r["COMPUTE_POOL"],
        sharepoint_site_url=r["SHAREPOINT_SITE_URL"],
        sharepoint_default_folder=r["SHAREPOINT_DEFAULT_FOLDER"],
        active_model=r["ACTIVE_MODEL"],
        max_document_chars=r["MAX_DOCUMENT_CHARS"],
        max_section_chars=r["MAX_SECTION_CHARS"],
        query_cache_ttl_hours=r["QUERY_CACHE_TTL_HOURS"],
        max_citations_display=r["MAX_CITATIONS_DISPLAY"],
        segmentation_profile=r["SEGMENTATION_PROFILE"],
        status=r["STATUS"],
    )


def list_active_projects(session):
    """Used by the Streamlit project selector."""
    return session.sql(
        f"""SELECT PROJECT_CODE, PROJECT_NAME
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
            WHERE STATUS = 'ACTIVE'
            ORDER BY PROJECT_NAME"""
    ).collect()