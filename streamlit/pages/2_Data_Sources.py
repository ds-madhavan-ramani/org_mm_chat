import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import streamlit as st
from snowflake_session import get_session
from ingestion.file_ingest import ingest_uploaded_files
from ingestion.sharepoint_ingest import list_sharepoint_folder, ingest_selected_files
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
    "pick what to bring in."
)

tab_upload, tab_sharepoint, tab_index = st.tabs(["📤 Upload Files", "🔗 SharePoint Folder", "🌳 Index"])

# ---------------------------------------------------------------------------
with tab_upload:
    st.subheader("Upload files")
    uploaded = st.file_uploader(
        "Choose files (PDF, DOCX, TXT)", type=["pdf", "docx", "txt"], accept_multiple_files=True
    )
    if uploaded and st.button("Ingest uploaded files", type="primary"):
        with st.spinner(f"Ingesting {len(uploaded)} file(s)…"):
            results = ingest_uploaded_files(session, project, uploaded)
        for r in results:
            if r.status == "INGESTED":
                st.success(f"✅ {r.file_name} — ingested (doc_id={r.doc_id})")
            elif r.status == "SKIPPED_DUPLICATE":
                st.info(f"↪️ {r.file_name} — already ingested, skipped")
            else:
                st.error(f"❌ {r.file_name} — {r.error}")
        st.info("Go to the **Index** tab to build/update the tree index for these documents.")

# ---------------------------------------------------------------------------
with tab_sharepoint:
    st.subheader("Ingest from a SharePoint folder")
    default_folder = project.sharepoint_default_folder or ""
    folder_url = st.text_input("SharePoint folder URL", value=default_folder,
                               placeholder="https://metrotrains.sharepoint.com/:f:/s/.../...")

    if "sp_listing" not in st.session_state:
        st.session_state["sp_listing"] = None

    if st.button("List files in folder"):
        with st.spinner("Listing folder from SharePoint…"):
            try:
                st.session_state["sp_listing"] = list_sharepoint_folder(session, folder_url)
                st.session_state["sp_folder_url"] = folder_url
            except Exception as e:  # noqa: BLE001
                st.error(f"Couldn't list that folder: {e}")
                st.session_state["sp_listing"] = None

    listing = st.session_state.get("sp_listing")
    if listing:
        st.write(f"Found **{len(listing)}** file(s):")
        selected_names = st.multiselect(
            "Select files to ingest",
            options=[item.name for item in listing],
            default=[item.name for item in listing],
        )
        if st.button("Ingest selected files", type="primary"):
            selected_items = [i for i in listing if i.name in selected_names]
            with st.spinner(f"Ingesting {len(selected_items)} file(s) from SharePoint…"):
                results = ingest_selected_files(
                    session, project, st.session_state["sp_folder_url"], selected_items
                )
            for r in results:
                if r.status == "INGESTED":
                    st.success(f"✅ {r.file_name} — ingested (doc_id={r.doc_id})")
                elif r.status == "SKIPPED_DUPLICATE":
                    st.info(f"↪️ {r.file_name} — already ingested, skipped")
                else:
                    st.error(f"❌ {r.file_name} — {r.error}")
            st.info("Go to the **Index** tab to build/update the tree index for these documents.")

# ---------------------------------------------------------------------------
with tab_index:
    st.subheader("Build / refresh the tree index")
    st.write(
        "Documents are searchable in Chat only after they've been indexed. "
        "New documents are indexed automatically the first time; use "
        "**Rebuild all** if you've changed the segmentation profile."
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
