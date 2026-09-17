"""Broken terminals page — filterable list of healthy→broken transitions."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import queries
from .helpers import filter_bar

DISPLAY_COLS = [
    "TERMINAL_IDENTIFIER", "FLAG_NAME", "BREAK_DATE",
    "FIXED_AGAIN_DATE", "IS_OPEN", "EPISODE_END_DATE", "EPISODE_DAYS",
    "BANK_MERCHANT_ID", "CUSTOMER_NAME", "COUNTRY_NAME", "REGION",
    "INDUSTRY_NAME", "ACQUIRER_NAME", "LOCATION_NAME", "FIRMWARE_VERSION",
]

COL_CONFIG = {
    "TERMINAL_IDENTIFIER": "Terminal ID",
    "FLAG_NAME": "Flag",
    "BREAK_DATE": "Broke on",
    "FIXED_AGAIN_DATE": "Fixed again",
    "IS_OPEN": "Still broken?",
    "EPISODE_END_DATE": "End date",
    "EPISODE_DAYS": st.column_config.NumberColumn("Days broken", format="%d"),
    "BANK_MERCHANT_ID": "Merchant ID",
    "CUSTOMER_NAME": "Customer",
    "COUNTRY_NAME": "Country",
    "REGION": "Region",
    "INDUSTRY_NAME": "Industry",
    "ACQUIRER_NAME": "Acquirer",
    "LOCATION_NAME": "Location",
    "FIRMWARE_VERSION": "Firmware",
}


def render(conn) -> None:
    st.title("Broken Terminals")
    st.caption("Terminals that transitioned from healthy to broken. Filter, sort and download.")

    df = conn.query(queries.break_episodes())
    if df.empty:
        st.info("No break episodes found. Run the pipeline to detect transitions.")
        return

    df["BREAK_DATE"] = pd.to_datetime(df["BREAK_DATE"]).dt.date
    if "EPISODE_END_DATE" in df.columns:
        df["EPISODE_END_DATE"] = pd.to_datetime(df["EPISODE_END_DATE"]).dt.date
    if "FIXED_AGAIN_DATE" in df.columns:
        df["FIXED_AGAIN_DATE"] = pd.to_datetime(df["FIXED_AGAIN_DATE"]).dt.date

    filtered = filter_bar(df, "brk", date_col="BREAK_DATE")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows", f"{len(filtered):,}")
    c2.metric("Terminals", f"{filtered['TERMINAL_IDENTIFIER'].nunique():,}")
    avg = f"{filtered['EPISODE_DAYS'].mean():.0f}" if not filtered.empty else "—"
    c3.metric("Avg duration (days)", avg)
    c4.metric("Still broken", f"{int(filtered['IS_OPEN'].sum()):,}" if not filtered.empty else "0")

    cols = [c for c in DISPLAY_COLS if c in filtered.columns]
    st.dataframe(
        filtered[cols].sort_values("BREAK_DATE", ascending=False),
        hide_index=True, use_container_width=True, height=560,
        column_config=COL_CONFIG,
    )

    st.download_button(
        "Download filtered results as CSV",
        data=filtered[cols].to_csv(index=False).encode("utf-8"),
        file_name="dcc_broken_terminals.csv",
        mime="text/csv",
        type="primary",
    )
