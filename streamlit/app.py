"""
streamlit/app.py — entry point.

Key change from ocms-llm-wiki: this app no longer assumes one fixed project.
The sidebar lets the user pick any ACTIVE project from the catalog, and every
page reads project config from session_state rather than a hardcoded schema.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "python"))

import streamlit as st
from snowflake_session import get_session
from config import load_project, list_active_projects

st.set_page_config(page_title="LLM Wiki", page_icon="📚", layout="wide")

session = get_session()

st.sidebar.title("📚 LLM Wiki")

projects = list_active_projects(session)
if not projects:
    st.sidebar.warning("No projects exist yet.")
    st.title("Welcome to LLM Wiki")
    st.write(
        "No projects have been created yet. Use the **New Project** page "
        "(left sidebar) to create your first one — it only needs a name; "
        "you can add documents afterwards from the Data Sources page."
    )
    st.page_link("pages/4_New_Project.py", label="➕ Create a project", icon="➕")
    st.stop()

project_labels = {f"{p['PROJECT_NAME']} ({p['PROJECT_CODE']})": p["PROJECT_CODE"] for p in projects}
selected_label = st.sidebar.selectbox("Active project", list(project_labels.keys()))
selected_code = project_labels[selected_label]

st.session_state["project_code"] = selected_code
st.session_state["project"] = load_project(session, selected_code)

st.sidebar.divider()
st.sidebar.page_link("pages/1_Chat.py", label="💬 Chat")
st.sidebar.page_link("pages/2_Data_Sources.py", label="📁 Data Sources")
st.sidebar.page_link("pages/3_Sync_Status.py", label="📊 Sync Status")
st.sidebar.page_link("pages/4_New_Project.py", label="➕ New Project")

st.title(f"📚 {st.session_state['project'].project_name}")
if st.session_state["project"].description:
    st.caption(st.session_state["project"].description)

st.write(
    "Use the pages in the left sidebar: **Chat** to ask questions, "
    "**Data Sources** to add documents (upload files or pull from "
    "SharePoint — nothing needs to be pre-loaded), and **Sync Status** to "
    "see what's been ingested."
)
