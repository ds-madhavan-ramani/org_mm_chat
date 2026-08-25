# project-llm-wiki

A reusable template for spinning up a vectorless, hierarchical-retrieval
LLM Wiki on Snowflake Cortex. Each project gets its own isolated data
schema, its own Streamlit app, and its own choice of compute — adaptable to
any project's document set in minutes, without forking code.

This repo is a standalone master: every deployment is created from it the
same way, by registering a new project in the shared catalog and deploying
that project's own app. There is no example project baked into the
template — the catalog starts empty until you create your first one.

## What is an LLM Wiki, and why not just RAG?

An LLM Wiki is a retrieval approach built for document sets that already
have real structure — meeting minutes, contracts, policy manuals — where a
human could navigate a table of contents to find the right section. Instead
of chunking every document and searching by embedding similarity (the
standard vector-RAG approach), an LLM Wiki builds a hierarchical index —
document → section → sub-section, each with an LLM-generated summary — and
has the model *traverse* that tree to find the answer, the same way a person
would skim a table of contents rather than search for keyword matches across
shredded fragments. This trades some of RAG's flexibility on large,
unstructured, fast-growing corpora for two things structured collections
value more: answers that cite an exact section and date rather than a fuzzy
chunk, and no risk of a chunk boundary splitting a fact in half. RAG remains
the better fit for large or loosely structured evidence bases; this template
is for the other case.

## Configure this template for your environment

Before creating any projects, decide and fix these once for your
deployment:

| Setting | Where it's set | Notes |
|---|---|---|
| Database | `sql/00_setup_catalog.sql`, `python/config.py` (`DATABASE`) | Not a shared constant across organizations or template instances — pick the Snowflake database this template's catalog and every project's data will live in, and use that same value consistently across the SQL/Python files. The rest of this README uses `<DATABASE>` as a placeholder for whatever you choose. |
| Role | `sql/00_setup_catalog.sql`, `python/config.py` (`ROLE`) | The role that owns the catalog and every project schema. |
| Default query warehouse | `python/config.py` (`WAREHOUSE_NAME`) | Only a fallback — see below, it's overridable per project. |

## Infra shared by every project within one deployment of this template

Once you've fixed `<DATABASE>` and `ROLE` above, these live inside that
single database and are genuinely shared across every project you create
from this template — not shared across unrelated deployments or other
organizations' use of this same template.

Graph API app registration (`GRAPH_API_SECRET`, `GRAPH_API_NETWORK_RULE`,
`GRAPH_API_ACCESS_INTEGRATION`) and, if any project uses container runtime,
the PyPI External Access Integration (`PYPI_NETWORK_RULE`,
`PYPI_ACCESS_INTEGRATION`) are also shared within a deployment — created
once, not per project (see `sql/test_graph_connectivity.sql` and notebook
Step 1b).

## What's shared vs. what's unique per project

