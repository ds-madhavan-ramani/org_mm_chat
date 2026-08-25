import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st
from snowflake_session import get_session
from query_engine import search

st.set_page_config(page_title="Chat — LLM Wiki", page_icon="💬", layout="wide")

if "project" not in st.session_state:
    st.warning("Select a project on the home page first.")
    st.stop()

session = get_session()
project = st.session_state["project"]

st.title(f"💬 Chat — {project.project_name}")

if "messages" not in st.session_state:
    st.session_state["messages"] = []

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])
        if msg.get("cited_docs"):
            with st.expander(f"Sources ({len(msg['cited_docs'])})"):
                for doc in msg["cited_docs"]:
                    st.write(f"- {doc}")

question = st.chat_input("Ask a question about this project's documents…")
if question:
    st.session_state["messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("Searching…"):
            result = search(session, project, question)
        st.write(result.answer)
        if result.cited_docs:
            with st.expander(f"Sources ({len(result.cited_docs)})"):
                for doc in result.cited_docs:
                    st.write(f"- {doc}")
        if result.from_cache:
            st.caption("⚡ Answered from cache")

    st.session_state["messages"].append({
        "role": "assistant",
        "content": result.answer,
        "cited_docs": result.cited_docs,
    })
