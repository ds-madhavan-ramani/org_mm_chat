"""
streamlit/Chat.py — entry point AND the chat UI itself.

This deployment is dedicated to one project (ORG_MM_CHAT) — a separate
"select a project" landing screen before you can even see the chat isn't
useful for a single-purpose app, so this page loads the project directly
and *is* the default view. If the catalog ever holds more than one active
project (see README "Under the hood"), a picker still appears — it just
isn't the app's front door.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "python"))

import streamlit as st
from snowflake_session import get_session
from config import load_project, list_active_projects
from query_engine import search

st.set_page_config(page_title="Chat — LLM Wiki", page_icon="💬", layout="wide")

session = get_session()

st.sidebar.title("📚 LLM Wiki")

projects = list_active_projects(session)
if not projects:
    st.sidebar.warning("No projects exist yet.")
    st.title("Welcome to LLM Wiki")
    st.write(
        "No projects have been created yet. This deployment is provisioned "
        "for the ORG_MM_CHAT project via `pipeline/00_provision_project.ipynb` "
        "— run that notebook's project-creation step, then reload this app."
    )
    st.stop()

if len(projects) == 1:
    selected_code = projects[0]["PROJECT_CODE"]
else:
    project_labels = {f"{p['PROJECT_NAME']} ({p['PROJECT_CODE']})": p["PROJECT_CODE"] for p in projects}
    selected_label = st.sidebar.selectbox("Active project", list(project_labels.keys()))
    selected_code = project_labels[selected_label]

st.session_state["project_code"] = selected_code
st.session_state["project"] = load_project(session, selected_code)
project = st.session_state["project"]

st.sidebar.divider()

st.title(f"💬 {project.project_name}")
if project.description:
    st.caption(project.description)

# Streamlit < 1.24 has neither st.chat_input nor st.chat_message — fall
# back to plain widgets so this page still works regardless of the actual
# runtime version (pinning streamlit=1.32.3 in environment.yml has not
# reliably taken effect for warehouse runtime on this account; see README).
_HAS_CHAT_UI = hasattr(st, "chat_input") and hasattr(st, "chat_message")


def _render_message(role: str, content: str, cited_docs=None, from_cache: bool = False):
    ctx = st.chat_message(role) if _HAS_CHAT_UI else st.container()
    with ctx:
        if not _HAS_CHAT_UI:
            st.markdown(f"**{'You' if role == 'user' else 'Assistant'}:**")
        st.write(content)
        if cited_docs:
            with st.expander(f"Sources ({len(cited_docs)})"):
                for doc in cited_docs:
                    st.write(f"- {doc}")
        if from_cache:
            st.caption("⚡ Answered from cache")


if "messages" not in st.session_state:
    st.session_state["messages"] = []

for msg in st.session_state["messages"]:
    _render_message(msg["role"], msg["content"], msg.get("cited_docs"))

if _HAS_CHAT_UI:
    question = st.chat_input("Ask a question about this project's documents…")
else:
    with st.form("chat_form", clear_on_submit=True):
        question_input = st.text_input("Ask a question about this project's documents…")
        asked = st.form_submit_button("Ask")
    question = question_input if asked and question_input else None

if question:
    st.session_state["messages"].append({"role": "user", "content": question})
    _render_message("user", question)

    with st.spinner("Searching…"):
        result = search(session, project, question)
    _render_message("assistant", result.answer, result.cited_docs, result.from_cache)

    st.session_state["messages"].append({
        "role": "assistant",
        "content": result.answer,
        "cited_docs": result.cited_docs,
    })
