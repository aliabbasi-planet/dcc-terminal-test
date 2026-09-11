"""Sidebar: environment connection controls."""

from __future__ import annotations

import streamlit as st

from ..config import ENVIRONMENTS
from ..database import DatabaseConnection
from ..state import DEFAULTS, reset_state, start_new_session


def render_connection() -> None:
    st.sidebar.title("🔌 Connection")

    env_name = st.sidebar.radio(
        "Environment",
        list(ENVIRONMENTS),
        horizontal=True,
        key="active_env",
        disabled=st.session_state.db_connected,
        help="Each environment keeps its own server, database and credentials.",
    )
    environment = ENVIRONMENTS[env_name]
    st.sidebar.caption(f"{environment.badge} {environment.note}")

    server = st.sidebar.text_input(
        "SQL Server", value=environment.server, key=f"db_server_{env_name}"
    )
    database = st.sidebar.text_input(
        "Database", value=environment.database, key=f"db_database_{env_name}"
    )
    username = st.sidebar.text_input(f"{env_name} username", key=f"db_user_{env_name}")
    password = st.sidebar.text_input(
        f"{env_name} password", type="password", key=f"db_pass_{env_name}"
    )

    connect_col, disconnect_col = st.sidebar.columns(2)

    if connect_col.button("Connect", use_container_width=True, type="primary"):
        candidate = DatabaseConnection(server, database, username, password)
        with st.spinner(f"Connecting to {env_name}…"):
            ok, message = candidate.connect()
        if ok:
            if st.session_state.connection is not None:
                st.session_state.connection.close()
            start_new_session()
            st.session_state.connection = candidate
            st.session_state.db_connected = True
            st.session_state.driver_used = candidate.driver
            st.session_state.connected_env = env_name
            st.session_state.connected_login = username
            st.sidebar.success(f"{env_name} · {message}")
        else:
            st.session_state.db_connected = False
            st.sidebar.error(message)

    if disconnect_col.button("Disconnect", use_container_width=True):
        if st.session_state.connection is not None:
            st.session_state.connection.close()
        reset_state()
        st.rerun()

    if st.session_state.db_connected:
        st.sidebar.success(
            f"✅ {st.session_state.connected_env} · {st.session_state.connected_login} "
            f"· {st.session_state.driver_used}"
        )
    else:
        st.sidebar.error("❌ Not connected")


__all__ = ["render_connection", "DEFAULTS"]
