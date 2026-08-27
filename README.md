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
| Segmentation granularity | `STANDARD` |
| Reranking | Enabled |
| Vector/semantic search | Enabled |
| Max candidate docs | `10` |
| Warehouse / runtime | `MTMWH02`, warehouse runtime (no compute pool) |

These are all set in `pipeline/00_provision_project.ipynb` Step 2 — you
don't need to re-enter them, they're already parameterized for this project.
The last four rows are per-project retrieval-mechanism toggles — see
[How retrieval works, and getting more thorough answers](#how-retrieval-works-and-getting-more-thorough-answers)
for what each one does and what it costs.

## Prerequisites

1. Snowflake access to the `ADVANCEDANALYTICS` role, `MTMWH02` warehouse,
   and `MEDSOCMS` database.
2. A Microsoft Graph API app registration with `Sites.Selected` permission
   granted on the `cabinet-mr4` SharePoint site, with:
   - Tenant ID / Client ID hardcoded as `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID`
     constants in `python/config.py` — not secret (Microsoft treats both as
     public identifiers, visible on the app registration's own Overview
     page), unlike the client secret below
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
  run. Click **List files** — the folder holds 500+ items (agendas,
  reports, minutes, and several draft/initial/final/updated/revised
  versions per meeting), so by default the page uses
  `BIS_ORG_Meeting_Minutes.xlsx`'s **FileName column as a register**:
  whoever maintains that workbook has already resolved, per meeting,
  which single file is the canonical one — the app downloads it, reads
  that column (`extract_column_values` in `xlsx_parser.py`, matched
  case/spacing-insensitively against the folder's actual file names via
  `filename_match_keys`), and pre-ticks only those ~90–100 files. A
  checkbox above the list shows how many register entries matched an
  actual file and lets you turn this off in favor of a plain name filter
  (defaults to `minutes`) if you'd rather browse everything, or if no
  register workbook is found in the folder at all. A caption right below
  "Found N file(s)" always states, in plain language, whether a
  `BIS_ORG_Meeting_Minutes` workbook was found in *this* folder listing
  and how many FileName values it yielded — the register can only be
  read if it's located inside the folder being listed here, not
  elsewhere on the site. Tick what you want, click **Ingest selected
  files**.
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

### How retrieval works, and getting more thorough answers

Retrieval is a two-stage LLM tree search — document-level, then
section-level — with several other mechanisms layered on top. Not every
LLM Wiki deployment needs every mechanism, so each one that has a real
cost (extra LLM calls, a reindex, slower answers) is a per-project toggle
on the `PROJECTS` catalog table (`MEDSOCMS.APP_CATALOG.PROJECTS`, one row
per project — set it via the provisioning notebook's project-creation
cell, or `UPDATE ... PROJECTS SET ... WHERE PROJECT_CODE = ...` directly).
The rest are cheap correctness/quality fixes with no real downside, so
they're always on rather than configurable.

**Always on (minimum baseline, not configurable):**

| Mechanism | What it does | Code |
|---|---|---|
| Keyword-search fallback | Literal `RAW_TEXT ILIKE` search on terms extracted from the question, used when document-routing finds nothing — catches specific codes/IDs/acronyms a document summary wouldn't mention | `query_engine._keyword_fallback_doc_ids` |
| All-sections fallback | Falls back to every section in a document judged relevant when section-routing (or reranking, see below) picks none — an over-conservative pick shouldn't produce an empty answer | `query_engine.search` |
| Richer segmentation prompt | `ORG_MEETING_MINUTES` profile asks the indexing model for a thorough per-section paragraph, verbatim codes/IDs/acronyms — routing accuracy depends on how much a summary actually captures | `index_builder.PROMPTS`, `PROJECTS.SEGMENTATION_PROFILE` |
| Chronological ordering | Synthesis prompt instructs oldest-first bullet ordering when an answer spans multiple dated meetings/events | `query_engine.search`'s `synthesis_prompt` |
| Citation numbering | Sources are numbered deterministically in code, not left to the model, so the Sources list is always correct | `query_engine.search` |

**Configurable per project (`PROJECTS` columns):**

| Column | Default | What it controls | Code |
|---|---|---|---|
| `ENABLE_RERANKING` | `TRUE` | Whether an extra LLM call judges/filters the section candidate pool before synthesis (a "reranker"), vs. taking the whole pool directly — cheaper/faster with it off, at the cost of the LLM never narrowing down an oversized pool itself | `query_engine.search`'s `project.enable_reranking` gate |
| `ENABLE_VECTOR_SEARCH` | `FALSE` | Whether sections are also found by embedding similarity (`AI_EMBED`) as a third retrieval signal alongside document routing and keyword search, and whether indexing computes embeddings at all — needs a reindex to backfill `NODE_EMBEDDING` on existing sections once turned on | `index_builder._index_one_document`, `query_engine._vector_search_section_ids`, gated by `project.enable_vector_search` |
| `MAX_CANDIDATE_DOCS` | `10` | Cap on documents one question's answer can draw from — a config number, clamped to **5–10** in code (`ProjectConfig.clamped_max_candidate_docs`; Snowflake accepts `CHECK` constraint syntax but never enforces it, so this isn't a DB-level guarantee) | `query_engine.search` |
| `SEGMENTATION_GRANULARITY` | `'STANDARD'` | `'DETAILED'` pushes the indexing prompt to split each natural break (meeting/sheet) further into one section per topic/agenda item, instead of one section per meeting — trades more, narrower sections (better precision, more indexing calls) for fewer, broader ones | `index_builder.SEGMENTATION_GRANULARITY_INSTRUCTIONS` |

