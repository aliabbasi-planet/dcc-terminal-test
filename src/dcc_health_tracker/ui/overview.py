"""Overview dashboard — KPIs, fix trends, profit tracking charts."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import queries
from .helpers import BLUE, GREEN, ORANGE, bar_chart


def render(conn) -> None:
    st.title("DCC Terminal Health Tracker — Overview")
    st.caption(
        "Track terminal fix events (broken → healthy transitions) and "
        "allocated profit from recovered DCC revenue."
    )

    summary = conn.query(queries.summary())
    if summary.empty:
        st.warning("No summary data. Run the pipeline first (see pipeline.py).")
        return

    row = summary.iloc[0]
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Terminals Fixed", f"{int(row.get('UNIQUE_TERMINALS_FIXED', 0)):,}")
        c2.metric("Total Episodes", f"{int(row.get('TOTAL_FIX_EPISODES', 0)):,}")
        c3.metric("Open Episodes", f"{int(row.get('OPEN_EPISODES', 0)):,}")
        c4.metric("Closed Episodes", f"{int(row.get('CLOSED_EPISODES', 0)):,}")

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(
            "Total Profit Recovered",
            f"${row.get('TOTAL_ALLOCATED_PROFIT', 0):,.2f}",
        )
        c2.metric("Avg Episode Days", f"{row.get('AVG_EPISODE_DAYS', 0):.1f}")
        c3.metric("Historical (Campaign)", f"{int(row.get('HISTORICAL_EPISODES', 0)):,}")
        c4.metric("Detected (Snapshot)", f"{int(row.get('DETECTED_EPISODES', 0)):,}")

    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        c1.metric("Baseline Date", str(row.get("BASELINE_DATE", "—"))[:10])
        c2.metric("Active Campaigns", f"{int(row.get('ACTIVE_CAMPAIGNS', 0)):,}")
        refreshed = str(row.get("REFRESHED_AT", "—"))[:19]
        c3.metric("Last Refreshed", refreshed)

    st.divider()
    st.subheader("Fix episodes — by flag")
    fix_flag = conn.query(queries.fix_by_flag())
    if not fix_flag.empty:
        c1, c2 = st.columns(2)
        with c1:
            with st.container(border=True):
                st.caption("Terminals fixed per flag")
                bar_chart(fix_flag, "FLAG_NAME", "TERMINALS", "Flag", "Terminals", GREEN)
        with c2:
            with st.container(border=True):
                st.caption("Profit allocated per flag")
                bar_chart(fix_flag, "FLAG_NAME", "TOTAL_PROFIT", "Flag", "Profit ($)", BLUE)

    st.divider()
    st.subheader("Geographic distribution")
    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            st.caption("Terminals fixed by country (top 20)")
            fix_country = conn.query(queries.fix_by_country())
            bar_chart(fix_country, "COUNTRY_NAME", "TERMINALS", "Country", "Terminals", GREEN)
    with c2:
        with st.container(border=True):
            st.caption("Profit by country (top 20)")
            bar_chart(fix_country, "COUNTRY_NAME", "TOTAL_PROFIT", "Country", "Profit ($)", BLUE)

    st.divider()
    st.subheader("Daily tracker")
    tracker = conn.query(queries.daily_tracker())
    if not tracker.empty:
        tracker["TRACK_DATE"] = pd.to_datetime(tracker["TRACK_DATE"]).dt.date
        c1, c2 = st.columns(2)
        with c1:
            with st.container(border=True):
                st.caption("Cumulative terminals fixed")
                tracker["DATE_STR"] = tracker["TRACK_DATE"].astype(str)
                bar_chart(
                    tracker, "DATE_STR", "CUMULATIVE_TERMINALS_FIXED",
                    "Date", "Cumulative fixed", GREEN,
                )
        with c2:
            with st.container(border=True):
                st.caption("Cumulative profit recovered")
                bar_chart(
                    tracker, "DATE_STR", "CUMULATIVE_PROFIT_RECOVERED",
                    "Date", "Cumulative profit ($)", ORANGE,
                )
