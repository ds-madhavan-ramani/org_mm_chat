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
   **flattened to the stage root** (`streamlit/Chat.py` → `@stage/Chat.py`,
   `streamlit/pages/1_Data_Sources.py` → `@stage/pages/1_Data_Sources.py` —
   see the `MAIN_FILE` gotcha below for why), stages the single matching
   dependency manifest (`environment.yml`, since this project runs on
   warehouse runtime), and runs `CREATE OR REPLACE STREAMLIT ... MAIN_FILE =
   'Chat.py' ... EXTERNAL_ACCESS_INTEGRATIONS = (GRAPH_API_ACCESS_INTEGRATION)`.
   Safe to re-run any time you change app code — fully idempotent.

   *Ignore the leftover debug cells further down in the notebook*
   (`MINIMAL_TEST_APP`, stray `ALTER`/`DESCRIBE STREAMLIT` statements) —
   those are exploratory residue from earlier deployment debugging, not part
   of the deploy path.

5. Open the app: Snowsight → **Streamlit** → `ORG_MM_CHAT_APP`.

## Adding / syncing meeting minutes

From the app, **Data Sources** page:

- **SharePoint Folder** tab — shows a friendly label for this project's
  configured folder (Clause 6.6(d) OCMS Review Group Minutes) rather than
  its raw URL; use the collapsed **"Use a different SharePoint folder
  instead"** expander only if you need to point at somewhere else for one
  run. Click **List files**, tick the `BIS_ORG_Meeting_Minutes.xlsx`
  file(s) you want, click **Ingest selected files**.
  - A file whose content is unchanged since it was last ingested is
    skipped.
  - A file that's been **edited** in SharePoint since it was last ingested
    is detected (matched on its stable SharePoint item ID, not by content)
    and its existing row is **updated in place** — not left as a stale
    duplicate alongside a new one.
  - Newly ingested or updated documents are **indexed automatically**,
    right after ingest, in the same click — no separate manual step.
- **Upload Files** tab — for any file not in that SharePoint folder (drop
  PDF/DOCX/TXT/XLSX directly); also auto-indexes after ingest.
- **Index** tab — a fallback for manual use only (e.g. **Rebuild all**
  after changing the segmentation profile); not part of the normal flow
  since ingest already indexes automatically.

`.xlsx`/`.xlsm` files are parsed natively (every sheet, row-by-row) rather
than through OCR — see `python/ingestion/xlsx_parser.py`.

## Using the chat

- **Chat** — this deployment's default/landing page (no separate "pick a
  project" screen — there's only one project here). Ask a question, get an
  answer with the source file(s) it was drawn from shown under **Sources**;
  a ⚡ marks answers served from cache.
