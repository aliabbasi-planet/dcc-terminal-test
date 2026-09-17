"""Shared UI helpers: filter bar and chart rendering."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

BLUE = "#1E6FFF"
GREEN = "#28B463"
ORANGE = "#F39C12"
RED = "#E74C3C"


def filter_bar(
    df: pd.DataFrame, key_prefix: str, date_col: str | None = "FIX_DATE"
) -> pd.DataFrame:
    if df.empty:
        return df
    with st.container(border=True):
        st.markdown("**Filters**")
        c1, c2, c3, c4 = st.columns(4)
        date_range = None
        with c1:
            if date_col and date_col in df.columns:
                date_range = st.date_input(
                    "Date range",
                    value=(df[date_col].min(), df[date_col].max()),
                    key=f"{key_prefix}_dates",
                )
            else:
                st.caption("(no date filter)")
        with c2:
            flags = _multi(df, "FLAG_NAME", "Flag", key_prefix)
        with c3:
            countries = _multi(df, "COUNTRY_NAME", "Country", key_prefix)
        with c4:
            regions = _multi(df, "REGION", "Region", key_prefix)
        c5, c6, c7, _ = st.columns(4)
        with c5:
            industries = _multi(df, "INDUSTRY_NAME", "Industry", key_prefix)
        with c6:
            acquirers = _multi(df, "ACQUIRER_NAME", "Acquirer", key_prefix)
        with c7:
            merchants = _multi(df, "BANK_MERCHANT_ID", "Merchant", key_prefix)

    out = df.copy()
    if date_col and date_range and isinstance(date_range, tuple) and len(date_range) == 2:
        out = out[(out[date_col] >= date_range[0]) & (out[date_col] <= date_range[1])]
    for col, vals in {
        "FLAG_NAME": flags, "COUNTRY_NAME": countries, "REGION": regions,
        "INDUSTRY_NAME": industries, "ACQUIRER_NAME": acquirers,
        "BANK_MERCHANT_ID": merchants,
    }.items():
        if vals and col in out.columns:
            out = out[out[col].isin(vals)]
    return out


def _multi(df, col, label, prefix):
    if col not in df.columns:
        return []
    return st.multiselect(label, sorted(df[col].dropna().unique()), key=f"{prefix}_{col}")


def bar_chart(
    df: pd.DataFrame, x: str, y: str, x_title: str, y_title: str,
    color: str = BLUE, height: int = 320,
) -> None:
    if df.empty:
        st.info("No data for the selected filters.")
        return
    chart = (
        alt.Chart(df)
        .mark_bar(color=color)
        .encode(
            x=alt.X(f"{x}:N", sort="-y", title=x_title, axis=alt.Axis(labelAngle=-30)),
            y=alt.Y(f"{y}:Q", title=y_title),
            tooltip=[x, y],
        )
        .properties(height=height)
    )
    st.altair_chart(chart, use_container_width=True)