`MAX_CANDIDATE_SECTIONS` (20, the per-question section cap once a
candidate pool exists) is still a `query_engine.py` module constant, not
yet a `PROJECTS` column — raise it there directly for more complete
answers on broad/thematic questions (e.g. "all references to X across the
minutes"), at the cost of a longer, slower, more expensive synthesis call,
since every extra section adds up to `MAX_SECTION_CHARS` (a `PROJECTS`
column) of excerpt text to the final prompt.

Deep-dive detail on a few of the mechanisms above:

- **Exact code/ID/acronym lookups** (e.g. "find all references to A9605")
  are a different retrieval need than thematic questions, and document
  routing alone handles them poorly: a document's summary is a broad
  gloss of an entire meeting and won't reliably contain every specific
  code mentioned in the raw text, even a very good one. When document
  routing returns nothing, `search()` now falls back to a literal
  `RAW_TEXT ILIKE` search on terms the model extracts from the question
  (`_keyword_fallback_doc_ids`) — a genuinely irrelevant question still
  correctly returns "I couldn't find a document relevant to that
  question," but a specific-code question that summary-routing missed
  gets a second chance via exact text match.
- **Hybrid retrieval + reranking** (`ENABLE_VECTOR_SEARCH` /
  `ENABLE_RERANKING`): `DOCUMENT_INDEX.NODE_EMBEDDING`
  (`VECTOR(FLOAT, 768)`, section-level only) holds each section's
  semantic embedding, computed at index time via
  `AI_EMBED('snowflake-arctic-embed-m', title + summary)` — only when the
  project's `ENABLE_VECTOR_SEARCH` is on; off, indexing skips `AI_EMBED`
  entirely and inserts sections without an embedding
  (`index_builder.py`'s `EMBED_MODEL`, imported by `query_engine.py` so
  the two can't drift out of sync — comparing vectors from different
  models is meaningless). At query time, when `ENABLE_VECTOR_SEARCH` is
  on, `_vector_search_section_ids()` embeds the question the same way and
  finds the top `MAX_VECTOR_CANDIDATES` (15) sections by
  `VECTOR_COSINE_SIMILARITY`, **project-wide** — independent of which
  documents Stage 1's summary-based routing selected, since a document's
  summary is a lossy compression that can miss a semantically-relevant
  section entirely even when the wording doesn't match well. Those
  sections are unioned into Stage 2's candidate pool. Then, when
  `ENABLE_RERANKING` is on, the section-routing LLM call judges that
  (possibly hybrid-sourced) pool — effectively a **reranking** pass over
  summary-routed + vector-found sections together, rather than only ever
  seeing Stage 1's narrower pick; when `ENABLE_RERANKING` is off, that
  call is skipped and the whole pool is used directly (cheaper/faster,
  same as the all-sections fallback above).
  - The embedding-at-index-time and search-at-query-time calls also
    degrade gracefully if `AI_EMBED` is unavailable even with the toggle
    on: `index_builder.py` tries it once per indexing run and disables it
    for the rest of that run on the first failure (not per-document — no
    reason to fail identically ~90 times if it's an account-wide
    capability gap), falling back to indexing that section without an
    embedding rather than failing the document; `_vector_search_section_ids()`
    catches any failure and returns `[]`, silently reducing to the other
    signals. Neither ever raises up into the user-facing answer.
  - **Sections indexed before `ENABLE_VECTOR_SEARCH` was turned on (or
    before this feature shipped) have `NODE_EMBEDDING = NULL`** and won't
    be found by vector search until reindexed — enabling the toggle
    doesn't retroactively embed existing rows. Data Sources → Index →
    **Rebuild all** to backfill.
- The `ORG_MEETING_MINUTES` segmentation prompt (`index_builder.py`) asks
  the indexing model to write a thorough paragraph per section — quoting
  reference codes/IDs/acronyms verbatim rather than paraphrasing them —
  since routing accuracy depends entirely on how much a section's summary
  actually captures. `SEGMENTATION_GRANULARITY = 'DETAILED'` layers on
  top of any profile (orthogonal setting) to push further, splitting each
  natural break into one section per topic/agenda item instead of one per
  meeting. **Takes a full reindex to apply to already-indexed
  documents** — Data Sources → Index → **Rebuild all**, which re-runs
  every document through Cortex again (91 documents ≈ 91 LLM calls; not
  free, and not instant).
  - This asks for genuinely more detail than a short gloss, which
    routinely produces summaries longer than `DOCUMENT_INDEX
    .NODE_SUMMARY`'s original `VARCHAR(4000)` — Snowflake errors on an
    overlong `INSERT` ("... is too long and would be truncated") rather
    than silently truncating, which failed indexing outright for any
    document whose summary crossed that line. Fixed two ways: the column
    is now `VARCHAR(8000)` (new projects get this from
    `sql/00_setup_catalog.sql`; the existing project needed the
    `NODE_SUMMARY` migration in the provisioning notebook's "Schema
    migrations" cell), and `index_builder.py` now also truncates
    defensively in code (`MAX_NODE_SUMMARY_CHARS`/`_truncate()`) as a
    backstop — a still-longer response degrades gracefully instead of
    failing the whole document, regardless of the column width.
  - The `ALTER COLUMN ... SET DATA TYPE VARCHAR(8000)` migration itself
    failed the first time with "cannot change column NODE_SUMMARY from
    type VARCHAR(4000) COLLATE 'en-ci' to VARCHAR(8000) because they
    have incompatible collations" — this account applies a default
    collation (`en-ci`) to `VARCHAR` columns, and Snowflake requires an
    `ALTER COLUMN ... SET DATA TYPE` to match the existing column's
    collation exactly, even for a pure length widen with no other type
    change. Fixed by specifying it explicitly:
    `VARCHAR(8000) COLLATE 'en-ci'`. Only matters for `ALTER COLUMN`
    migrations on an existing column — a fresh `CREATE TABLE` isn't
    affected, since there's no prior collation to conflict with. Any
    future `ALTER COLUMN ... SET DATA TYPE` migration added to the
    provisioning notebook's "Schema migrations" cell on this account
    will need the same explicit `COLLATE 'en-ci'`.
- The synthesis prompt instructs the model to order bullets
  chronologically (oldest first) when an answer spans multiple dated
  meetings/events, using whatever dates appear in the excerpts —
  `DOCUMENT_INDEX`/`RAW_DOCUMENTS` has no structured, reliably-populated
  date column to sort by in code (`RAW_DOCUMENTS.DOCUMENT_DATE` exists
  but is never actually set at ingest time), so this is a prompt
  instruction, not a deterministic guarantee the way citation numbering
  is.

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
- **`GRAPH_TENANT_ID`/`GRAPH_CLIENT_ID` read as empty strings, causing
  "Token request failed (404)"** — `config.py` originally read both via
  `os.environ.get(..., "")`, on the assumption (never actually
  implemented) that the deploy cell would set them as environment
  variables. `CREATE STREAMLIT` has no generic environment-variable
  injection clause — only `SECRETS` — so nothing ever set them, both
  silently defaulted to `""`, and the resulting token request went to
  `https://login.microsoftonline.com//oauth2/v2.0/token` (empty tenant
  segment), which Azure AD 404s. Fixed by hardcoding both as plain string
  constants in `config.py`, same as `DATABASE`/`ROLE`/`WAREHOUSE_NAME` —
  unlike the client secret, the tenant ID and client ID aren't
  confidential (Microsoft surfaces both on the app registration's own
  Overview page to any reader), so there's no reason to route them
  through the secret/grant/allow-list chain above.
- **`AI_PARSE_DOCUMENT` type error on non-xlsx uploads/SharePoint files:
  "Invalid argument types for function 'AI_PARSE_DOCUMENT$V4':
  (VARCHAR, VARIANT)"** — `AI_PARSE_DOCUMENT` needs a `FILE`-typed
  argument, built via `TO_FILE('@stage', 'path')`; `BUILD_SCOPED_FILE_URL`
  returns a plain `VARCHAR` URL instead, which fails this way. Also fixed
  separately: `BUILD_SCOPED_FILE_URL`'s stage argument is a bare `@stage`
  reference resolved at SQL parse time and can't be a bind parameter at
  all (a different error, "Argument 1 ... cannot be null or empty") —
  `TO_FILE`'s stage argument is an ordinary quoted string, so switching to
  it fixes both problems and lets both arguments stay bind parameters.
  Both `sharepoint_ingest.py` and `file_ingest.py` had this bug
  independently (same `AI_PARSE_DOCUMENT` call, copy-pasted).
- **`complete_json()` failing on markdown-fenced or trailing-content
  responses** — models don't reliably follow "return ONLY valid JSON, no
  commentary." Three distinct failure shapes hit in production: the whole
  fenced answer escaped as one JSON string
  (`'"```json\n{...}\n```"'`), a valid JSON object followed by a prose
  explanation after the closing fence (`'```json\n{...}\n```\n\nBased on
  my review...'`, which plain `json.loads()` rejects as "Extra data" even
  though the JSON itself is fine), and doubly-encoded JSON. Fixed by
  switching to `json.JSONDecoder().raw_decode()` (parses just the first
  complete JSON value, ignoring anything trailing it) plus a bounded
  unwrap loop for JSON-string-encoded results, with markdown fences
  stripped at each unwrap level, not just before the first parse attempt.
- **Chat answers showing literal `\n` instead of line breaks, and
  citations with no numbering or links** — the final answer-synthesis
  call (`complete()`, not `complete_json()` — there's no JSON involved)
  sometimes emits the literal two-character sequence `\n` instead of a
  real newline, the same unreliable-formatting-instructions pattern as
  above, just showing up as visible backslash-n text in the chat UI
  rather than a parse error. `query_engine.py` now normalizes that after
  the model call. Separately, citations are now numbered and, when the
  source document came from SharePoint, clickable — `RAW_DOCUMENTS` grew
  a `SOURCE_URL` column (Graph API's `webUrl`, captured at ingest time;
  `NULL` for direct uploads, which have no SharePoint source to link to).
  **The already-created `ORG_MM_CHAT` project's live schema needs a
  one-time manual migration** (new projects get the column automatically
  from the updated `sql/00_setup_catalog.sql` template):
  ```sql
  ALTER TABLE MEDSOCMS.DATA_ORG_MM_CHAT.RAW_DOCUMENTS
    ADD COLUMN IF NOT EXISTS SOURCE_URL VARCHAR(2000);
  ```
  Existing rows will have `SOURCE_URL = NULL` until then; a citation for
  one of those just renders without a link, not an error. From there,
  re-running **List files → Ingest selected files** on the SharePoint tab
  backfills `SOURCE_URL` for already-ingested files too — even an
  unchanged file's "skipped, unchanged" path now cheaply updates just
  that column instead of requiring a full re-parse.

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
