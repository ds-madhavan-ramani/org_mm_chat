import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st
from snowflake_session import get_session
from ingestion.file_ingest import ingest_uploaded_files
from ingestion.sharepoint_ingest import (
    list_sharepoint_folder, ingest_selected_files, get_canonical_filenames, filename_match_keys,
)
from ingestion.index_builder import build_index_for_project

st.set_page_config(page_title="Data Sources — LLM Wiki", page_icon="📁", layout="wide")

if "project" not in st.session_state:
    st.warning("Select a project on the home page first.")
    st.stop()

session = get_session()
project = st.session_state["project"]

st.title(f"📁 Data Sources — {project.project_name}")
st.caption(
    "Add documents any time — there's no setup step required before this app "
    "is usable. Upload files directly, or point at a SharePoint folder and "
    "pick what to bring in. Newly ingested or updated documents are indexed "
    "automatically, right after ingest."
)


def _reindex(session, project, results) -> int:
    """Indexes/re-indexes exactly the documents this run touched (new or
    changed) — a rebuild for those doc_ids specifically, not the whole
    project, so an unrelated document's index isn't rebuilt on every run."""
    doc_ids = [r.doc_id for r in results if r.status in ("INGESTED", "UPDATED") and r.doc_id]
    if not doc_ids:
        return 0
    return build_index_for_project(session, project, doc_ids=doc_ids, rebuild=True)


def _status_line(r) -> str:
    if r.status == "INGESTED":
        return f"✅ {r.file_name} — ingested (doc_id={r.doc_id})"
    if r.status == "UPDATED":
        return f"🔄 {r.file_name} — content changed, index refreshed (doc_id={r.doc_id})"
    if r.status == "SKIPPED_DUPLICATE":
        return f"↪️ {r.file_name} — unchanged, skipped"
    return f"❌ {r.file_name} — {r.error}"


tab_upload, tab_sharepoint, tab_index = st.tabs(["📤 Upload Files", "🔗 SharePoint Folder", "🌳 Index"])

# ---------------------------------------------------------------------------
with tab_upload:
    st.subheader("Upload files")
    uploaded = st.file_uploader(
        "Choose files (PDF, DOCX, TXT, XLSX)", type=["pdf", "docx", "txt", "xlsx", "xlsm"],
        accept_multiple_files=True
    )
    if uploaded and st.button("Ingest uploaded files", type="primary"):
        with st.spinner(f"Ingesting {len(uploaded)} file(s)…"):
            results = ingest_uploaded_files(session, project, uploaded)
            indexed_count = _reindex(session, project, results)
        for r in results:
            fn = st.success if r.status == "INGESTED" else (
                st.info if r.status == "SKIPPED_DUPLICATE" else st.error)
            fn(_status_line(r))
        if indexed_count:
            st.success(f"Indexed {indexed_count} document(s) — ready to ask about in Chat.")

# ---------------------------------------------------------------------------
with tab_sharepoint:
    st.subheader("Ingest from SharePoint")
    default_folder = project.sharepoint_default_folder or ""
    st.caption("📁 Source: Clause 6.6(d) OCMS Review Group Minutes")

    with st.expander("Use a different SharePoint folder instead"):
        override_folder = st.text_input(
            "SharePoint folder URL", placeholder="https://metrotrains.sharepoint.com/:f:/s/.../..."
        )
    folder_url = override_folder.strip() if override_folder.strip() else default_folder

    if "sp_listing" not in st.session_state:
        st.session_state["sp_listing"] = None

    if not folder_url:
        st.warning("This project has no SharePoint folder configured — use the override above.")
    elif st.button("List files in folder"):
        with st.spinner("Listing folder from SharePoint…"):
            try:
                listing = list_sharepoint_folder(session, folder_url)
                st.session_state["sp_listing"] = listing
                st.session_state["sp_folder_url"] = folder_url
                st.session_state["sp_canonical_names"] = get_canonical_filenames(
                    session, folder_url, listing
                )
            except Exception as e:  # noqa: BLE001
                st.error(f"Couldn't list that folder: {e}")
                st.session_state["sp_listing"] = None
                st.session_state["sp_canonical_names"] = []

    listing = st.session_state.get("sp_listing")
    canonical_names = st.session_state.get("sp_canonical_names") or []
    if listing:
        st.write(f"Found **{len(listing)}** file(s) in this folder.")

        use_register = False
        register_matches = []
        if canonical_names:
            register_keys = set()
            for n in canonical_names:
                register_keys |= filename_match_keys(n)
            register_matches = [
                item for item in listing if filename_match_keys(item.name) & register_keys
            ]
            use_register = st.checkbox(
                f"Only show canonical files from BIS_ORG_Meeting_Minutes.xlsx's "
                f"FileName column ({len(register_matches)} of {len(canonical_names)} "
                f"register entries matched a file in this folder)",
                value=True,
                help="This register is maintained as the source of truth for which "
                     "single version (Draft / Initial / Final / Updated / Revised, ...) "
                     "is canonical per meeting. Uncheck to fall back to a plain name "
                     "filter over every file in the folder instead.",
            )

        if canonical_names and use_register:
            pool = register_matches
        else:
            name_filter = st.text_input(
                "Filter by file name",
                value="minutes",
                help='Matches this text anywhere in the file name (case-insensitive). '
                     'Defaults to "minutes" so only actual meeting-minutes files show up '
                     'below, out of everything in the folder (agendas, reports, etc). '
                     'Clear it, or change it to something like "agenda", when you\'re '
                     'ready to bring in other content.',
            )
            pool = (
                [item for item in listing if name_filter.strip().lower() in item.name.lower()]
                if name_filter.strip() else listing
            )
            st.caption(f'Showing {len(pool)} of {len(listing)} file(s) matching "{name_filter}".')

        selected_names = st.multiselect(
            "Select files to ingest",
            options=[item.name for item in pool],
            default=[item.name for item in pool],
        )
        if st.button("Ingest selected files", type="primary"):
            selected_items = [i for i in listing if i.name in selected_names]
            with st.spinner(f"Ingesting {len(selected_items)} file(s) from SharePoint…"):
                results = ingest_selected_files(
                    session, project, st.session_state["sp_folder_url"], selected_items
                )
                indexed_count = _reindex(session, project, results)
            for r in results:
                fn = st.success if r.status in ("INGESTED", "UPDATED") else (
                    st.info if r.status == "SKIPPED_DUPLICATE" else st.error)
                fn(_status_line(r))
            if indexed_count:
                st.success(f"Indexed {indexed_count} document(s) — ready to ask about in Chat.")

# ---------------------------------------------------------------------------
with tab_index:
    st.subheader("Manual index rebuild")
    st.write(
        "Documents are indexed automatically right after they're ingested "
        "or updated — you shouldn't normally need this tab. Use **Rebuild "
        "all** only if the segmentation profile changes, or the index "
        "otherwise needs to be regenerated from scratch."
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Index new/unindexed documents"):
            with st.spinner("Building index…"):
                count = build_index_for_project(session, project, rebuild=False)
            st.success(f"Indexed {count} document(s).")
    with col2:
        if st.button("Rebuild all", type="secondary"):
            with st.spinner("Rebuilding full index…"):
                count = build_index_for_project(session, project, rebuild=True)
            st.success(f"Rebuilt index for {count} document(s).")
