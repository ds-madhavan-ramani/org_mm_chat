# ORG MM Chat

**ORG MM Chat** is an LLM Wiki chatbot for querying the OCMS Review Group
(ORG) meeting minutes across every lease year. It's built on
[project-llm-wiki](https://github.com/ds-madhavan-ramani/project-llm-wiki), a
reusable multi-project LLM Wiki template for Snowflake Cortex, and is the
first real project provisioned from it — `PROJECT_CODE = 'ORG_MM_CHAT'` in
the shared catalog.

## What it does

- **Source of truth**: `BIS_ORG_Meeting_Minutes.xlsx` workbooks maintained on
  SharePoint, under **BIS > Clause 6.6(d) OCMS Review Group Minutes**
  ([folder link](https://metrotrains.sharepoint.com/:f:/s/cabinet-mr4/IgB1xW6fxABYQqW7zhSCst56AR9xCdADTTa_g7N6grpMMto?e=gK4PdO)).
  One workbook can cover a single lease year or several sheets (e.g. one
  sheet per year) — the ingestion path handles either.
- **Ingestion**: pulled from SharePoint via the Microsoft Graph API (or
  uploaded directly), on demand from the app itself — no separate sync job
  to run first.
- **Retrieval**: not vector-chunked RAG. Each document is indexed into a
  navigable tree (document → meeting/section, with LLM-generated summaries),
  and questions are answered by having the model traverse that tree —
  answers cite the exact source file, the same way a person would find an
  answer by skimming a table of contents rather than searching keyword
  fragments.
- **Chat UI**: Streamlit-in-Snowflake, with cited sources shown under every
  answer and a cache layer so repeated questions don't re-run the full
  search.

## How it works

```
SharePoint (Clause 6.6(d) OCMS Review Group Minutes)
        │  Microsoft Graph API
        ▼
RAW_DOCUMENTS  (MEDSOCMS.DATA_ORG_MM_CHAT)
   .xlsx → parsed natively (stdlib zipfile/XML, no third-party package),
           NOT AI_PARSE_DOCUMENT — spreadsheets are structured data, not
           scanned pages
   .pdf/.docx/.txt → AI_PARSE_DOCUMENT (OCR)
        │  index_builder.py (ORG_MEETING_MINUTES segmentation profile)
        ▼
DOCUMENT_INDEX  — one section per meeting date / per sheet, each with an
                  LLM summary (attendees, agenda items, decisions, actions)
        │  query_engine.py — 3-stage tree search
        │  (doc summaries → section summaries → excerpt synthesis, via
        │   Snowflake Cortex AI_COMPLETE)
        ▼
Chat answer, with cited source file names
```

## Project configuration

| Setting | Value |
|---|---|
| Project code | `ORG_MM_CHAT` |
| Display name | ORG - Meeting Minutes - Chat |
| Data schema | `MEDSOCMS.DATA_ORG_MM_CHAT` |
| Streamlit app | `MEDSOCMS.APP_CATALOG.ORG_MM_CHAT_APP` |
| SharePoint site | `https://metrotrains.sharepoint.com/sites/cabinet-mr4` |
| SharePoint default folder | Clause 6.6(d) OCMS Review Group Minutes (link above) |
| Segmentation profile | `ORG_MEETING_MINUTES` (per-meeting sections, not generic prose sectioning) |
| Warehouse / runtime | `MTMWH02`, warehouse runtime (no compute pool) |

These are all set in `pipeline/00_provision_project.ipynb` Step 2 — you
don't need to re-enter them, they're already parameterized for this project.

## Prerequisites

1. Snowflake access to the `ADVANCEDANALYTICS` role, `MTMWH02` warehouse,
   and `MEDSOCMS` database.
2. A Microsoft Graph API app registration with `Sites.Selected` permission
   granted on the `cabinet-mr4` SharePoint site, with:
   - Tenant ID / Client ID available as `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID`
     (environment variables the app reads at runtime)
   - Client secret stored in the Snowflake secret
     `MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET`
3. The shared Graph API network rule + External Access Integration
   (`MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE`,
   `GRAPH_API_ACCESS_INTEGRATION`) — tenant-level, created once for every
   project in this catalog, not per-project.

Run `sql/test_graph_connectivity.sql` to confirm all three exist before
deploying. If any come back empty, the `CREATE SECRET` / `CREATE NETWORK
RULE` / `CREATE EXTERNAL ACCESS INTEGRATION` statements are commented at the
bottom of that file (the integration needs `ACCOUNTADMIN`).

## Deploying / running

Everything is `pipeline/00_provision_project.ipynb`, run top to bottom in
Snowflake Notebooks:

1. **Connect** — first code cell, always run.
2. **Catalog setup** — creates `MEDSOCMS.APP_CATALOG` and the
   `CREATE_PROJECT`/`TEARDOWN_PROJECT` procedures. Skip if it already exists.
3. **Create the project** — creates `MEDSOCMS.DATA_ORG_MM_CHAT` (schema,
   stage, tables) and registers `ORG_MM_CHAT` in the catalog with its
   segmentation profile. Already parameterized; just run it. If the project
   already exists it prints "already exists — skipping" and does nothing
   further — safe to re-run.
4. **Deploy the app** — stages `python/` onto the project's own stage
   preserving its folder structure, stages `streamlit/`'s contents
   **flattened to the stage root** (`streamlit/app.py` → `@stage/app.py`,
   `streamlit/pages/1_Chat.py` → `@stage/pages/1_Chat.py` — see the
   `MAIN_FILE` gotcha below for why), stages the single matching dependency
   manifest (`environment.yml`, since this project runs on warehouse
   runtime), and runs `CREATE OR REPLACE STREAMLIT ... MAIN_FILE = 'app.py'
   ... EXTERNAL_ACCESS_INTEGRATIONS = (GRAPH_API_ACCESS_INTEGRATION)`. Safe
   to re-run any time you change app code — fully idempotent.

   *Ignore the leftover debug cells further down in the notebook*
   (`MINIMAL_TEST_APP`, stray `ALTER`/`DESCRIBE STREAMLIT` statements) —
   those are exploratory residue from earlier deployment debugging, not part
   of the deploy path.

5. Open the app: Snowsight → **Streamlit** → `ORG_MM_CHAT_APP`.

## Adding / syncing meeting minutes

From the app, **Data Sources** page:

- **SharePoint Folder** tab — the folder URL is pre-filled with the Clause
  6.6(d) OCMS Review Group Minutes folder. Click **List files**, tick the
  `BIS_ORG_Meeting_Minutes.xlsx` file(s) you want, click **Ingest selected
  files**. Already-ingested files are skipped automatically (deduped on the
  SharePoint item ID).
- **Upload Files** tab — for any file not in that SharePoint folder (drop
  PDF/DOCX/TXT/XLSX directly).
- **Index** tab — new documents are indexed automatically the first time;
  use **Rebuild all** if the segmentation profile ever changes.

`.xlsx`/`.xlsm` files are parsed natively (every sheet, row-by-row) rather
than through OCR — see `python/ingestion/xlsx_parser.py`.

## Using the chat

- **Chat** — ask a question, get an answer with the source file(s) it was
  drawn from shown under **Sources**; a ⚡ marks answers served from cache.
- **Sync Status** — document/index counts and recent ingestion run history,
  useful for confirming a SharePoint sync actually picked up new files.

## Known account-level gotchas (Streamlit-in-Snowflake)

- **External Access Integrations must be attached explicitly, on every
  runtime** — not just container runtime. The deploy cell attaches
  `GRAPH_API_ACCESS_INTEGRATION` unconditionally for exactly this reason:
  without it, the SharePoint tab's outbound calls to
  `login.microsoftonline.com`/`graph.microsoft.com` are blocked even on the
  default warehouse runtime.
- **`ROOT_LOCATION` is retired on some accounts** — the deploy cell uses
  `FROM '@<stage>'`, not the legacy `ROOT_LOCATION`.
- **A nested `MAIN_FILE` (e.g. `MAIN_FILE = 'streamlit/app.py'`) reliably
  fails to load on this account** with `Python Interpreter Error: TypeError:
  bad argument type for built-in operation` at bootstrap — before a single
  line of the app's own code runs, and regardless of what that code is.
  Confirmed by isolating the variable directly: a trivial `st.write(...)`
  app with no imports, no `pages/` folder, deployed with `MAIN_FILE` in a
  subfolder, failed identically to the real app; the exact same content at
  the stage root worked. `__file__`/`os.path.dirname(__file__)` were
  separately confirmed to behave normally (`/tmp/appRoot/...`) and are not
  the cause. **Fix**: `MAIN_FILE` is `'app.py'`, not `'streamlit/app.py'` —
  the deploy cell flattens `streamlit/`'s contents to the stage root
  instead of preserving its folder name (`python/` keeps its own folder
  structure since it's imported via `sys.path`, not used as `MAIN_FILE`).
  If this ever needs re-diagnosing, isolate one variable at a time: a bare
  `st.write` app is fastest for testing whether an app-load failure is
  structural (stage layout / `MAIN_FILE`) vs. something in the app's own
  code.
- **The generic `TypeError: bad argument type for built-in operation` is
  not a diagnostic message** — it's whatever Snowflake's sandbox bootstrap
  throws on several unrelated failure modes (the nested-`MAIN_FILE` case
  above; also reproduced by an unresolvable `environment.yml` package —
  adding `openpyxl` for xlsx ingestion broke app load the same way, since
  it isn't resolvable from this account's Anaconda channel snapshot for
  warehouse-runtime Streamlit apps; fixed by rewriting
  `python/ingestion/xlsx_parser.py` to use only the Python standard library
  — `zipfile` + `xml.etree.ElementTree`, since an `.xlsx` is just a zip of
  XML parts). Don't guess from the message alone: import every app
  dependency module-by-module from a Notebook cell to rule out a bad
  package, and bisect with a minimal `st.write` app at the stage root vs.
  nested to rule out a structural/layout issue, before assuming it's
  something in the app's actual logic.
- **`RUNTIME_NAME` set explicitly on warehouse runtime is harmless but
  was not the fix for the above** — an earlier pass through this bug set
  `RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'` explicitly on every
  `CREATE STREAMLIT`, reasoning from a debug trail that had modified a
  throwaway `MINIMAL_TEST_APP`. That trail's app was also nested-vs-flat at
  different points, which likely explains why that fix looked plausible —
  the real cause was the stage layout, not `RUNTIME_NAME`. Left in place as
  an explicit default since it doesn't hurt, but don't expect it alone to
  fix a similar error in the future.
- **Stage exactly one dependency manifest** (`environment.yml` *or*
  `pyproject.toml`, matching the runtime) — staging both is ambiguous and
  can make Snowflake attempt PyPI resolution even on warehouse runtime.
- **Container runtime requires `CREATE COMPUTE POOL`/`CREATE EXTERNAL
  ACCESS INTEGRATION` privileges** typically held by `SYSADMIN`/
  `ACCOUNTADMIN`, not `ADVANCEDANALYTICS` by default. Not applicable to
  `ORG_MM_CHAT` today (it runs on warehouse runtime), but relevant if this
  project — or another one in the same catalog — ever moves to a compute
  pool.
- **A shared compute pool can silently block startup** if it's already at
  `max_nodes`. Check `SHOW COMPUTE POOLS` before assuming a container-runtime
  deploy will work.
- **`st.experimental_user` is gone** in current Streamlit — the New Project
  page uses `st.user` (with a fallback) to read the requesting user's email.
- **The warehouse-runtime Streamlit version is controlled by the platform,
  not by `environment.yml`/`pyproject.toml`** — this account's runtime
  predates `st.page_link()` (added in Streamlit 1.31), which raised
  `StreamlitAPIException: page_link() is not a valid Streamlit command`
  even though nothing in our own dependency pins looked wrong. `app.py`'s
  sidebar no longer calls `st.page_link` — Streamlit's automatic `pages/`
  navigation (the sidebar page list shown by default) already provides
  working links without it. If a future change wants an explicit page
  link again, confirm the platform's actual Streamlit version first rather
  than assuming what's pinned in the manifests applies.

## Removing the project

```sql
CALL TEARDOWN_PROJECT('ORG_MM_CHAT', FALSE);  -- keep logs
CALL TEARDOWN_PROJECT('ORG_MM_CHAT', TRUE);   -- purge logs too
```

Drops the `ORG_MM_CHAT_APP` Streamlit app, its deploy stage, and
`MEDSOCMS.DATA_ORG_MM_CHAT`, and removes the catalog row.

## Under the hood: the multi-project template

The code in this repo is the generic `project-llm-wiki` engine — nothing
here is hardcoded to meeting minutes except the `ORG_MEETING_MINUTES`
segmentation profile and the values in the provisioning notebook. The same
`MEDSOCMS.APP_CATALOG` catalog this project lives in could host additional,
unrelated projects (each gets its own isolated data schema, stage, and
Streamlit app — see `streamlit/pages/4_New_Project.py`), but this
deployment's day-to-day purpose is `ORG_MM_CHAT`.

```
project-llm-wiki/
├── sql/
│   ├── 00_setup_catalog.sql            # APP_CATALOG + CREATE_PROJECT / TEARDOWN_PROJECT (shared, one-time)
│   ├── 02_project_schema_template.sql  # reference copy of per-project data DDL
│   └── test_graph_connectivity.sql
├── python/
│   ├── config.py                       # infra constants + ProjectConfig loader
│   ├── snowflake_session.py            # get_active_session() → st.connection() → external, in order
│   ├── query_engine.py                 # tree search + synthesis
│   ├── ingestion/
│   │   ├── file_ingest.py              # upload → RAW_DOCUMENTS
│   │   ├── sharepoint_ingest.py        # SharePoint folder → RAW_DOCUMENTS
│   │   ├── xlsx_parser.py              # native xlsx/xlsm parsing (stdlib only, no openpyxl)
│   │   └── index_builder.py            # RAW_DOCUMENTS → DOCUMENT_INDEX (GENERIC / ORG_MEETING_MINUTES profiles)
│   ├── provisioning/
│   │   └── create_project.py           # CLI wrapper for CREATE_PROJECT
│   └── utils/
│       ├── cortex_client.py
│       ├── graph_client.py             # generalized Graph API (any folder URL)
│       ├── sql_utils.py
│       ├── sql_script.py
│       └── logging_utils.py
├── streamlit/
│   ├── app.py                          # project selector
│   ├── environment.yml                 # warehouse-runtime dependencies (conda)
│   ├── pyproject.toml                  # container-runtime dependencies (pip/uv)
│   ├── requirements.txt                # pip reference list (local dev / external session use)
│   └── pages/
│       ├── 1_Chat.py
│       ├── 2_Data_Sources.py
│       ├── 3_Sync_Status.py
│       └── 4_New_Project.py            # only needed to provision a project other than ORG_MM_CHAT
└── pipeline/
    └── 00_provision_project.ipynb      # connect, catalog setup, create ORG_MM_CHAT, deploy
```
