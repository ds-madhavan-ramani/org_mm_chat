import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st
from snowflake_session import get_session

st.set_page_config(page_title="New Project — LLM Wiki", page_icon="➕", layout="wide")

st.title("➕ Create a new project")
st.caption(
    "This provisions an isolated schema (MEDSOCMS.DATA_<CODE>) and registers "
    "the project in the catalog. Nothing about documents is required here — "
    "add those afterwards from the Data Sources page."
)

session = get_session()

with st.form("new_project_form"):
    code = st.text_input(
        "Project code", placeholder="CONTRACT_ANALYSIS",
        help="Short, upper_snake_case, becomes schema DATA_<CODE>",
    )
    name = st.text_input("Display name", placeholder="Contract Analysis LLM Wiki")
    description = st.text_area("Description (optional)")
    sharepoint_site = st.text_input("Default SharePoint site URL (optional)")
    sharepoint_folder = st.text_input("Default SharePoint folder URL (optional)",
                                      help="Pre-fills the folder field on the Data Sources page")
    submitted = st.form_submit_button("Create project", type="primary")

if submitted:
    if not code or not name:
        st.error("Project code and display name are required.")
    else:
        try:
            # st.experimental_user was renamed to st.user in Streamlit 1.4x+
            # (this app pins streamlit[snowflake]>=1.54.0, where the
            # experimental name is gone) — try the current API first.
            if hasattr(st, "user"):
                created_by = getattr(st.user, "email", "") or ""
            elif hasattr(st, "experimental_user"):
                created_by = getattr(st.experimental_user, "email", "") or ""
            else:
                created_by = ""

            result = session.sql(
                "CALL CREATE_PROJECT(?, ?, ?, ?, ?, ?, ?, ?)",
                params=[code, name, description, sharepoint_site, sharepoint_folder,
                        created_by, "", ""],
            ).collect()
            st.success(result[0][0])
            st.info("Reload the app (or reselect from the sidebar) to see the new project.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not create project: {e}")
