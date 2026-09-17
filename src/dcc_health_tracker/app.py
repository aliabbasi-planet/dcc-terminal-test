"""Streamlit entry point for the DCC Terminal Health Tracker."""

from __future__ import annotations

import os

import streamlit as st

from dcc_health_tracker.ui import broken, fixed, overview


def main() -> None:
    st.set_page_config(
        page_title="DCC Health Tracker",
        page_icon=":material/monitor_heart:",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    conn = st.connection("snowflake", ttl=os.getenv("SNOWFLAKE_CONNECTION_TTL"))

    pages = {
        "Overview": st.Page(
            lambda: overview.render(conn), title="Overview", icon=":material/dashboard:"
        ),
        "Fixed Terminals": st.Page(
            lambda: fixed.render(conn), title="Fixed Terminals", icon=":material/check_circle:"
        ),
        "Broken Terminals": st.Page(
            lambda: broken.render(conn), title="Broken Terminals", icon=":material/error:"
        ),
    }
    nav = st.navigation(list(pages.values()))
    nav.run()


if __name__ == "__main__":
    main()
