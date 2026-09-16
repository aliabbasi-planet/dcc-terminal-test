"""Streamlit session-state helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

from .database import DatabaseConnection
from .execution import TestResult

DEFAULTS: dict[str, Any] = {
    "db_connected": False,
    "connection": None,
    "driver_used": None,
    "active_env": "DEV",
    "connected_env": None,
    "connected_login": None,
    "terminals": None,
    "instances": None,
    "locations": None,
    "selected_terminal": "",
    "results": [],
    "campaigns": [],
    "session_history": [],
    "readiness": None,
    "procedure_version": None,
}

# Keys that must survive a disconnect so the sidebar keeps its selection.
_PRESERVED_ON_RESET = {"active_env", "session_history"}

# Sidebar credential widgets are already on screen when a new login is made, and
# Streamlit forbids rewriting the state of an instantiated widget.
_CONNECTION_WIDGET_PREFIXES = ("db_server_", "db_database_", "db_user_", "db_pass_")


def init_state() -> None:
    for key, value in DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_state() -> None:
    for key, value in DEFAULTS.items():
        if key not in _PRESERVED_ON_RESET:
            st.session_state[key] = value


def archive_current_session() -> None:
    """Keep prior factual results available without carrying them into a new login."""
    current_results = st.session_state.results
    if not current_results:
        return

    st.session_state.session_history.insert(
        0,
        {
            "environment": st.session_state.connected_env,
            "login": st.session_state.connected_login,
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            "results": current_results.copy(),
        },
    )


def start_new_session() -> None:
    """Archive the current run and clear all test UI state for a new login."""
    archive_current_session()
    for key in list(st.session_state):
        if key in _PRESERVED_ON_RESET or key.startswith(_CONNECTION_WIDGET_PREFIXES):
            continue
        del st.session_state[key]
    init_state()


def clear_reference_cache() -> None:
    st.session_state.terminals = None
    st.session_state.instances = None
    st.session_state.locations = None
    st.session_state.readiness = None
    st.session_state.procedure_version = None


def connection() -> DatabaseConnection:
    conn = st.session_state.connection
    if conn is None:
        raise RuntimeError("No active database connection.")
    return conn


def results() -> list[TestResult]:
    return st.session_state.results


def add_result(result: TestResult) -> None:
    st.session_state.results.insert(0, result)