| Resource | Scope | Where it's set |
|---|---|---|
| `<DATABASE>.APP_CATALOG` schema | Shared, one-time per deployment | `sql/00_setup_catalog.sql` |
| `PROJECTS` / `PROJECT_SYNC_LOG` / `PROJECT_QUERY_LOG` tables | Shared, one-time per deployment | `sql/00_setup_catalog.sql` |
| Graph API integration (SharePoint) | Shared, one-time per deployment | created outside this repo; see `sql/test_graph_connectivity.sql` |
| PyPI integration (container runtime only) | Shared, one-time per deployment | notebook Step 1b |
| Data schema `<DATABASE>.DATA_<CODE>` | **Unique per project** | auto-created by `CREATE_PROJECT` |
| Data stage `DOCS_STAGE` (inside that schema) | **Unique per project** | auto-created by `CREATE_PROJECT` |
| `RAW_DOCUMENTS` / `DOCUMENT_INDEX` tables | **Unique per project** (live in that project's own schema) | auto-created by `CREATE_PROJECT` |
| Streamlit deploy stage `<CODE>_APP_STAGE` | **Unique per project** | auto-created by `CREATE_PROJECT` |
| Streamlit app object `<CODE>_APP` | **Unique per project** | deployed by notebook Step 3 |
| Query warehouse | **Configurable per project** | `PROJECTS.QUERY_WAREHOUSE` |
| Compute pool (or none, for warehouse runtime) | **Configurable per project** | `PROJECTS.COMPUTE_POOL` |
| Model, chunk sizes, cache TTL, segmentation profile | **Configurable per project** | `PROJECTS` row |

Nothing about a new project touches another project's schema, stage, app,
warehouse, or compute pool unless you deliberately point two projects at the
same one.

## Architecture

```
                          <DATABASE>.APP_CATALOG
        (PROJECTS, PROJECT_SYNC_LOG, PROJECT_QUERY_LOG — shared, one-time)
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
  Project: PROJECT_A         Project: PROJECT_B          Project: <NEW>
  ─────────────────────      ─────────────────────       ─────────────────
  DATA_PROJECT_A schema      DATA_PROJECT_B schema        DATA_<NEW> schema
    RAW_DOCUMENTS              RAW_DOCUMENTS                 RAW_DOCUMENTS
    DOCUMENT_INDEX             DOCUMENT_INDEX                DOCUMENT_INDEX
  PROJECT_A_APP_STAGE        PROJECT_B_APP_STAGE          <NEW>_APP_STAGE
  PROJECT_A_APP              PROJECT_B_APP                <NEW>_APP
  (its own warehouse/pool)   (its own warehouse/pool)     (its own warehouse/pool)
```

Each `<CODE>_APP` is a full copy of this template's `streamlit/` + `python/`
code, deployed independently — same app logic everywhere, isolated data and
compute per project.

## Setup (one-time, before the first project)

1. Run `sql/00_setup_catalog.sql` — creates `<DATABASE>.APP_CATALOG` and the
   `CREATE_PROJECT` / `TEARDOWN_PROJECT` procedures. (Notebook Step 1 does
   this for you via `run_sql_file`.)
2. Confirm Graph API connectivity: `sql/test_graph_connectivity.sql`.
3. **Only if any project will use container runtime** (a compute pool
   rather than a warehouse): run notebook Step 1b to create the shared PyPI
   External Access Integration. Skip entirely if every project stays on
   warehouse runtime.

## Deploying a new project

Everything below is `notebooks/00_provision_project.ipynb`, in order.

**Step 2 — create the project.** Set these values and run:

```python
PROJECT_CODE = 'CONTRACT_ANALYSIS'
PROJECT_NAME = 'Contract Analysis LLM Wiki'
DESCRIPTION = ''
SHAREPOINT_SITE_URL = ''
SHAREPOINT_DEFAULT_FOLDER = ''
CREATED_BY = ''
QUERY_WAREHOUSE = 'MTMWH02'   # or any other warehouse this project should run on
COMPUTE_POOL = ''             # blank = warehouse runtime; set a pool name for container runtime
```

This creates `<DATABASE>.DATA_CONTRACT_ANALYSIS` (schema, stage, tables), a
dedicated deploy stage `<DATABASE>.APP_CATALOG.CONTRACT_ANALYSIS_APP_STAGE`,
and registers the project row — including its own warehouse/compute pool
choice. It does **not** touch any other project.

If you set `COMPUTE_POOL`, check it has spare capacity first:
```sql
SHOW COMPUTE POOLS;
```
Look at `active_nodes` vs `max_nodes` for the pool you're pointing at. A
pool at capacity will fail to start your app with "the pool is unable to
start your app" — this isn't a code issue, it's the pool being full. Prefer
a dedicated pool per project over sharing one already used by another
production app, to avoid contention with other teams' apps.

**Step 3 — deploy the app.** Run as-is; it reads `QUERY_WAREHOUSE` and
`COMPUTE_POOL` from the project row you just created, so this cell never
needs manual edits per project:

```python
PROJECT_CODE = 'CONTRACT_ANALYSIS'  # the project to (re)deploy
# ... cell reads the rest from the PROJECTS catalog row automatically
```

This stages `streamlit/` + `python/` onto the project's own deploy stage,
stages both `environment.yml` and `pyproject.toml` at the stage root, and
runs `CREATE OR REPLACE STREAMLIT ... FROM '@<project's stage>'`. If
`COMPUTE_POOL` was set in Step 2, `RUNTIME_NAME`, `COMPUTE_POOL`, and
`EXTERNAL_ACCESS_INTEGRATIONS = (PYPI_ACCESS_INTEGRATION)` are added
automatically — nothing to configure by hand.

Re-run Step 3 (only) any time you change app code. It's fully idempotent —
safe to re-run for any project without affecting others.

## Adding documents (no separate pipeline run required)

Open the deployed app for a project → **Data Sources**:

- **Upload Files** tab — drop PDFs/DOCX/TXT directly; ingested immediately.
- **SharePoint Folder** tab — paste any folder URL, click **List files**,
  tick the ones you want, click **Ingest selected files**.
- **Index** tab — new documents are indexed automatically; use
  **Rebuild all** after changing a project's segmentation profile.

## File Structure

```
project-llm-wiki/
├── sql/
│   ├── 00_setup_catalog.sql            # APP_CATALOG + CREATE_PROJECT / TEARDOWN_PROJECT
│   ├── 02_project_schema_template.sql  # reference copy of per-project data DDL
│   └── test_graph_connectivity.sql
├── python/
│   ├── config.py                       # infra constants + ProjectConfig loader
│   ├── snowflake_session.py            # get_active_session() → st.connection() → external, in order
│   ├── query_engine.py                 # generalized tree search + synthesis
│   ├── ingestion/
│   │   ├── file_ingest.py              # upload → RAW_DOCUMENTS
│   │   ├── sharepoint_ingest.py        # SharePoint folder → RAW_DOCUMENTS
│   │   └── index_builder.py            # RAW_DOCUMENTS → DOCUMENT_INDEX
│   ├── provisioning/
│   │   └── create_project.py           # CLI wrapper for CREATE_PROJECT
│   └── utils/
│       ├── cortex_client.py
│       ├── graph_client.py             # generalized Graph API (any folder URL)
│       ├── sql_utils.py                # parameterized SQLBuilder for ingestion writes
│       ├── sql_script.py               # dollar-quote-aware .sql file splitter/runner
│       └── logging_utils.py
├── streamlit/
│   ├── app.py                          # project selector
│   ├── environment.yml                 # warehouse-runtime dependencies (conda)
│   ├── pyproject.toml                  # container-runtime dependencies (pip/uv)
│   └── pages/
│       ├── 1_Chat.py
│       ├── 2_Data_Sources.py
│       ├── 3_Sync_Status.py
│       └── 4_New_Project.py            # creates the data schema + catalog row only —
│                                        #   deploying that new project's app is still
│                                        #   a notebook Step 3 run, since it needs local
│                                        #   file access this repo checkout has and a
│                                        #   running app doesn't.
├── notebooks/
│   └── 00_provision_project.ipynb      # Step 1 (catalog), 1b (PyPI EAI), 2 (create), 3 (deploy)
├── requirements.txt
└── README.md
```

## Removing a project

```sql
CALL TEARDOWN_PROJECT('CONTRACT_ANALYSIS', FALSE);  -- keep logs
CALL TEARDOWN_PROJECT('CONTRACT_ANALYSIS', TRUE);   -- purge logs too
```

Drops that project's Streamlit app, its deploy stage, and its data schema
(`<DATABASE>.DATA_CONTRACT_ANALYSIS`), and removes the catalog row. No other
project is affected.

## Known account-level gotchas worth checking before deploying a new project

- **`ROOT_LOCATION` is retired on some accounts** — Step 3 already uses
  `FROM`, not the legacy `ROOT_LOCATION`. If you ever hand-write a
  `CREATE STREAMLIT` outside this notebook, use `FROM '@<stage>'` too.
- **Container runtime requires `CREATE COMPUTE POOL` / `CREATE EXTERNAL
  ACCESS INTEGRATION` privileges**, which `ADVANCEDANALYTICS` may not have
  by default (these are typically `SYSADMIN`/`ACCOUNTADMIN`-owned). If Step
  1b or a new `CREATE COMPUTE POOL` fails on permissions, that's a one-time
  ask to whoever holds that role — not a code problem.
- **A shared compute pool can silently block a new project's app from
  starting** if it's already at `max_nodes` from other apps/projects. Check
  `SHOW COMPUTE POOLS` before assuming a container-runtime deploy will work,
  and prefer a dedicated pool per project unless you've confirmed headroom.
