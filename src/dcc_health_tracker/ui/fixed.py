"""Fixed terminals page — filterable list of broken→healthy transitions with profit."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import queries
from .helpers import filter_bar

DISPLAY_COLS = [
    "TERMINAL_IDENTIFIER", "FLAG_NAME", "FIX_SOURCE", "FIX_DATE",
    "CAMPAIGN_ID", "CAMPAIGN_NAME",
    "BREAK_AGAIN_DATE", "IS_OPEN", "EPISODE_END_DATE", "EPISODE_DAYS",
    "BANK_MERCHANT_ID", "CUSTOMER_NAME", "COUNTRY_NAME", "REGION",
    "INDUSTRY_NAME", "ACQUIRER_NAME", "LOCATION_NAME",
    "FIRMWARE_VERSION", "MARKET_GROUP", "BRAND", "ALLOCATED_DCC_PROFIT",
]

COL_CONFIG = {
    "TERMINAL_IDENTIFIER": "Terminal ID",
    "FLAG_NAME": "Flag",
    "FIX_SOURCE": "Source",
    "FIX_DATE": "Fix date",
    "CAMPAIGN_ID": "Campaign",
    "CAMPAIGN_NAME": "Campaign name",
    "BREAK_AGAIN_DATE": "Broke again",
    "IS_OPEN": "Open?",
    "EPISODE_END_DATE": "End date",
    "EPISODE_DAYS": st.column_config.NumberColumn("Days", format="%d"),
    "BANK_MERCHANT_ID": "Merchant ID",
    "CUSTOMER_NAME": "Customer",
    "COUNTRY_NAME": "Country",
    "REGION": "Region",
    "INDUSTRY_NAME": "Industry",
    "ACQUIRER_NAME": "Acquirer",
    "LOCATION_NAME": "Location",
    "FIRMWARE_VERSION": "Firmware",
    "MARKET_GROUP": "Market Group",
    "BRAND": "Brand",
    "ALLOCATED_DCC_PROFIT": st.column_config.NumberColumn("Profit ($)", format="$%.2f"),
}


def render(conn) -> None:
    st.title("Fixed Terminals")
    st.caption(
        "Terminals that transitioned from broken to healthy. "
        "Includes historical campaign fixes and snapshot-detected fixes."
    )

    df = conn.query(queries.fix_episodes())
    if df.empty:
        st.info("No fix episodes found. Run the pipeline to detect transitions.")
        return

    df["FIX_DATE"] = pd.to_datetime(df["FIX_DATE"]).dt.date
    if "EPISODE_END_DATE" in df.columns:
        df["EPISODE_END_DATE"] = pd.to_datetime(df["EPISODE_END_DATE"]).dt.date
    if "BREAK_AGAIN_DATE" in df.columns:
        df["BREAK_AGAIN_DATE"] = pd.to_datetime(df["BREAK_AGAIN_DATE"]).dt.date

    filtered = filter_bar(df, "fix", date_col="FIX_DATE")

    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Rows", f"{len(filtered):,}")
        c2.metric("Terminals", f"{filtered['TERMINAL_IDENTIFIER'].nunique():,}")
        avg = f"{filtered['EPISODE_DAYS'].mean():.0f}" if not filtered.empty else "—"
        c3.metric("Avg episode (days)", avg)
        c4.metric("Still open", f"{int(filtered['IS_OPEN'].sum()):,}" if not filtered.empty else "0")
        profit = filtered["ALLOCATED_DCC_PROFIT"].sum() if "ALLOCATED_DCC_PROFIT" in filtered else 0
        c5.metric("Total profit", f"${profit:,.2f}")

    cols = [c for c in DISPLAY_COLS if c in filtered.columns]
    st.dataframe(
        filtered[cols].sort_values("FIX_DATE", ascending=False),
        hide_index=True, use_container_width=True, height=560,
        column_config=COL_CONFIG,
    )

    st.download_button(
        "Download filtered results as CSV",
        data=filtered[cols].to_csv(index=False).encode("utf-8"),
        file_name="dcc_fixed_terminals.csv",
        mime="text/csv",
        type="primary",
    )