- **Sync Status** — document/index counts and recent ingestion run history,
  useful for confirming a SharePoint sync actually picked up new/changed
  files.

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
  the cause. **Fix**: `MAIN_FILE` is `'Chat.py'`, not `'streamlit/app.py'`
  — the deploy cell flattens `streamlit/`'s contents to the stage root
  instead of preserving its folder name (`python/` keeps its own folder
  structure since it's imported via `sys.path`, not used as `MAIN_FILE`).
  The entry script is named `Chat.py` rather than `app.py` specifically so
  its sidebar nav label reads "Chat" (Streamlit derives a page's label
  from its filename) — Chat is this deployment's default/landing page, not
  a separate project-picker screen. If this ever needs re-diagnosing,
  isolate one variable at a time: a bare
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
- **Warehouse runtime runs Streamlit 1.22.0 on this account, and pinning
  `environment.yml`'s `streamlit=1.32.3` does not change that** — confirmed
  by redeploying after adding the pin and seeing the exact same errors
  (`st.file_uploader` still names "1.22.0" in its own error text). Nothing
  about `pyproject.toml`/`requirements.txt` affects warehouse runtime
  either — those only apply under container runtime. This looks like a
  genuine platform limitation on this account/region rather than something
  fixable from the deploy cell: `environment.yml` can pin ordinary
  importable packages (see the `openpyxl` case above), but not, apparently,
  the Streamlit engine version itself. Investigate with Snowflake
  support/account admin if this needs a real fix (e.g. an account-level
  runtime version bump); the pin is left in `environment.yml` in case a
  future account/region behaves differently.

  Two concrete breakages and how they're handled given the version can't
  be changed here:
  - `st.page_link()` (added 1.31) — removed; Streamlit's automatic `pages/`
    sidebar navigation already covers the same thing without it.
  - `st.chat_input()`/`st.chat_message()` (added 1.24, both missing on
    1.22) — `Chat.py` checks `hasattr(st, "chat_input")` and falls back to
    a plain `st.form` + `st.text_input` + a manually-labeled `st.container`
    when they're unavailable, so Chat works either way.
  - `st.file_uploader()` — Snowflake's own error names 1.26.0 as the
    minimum, and there's no older-API fallback for file upload the way
    there is for chat. Not fixed in code. Not currently a hard blocker for
    this deployment's actual use case, though: the **Upload Files** tab is
    a secondary path — meeting minutes come from the fixed SharePoint
    folder via the **SharePoint Folder** tab, which doesn't use
    `file_uploader` at all.

  **Resolved**: this project has since moved to **container runtime** on
  the shared `STREAMLIT_COMPUTE_POOL_OCMS_BUSPERF` compute pool
  (`PROJECTS.COMPUTE_POOL` set accordingly — see the project-creation
  cell in the notebook), which gets a real, controllable Streamlit
  version (`streamlit[snowflake]==1.50.0`, pinned as an exact version in
  `pyproject.toml`/`requirements.txt`/`environment.yml` — a floor like
  `>=1.50.0` is unnecessary now that an exact working version is known,
  and avoids any resolver ambiguity). This needed two things warehouse
  runtime didn't: the `PYPI_ACCESS_INTEGRATION` external access
  integration (already existed on this account) with `USAGE` granted to
  `ADVANCEDANALYTICS`, and `USAGE`/`MONITOR` on the compute pool — both
  are `ACCOUNTADMIN`-only grants that `SYSADMIN` could not make on this
  account, confirmed by testing directly. The `st.chat_input`/
  `st.chat_message` fallback logic in `Chat.py` is left in place — it's
  harmless on a modern Streamlit version (the `hasattr` checks just take
  the native path) and is cheap insurance if this project ever has to
  fall back to warehouse runtime again.
- **`SYSTEM$GET_SECRET_STRING` as an ad-hoc SQL call fails with "Unknown
  function"** — it's only usable from inside an object (Streamlit app /
  UDF / procedure) that has the secret bound via a `SECRETS` clause on its
  own `CREATE`/`ALTER` statement, not as a plain `session.sql(...)` call
  from application code. This broke the SharePoint tab's "List files"
  entirely (`graph_client.py`'s `get_client_secret()` used the ad-hoc
  form). Fixed by adding `SECRETS = ('graph_secret' =
  MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET)` to the deploy cell's `CREATE
  STREAMLIT` statement (both runtime branches). Getting that clause to
  actually work took three more steps, each surfacing only once the prior
  one was fixed:
  1. The `SECRET` object itself has to exist — `CREATE SECRET ...
     SECRET_STRING = '<value>'` is a one-time, manual, **not committed to
     this repo** statement run directly in Snowsight by whoever holds the
     real Azure AD client secret value (see `sql/test_graph_connectivity.sql`,
     which keeps that statement commented out permanently, purely as
     documentation of the step).
  2. The role deploying the app needs `USAGE` on that secret
     (`GRANT USAGE ON SECRET MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET TO ROLE
     ADVANCEDANALYTICS`), or `CREATE OR REPLACE STREAMLIT` fails with
     "Secret ... does not exist or operation not authorized."
  3. The external access integration referenced alongside the `SECRETS`
     clause (`GRAPH_API_ACCESS_INTEGRATION`) must separately allow-list
     the secret via `ALTER EXTERNAL ACCESS INTEGRATION
     GRAPH_API_ACCESS_INTEGRATION SET ALLOWED_AUTHENTICATION_SECRETS =
     (MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET)`, or deploy fails with
     "Integrations do not allow secret '...'". Like the other EAI/compute
     pool grants in this project, both (2) and (3) needed `ACCOUNTADMIN`.
  4. **Reading** a bound secret from Python differs by runtime, and the
     two are mutually exclusive: warehouse runtime exposes it via the
     `_snowflake` module (`_snowflake.get_generic_secret_string(alias)`),
     while container runtime — what this app actually runs on — has no
     `_snowflake` module at all and instead exposes it through
     `st.secrets[alias]` (also mirrored into `os.environ`). Using the
     warehouse-runtime API on container runtime fails at import time with
     `No module named '_snowflake'`. `get_client_secret()` now tries
     `st.secrets["graph_secret"]` first and falls back to `_snowflake`
     only if that raises, so it works on either runtime.

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
Streamlit app via the `CREATE_PROJECT` procedure — see
`python/provisioning/create_project.py` or call it directly from SQL), but
this deployment is dedicated to `ORG_MM_CHAT` and provisioning a project
isn't exposed in the app's UI — it's an admin/notebook operation, done
once.

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
│   ├── Chat.py                          # entry point AND the chat UI (default/landing page)
│   ├── environment.yml                 # warehouse-runtime dependencies (conda)
│   ├── pyproject.toml                  # container-runtime dependencies (pip/uv)
│   ├── requirements.txt                # pip reference list (local dev / external session use)
│   └── pages/
│       ├── 1_Data_Sources.py
│       └── 2_Sync_Status.py
└── pipeline/
    └── 00_provision_project.ipynb      # connect, catalog setup, create ORG_MM_CHAT, deploy
```
