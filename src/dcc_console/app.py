"""Streamlit entry point for the DCC Enablement Configuration test console."""

from __future__ import annotations

import logging

import streamlit as st

from dcc_console.config import ENVIRONMENTS, PROCEDURE
from dcc_console.state import init_state
from dcc_console.ui import (
    render_campaign,
    render_connection,
    render_mode,
    render_readiness,
    render_recovery_banner,
    render_results,
    render_terminals,
    render_test,
)

logging.basicConfig(level=logging.INFO)


def main() -> None:
    st.set_page_config(
        page_title="DCC Test Console",
        page_icon="🧪",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    init_state()

    st.title("🧪 DCC Enablement Configuration — Test Console")
    render_recovery_banner()
    render_connection()

    if not st.session_state.db_connected:
        st.caption(f"Single-page harness for `{PROCEDURE}`")
        st.info(
            "Pick DEV or UAT in the sidebar, enter that environment's credentials, "
            "then connect."
        )
        return

    conn = st.session_state.connection
    environment = ENVIRONMENTS.get(st.session_state.connected_env)
    badge = environment.badge if environment else ""
    st.caption(
        f"{badge} **{st.session_state.connected_env}** · `{conn.server}` / `{conn.database}` "
        f"· login `{st.session_state.connected_login}` · harness for `{PROCEDURE}`"
    )

    ready = render_readiness()
    st.divider()
    simulation, confirmed = render_mode()
    st.divider()
    render_terminals()
    st.divider()
    test_tab, campaign_tab = st.tabs(["Single test", "Campaigns"])
    with test_tab:
        render_test(simulation, confirmed and ready)
        st.divider()
        render_results()
    with campaign_tab:
        render_campaign(simulation, confirmed and ready)


if __name__ == "__main__":
    main()
