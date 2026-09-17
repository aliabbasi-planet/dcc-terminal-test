"""Overview dashboard — KPIs, fix vs break trends, charts by flag/country."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import queries
from .helpers import BLUE, GREEN, ORANGE, RED, bar_chart


def render(conn) -> None:
    st.title("DCC Terminal Health Tracker — Overview")
    st.caption(
        "Day-over-day transition detection: fixed (broken to healthy) "
        "and broken (healthy to broken) terminals."
    )

    summary = conn.query(queries.summary())
    if summary.empty:
        st.warning("No summary data. Run the pipeline first (see pipeline.py).")
        return

    row = summary.iloc[0]
    with st.container(horizontal=True):
        st.metric("Terminals Fixed", f"{int(row.get('UNIQUE_TERMINALS_FIXED', 0)):,}",
                  f"{int(row.get('TOTAL_FIX_EPISODES', 0)):,} episodes", border=True)
        st.metric("Terminals Broken", f"{int(row.get('UNIQUE_TERMINALS_BROKEN', 0)):,}",
                  f"{int(row.get('TOTAL_BREAK_EPISODES', 0)):,} episodes", border=True)
        st.metric("Open Fix Episodes", f"{int(row.get('OPEN_FIX_EPISODES', 0)):,}", border=True)
        st.metric("Open Break Episodes", f"{int(row.get('OPEN_BREAK_EPISODES', 0)):,}", border=True)

    with st.container(horizontal=True):
        st.metric("Avg Fix Duration", f"{row.get('AVG_FIX_EPISODE_DAYS', 0):.0f} days", border=True)
        st.metric(
            "Avg Break Duration",
            f"{row.get('AVG_BREAK_EPISODE_DAYS', 0):.0f} days",
            border=True,
        )
        st.metric("Earliest Snapshot", str(row.get("EARLIEST_SNAPSHOT", "—"))[:10], border=True)
        st.metric("Latest Snapshot", str(row.get("LATEST_SNAPSHOT", "—"))[:10], border=True)

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
                st.caption("Avg fix episode duration (days)")
                bar_chart(fix_flag, "FLAG_NAME", "AVG_DAYS", "Flag", "Avg days", BLUE)

    st.subheader("Break episodes — by flag")
    break_flag = conn.query(queries.break_by_flag())
    if not break_flag.empty:
        c1, c2 = st.columns(2)
        with c1:
            with st.container(border=True):
                st.caption("Terminals broken per flag")
                bar_chart(break_flag, "FLAG_NAME", "TERMINALS", "Flag", "Terminals", RED)
        with c2:
            with st.container(border=True):
                st.caption("Avg break episode duration (days)")
                bar_chart(break_flag, "FLAG_NAME", "AVG_DAYS", "Flag", "Avg days", ORANGE)

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
            st.caption("Terminals broken by country (top 20)")
            break_country = conn.query(queries.break_by_country())
            bar_chart(break_country, "COUNTRY_NAME", "TERMINALS", "Country", "Terminals", RED)

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
                    tracker, "DATE_STR", "CUM_TERMINALS_FIXED",
                    "Date", "Cumulative fixed", GREEN,
                )
        with c2:
            with st.container(border=True):
                st.caption("Cumulative terminals broken")
                bar_chart(
                    tracker, "DATE_STR", "CUM_TERMINALS_BROKEN",
                    "Date", "Cumulative broken", RED,
                )
