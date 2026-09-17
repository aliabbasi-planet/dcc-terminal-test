"""Broken terminals page — currently broken terminals from latest snapshot."""

from __future__ import annotations

import streamlit as st

from .. import queries
from .helpers import filter_bar

DISPLAY_COLS = [
    "TERMINAL_IDENTIFIER", "COUNTRY_NAME", "REGION", "INDUSTRY_NAME",
    "BANK_MERCHANT_ID", "CUSTOMER_NAME", "ACQUIRER_NAME", "LOCATION_NAME",
    "FIRMWARE_VERSION",
    "LOCATION_DCCENABLED_CHECK_C", "HANDLER_DCCENABLE_CHECK_O",
    "HANDLER_DCCENABLECOMPLETION_CHECK_O", "HANDLER_DCCENABLEAUTH_CHECK_O",
    "HANDLER_DCCENABLENFC_CHECK_O", "HANDLER_DCCENABLENFCSINGLETAP_CHECK_O",
    "DCCXPRESSCO_CHECK_O", "DCCXPRESSCODT_CHECK_O", "DCCMERCHANT_NO_CHECK_O",
]

COL_CONFIG = {
    "TERMINAL_IDENTIFIER": "Terminal ID",
    "COUNTRY_NAME": "Country",
    "REGION": "Region",
    "INDUSTRY_NAME": "Industry",
    "BANK_MERCHANT_ID": "Merchant ID",
    "CUSTOMER_NAME": "Customer",
    "ACQUIRER_NAME": "Acquirer",
    "LOCATION_NAME": "Location",
    "FIRMWARE_VERSION": "Firmware",
    "LOCATION_DCCENABLED_CHECK_C": st.column_config.CheckboxColumn("Loc DCC"),
    "HANDLER_DCCENABLE_CHECK_O": st.column_config.CheckboxColumn("Enable"),
    "HANDLER_DCCENABLECOMPLETION_CHECK_O": st.column_config.CheckboxColumn("Completion"),
    "HANDLER_DCCENABLEAUTH_CHECK_O": st.column_config.CheckboxColumn("Auth"),
    "HANDLER_DCCENABLENFC_CHECK_O": st.column_config.CheckboxColumn("NFC"),
    "HANDLER_DCCENABLENFCSINGLETAP_CHECK_O": st.column_config.CheckboxColumn("NFC Tap"),
    "DCCXPRESSCO_CHECK_O": st.column_config.CheckboxColumn("Xpress CO"),
    "DCCXPRESSCODT_CHECK_O": st.column_config.CheckboxColumn("Xpress DT"),
    "DCCMERCHANT_NO_CHECK_O": st.column_config.CheckboxColumn("Merchant"),
}


def render(conn) -> None:
    st.title("Currently Broken Terminals")
    st.caption(
        "Terminals from the latest snapshot where IS_DCC_BROKEN = TRUE. "
        "Use this to identify terminals that need attention."
    )

    df = conn.query(queries.current_broken_terminals())
    if df.empty:
        st.success("No broken terminals found in the latest snapshot.")
        return

    filtered = filter_bar(df, "brk", date_col=None)

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Broken", f"{len(filtered):,}")
        c2.metric("Countries", f"{filtered['COUNTRY_NAME'].nunique():,}")
        c3.metric("Merchants", f"{filtered['BANK_MERCHANT_ID'].nunique():,}")
        c4.metric("Acquirers", f"{filtered['ACQUIRER_NAME'].nunique():,}")

    # Show breakdown by flag
    st.subheader("Breakdown by flag (1 = broken)")
    flag_cols = [c for c in DISPLAY_COLS if c.endswith("_CHECK_O") or c.endswith("_CHECK_C")]
    if filtered.empty:
        st.info("No data to display.")
    else:
        flag_counts = {col: int(filtered[col].sum()) for col in flag_cols if col in filtered}
        cols = st.columns(len(flag_counts))
        for i, (flag, count) in enumerate(sorted(flag_counts.items(), key=lambda x: -x[1])):
            short_name = flag.replace("_CHECK_O", "").replace("_CHECK_C", "")
            short_name = short_name.replace("HANDLER_", "").replace("LOCATION_", "")
            cols[i % len(cols)].metric(short_name, f"{count:,}")

    st.divider()
    cols = [c for c in DISPLAY_COLS if c in filtered.columns]
    st.dataframe(
        filtered[cols].sort_values("TERMINAL_IDENTIFIER"),
        hide_index=True, use_container_width=True, height=500,
        column_config=COL_CONFIG,
    )

    st.download_button(
        "Download broken terminals as CSV",
        data=filtered[cols].to_csv(index=False).encode("utf-8"),
        file_name="dcc_broken_terminals.csv",
        mime="text/csv",
        type="primary",
    )
