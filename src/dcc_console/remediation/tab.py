"""Streamlit tab: DCC Remediation (identify / analyse broken terminals).

Read-only phase: connect to Snowflake (SSO), point at your own DEV_CORE schema,
initialise it if needed, see how fresh the daily snapshot is, optionally trigger a
refresh, review KPIs and breakdowns, and filter the worklist of broken terminals.

Live one-by-one fixing is a later phase and is intentionally not wired here — the
fix path needs the SQL-Server connection plus a confirmed role allowlist, tracked
separately. Note that fixes are logged per-schema, so the re-fix guard is per user
until a shared APP_FIX_LOG is adopted (see the caption at the foot of the tab).
"""

from __future__ import annotations

import os

import streamlit as st

from . import MAINTENANCE_SOURCE, RemediationObjects, ddl
from . import analytics as an
from . import snapshot as snap
from . import worklist as wl
from .mapping import DIMENSION_COLUMNS, FIXABLE_CHECK_COLUMNS, TRACKED_CHECK_COLUMNS
from .sf_connection import SnowflakeConnection, SnowflakeSettings, connector_available

_CONN_KEY = "rem_sf_conn"
_FILTER_DIMS = ("COUNTRY_NAME", "REGION", "TERMINAL_BRAND_NAME", "ACQUIRER_NAME")


def _connection() -> SnowflakeConnection | None:
    conn = st.session_state.get(_CONN_KEY)
    return conn if conn is not None and conn.connection is not None else None


def _objects(conn: SnowflakeConnection) -> RemediationObjects:
    """Build the FQN set for this user's chosen database/schema."""
    return RemediationObjects(conn.settings.database, conn.settings.schema)


def _run(conn: SnowflakeConnection, sql: str, params: list | None = None):
    """Execute a read and return ``(dataframe, error_message)``."""
    try:
        return conn.query(sql, params), None
    except Exception as exc:  # surface DB errors in the UI, never crash the tab
        return None, str(exc)


def _render_connection_panel() -> None:
    connected = _connection() is not None
    label = "Snowflake connection — connected" if connected else "Snowflake connection"
    with st.expander(label, expanded=not connected):
        if not connector_available():
            st.error(
                "`snowflake-connector-python` is not installed in this environment. "
                "Add it to requirements and reload before using this tab."
            )
            return
        st.caption(
            "Sign in with your own Snowflake identity (SSO / external browser). "
            "Point 'Database' at your own DEV_CORE; your role scopes what you can do."
        )
        c1, c2 = st.columns(2)
        account = c1.text_input("Account", value=os.getenv("SNOWFLAKE_ACCOUNT", ""))
        user = c2.text_input("User", value=os.getenv("SNOWFLAKE_USER", ""))
        role = c1.text_input("Role", value=os.getenv("SNOWFLAKE_ROLE", "DATA_SCIENTIST"))
        warehouse = c2.text_input(
            "Warehouse", value=os.getenv("SNOWFLAKE_WAREHOUSE", "DATA_SCIENCE")
        )
        database = c1.text_input(
            "Database (your DEV_CORE)", value=os.getenv("SNOWFLAKE_DATABASE", "DEV_CORE_AAB")
        )
        schema = c2.text_input(
            "Schema", value=os.getenv("SNOWFLAKE_SCHEMA", "DCC_REMEDIATION")
        )
        if st.button("Connect to Snowflake", type="primary", use_container_width=True):
            settings = SnowflakeSettings(
                account=account, user=user, role=role, warehouse=warehouse,
                database=database, schema=schema,
            )
            conn = SnowflakeConnection(settings)
            ok, message = conn.connect()
            if ok:
                st.session_state[_CONN_KEY] = conn
                st.success(message)
                st.rerun()
            else:
                st.error(message)
        if connected and st.button("Disconnect", use_container_width=True):
            _connection().close()
            st.session_state[_CONN_KEY] = None
            st.rerun()


def _render_init(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    st.caption(
        f"Target schema `{objs.schema_fqn}`. Initialise creates the tables/views and "
        "seeds the flag reference if they are missing (idempotent — safe to re-run)."
    )
    df, error = _run(conn, ddl.build_objects_present_query(objs))
    if error is None and df is not None and not df.empty:
        present = int(df.iloc[0].get("N", 0))
        if present < len(ddl.TABLE_NAMES):
            st.warning(
                f"Schema not fully initialised ({present}/{len(ddl.TABLE_NAMES)} core "
                "tables present). Click **Initialise** below."
            )
    if st.button("Initialise / verify my schema", use_container_width=True):
        with st.spinner(f"Creating objects in {objs.schema_fqn}..."):
            try:
                for statement in ddl.build_create_statements(objs):
                    conn.execute(statement)
                conn.execute(ddl.build_seed_merge(objs))
                st.success(f"Schema {objs.schema_fqn} is ready.")
            except Exception as exc:
                st.error(f"Initialise failed: {exc}")
        st.rerun()


def _render_kpis(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    df, error = _run(conn, an.build_kpis_query(objs))
    if error:
        st.info("KPIs unavailable — initialise the schema and refresh the snapshot.")
        return
    if df is None or df.empty:
        st.info("No snapshot loaded yet. Use **Refresh snapshot** below.")
        return
    row = df.iloc[0]
    cols = st.columns(4)
    cols[0].metric("Actionable broken", f"{int(row.get('BROKEN_ACTIONABLE', 0)):,}")
    cols[1].metric("Awaiting refresh", f"{int(row.get('AWAITING_REFRESH', 0)):,}")
    cols[2].metric("Live fixes logged", f"{int(row.get('TOTAL_LIVE_FIXES', 0)):,}")
    cols[3].metric("Latest snapshot", str(row.get("LATEST_SNAPSHOT_DATE") or "—"))


def _render_refresh(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    st.caption(
        f"The snapshot is loaded from `{MAINTENANCE_SOURCE}` (refreshed daily ~06:00). "
        "Refreshing re-reads it now and upserts today's broken-terminal rows."
    )
    if st.button("Refresh snapshot now", use_container_width=True):
        with st.spinner("Refreshing broken-terminal snapshot..."):
            try:
                conn.execute(snap.build_refresh_merge(objs))
                st.success("Snapshot refreshed.")
            except Exception as exc:
                st.error(f"Refresh failed: {exc}")
        st.rerun()


def _render_analytics(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    st.subheader("Breakdown")
    flags_df, error = _run(conn, an.build_flag_totals_query(objs))
    if error:
        st.error(f"Could not read flag totals: {error}")
    elif flags_df is not None and not flags_df.empty:
        melted = (
            flags_df.iloc[0]
            .rename_axis("check")
            .reset_index(name="terminals")
            .sort_values("terminals", ascending=False)
        )
        melted = melted[melted["terminals"] > 0].set_index("check")
        st.caption("Actionable broken terminals per check")
        st.bar_chart(melted)

    dimension = st.selectbox("Group by dimension", DIMENSION_COLUMNS, index=0)
    dim_df, dim_error = _run(conn, an.build_breakdown_query(dimension, objs, top_n=20))
    if dim_error:
        st.error(f"Could not read breakdown: {dim_error}")
    elif dim_df is not None and not dim_df.empty:
        st.bar_chart(dim_df.set_index("CATEGORY"))


def _distinct_values(conn: SnowflakeConnection, column: str, objs: RemediationObjects) -> list[str]:
    df, error = _run(conn, wl.build_distinct_values_query(column, objs))
    if error or df is None or df.empty:
        return []
    return [str(v) for v in df["VALUE"].tolist()]


def _render_worklist(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    st.subheader("Worklist")
    top = st.columns(3)
    state = top[0].selectbox("State", ["ACTIONABLE", "AWAITING_REFRESH", "(all)"], index=0)
    fixable_only = top[1].checkbox("Fixable checks only", value=True)
    limit = int(top[2].number_input("Max rows", min_value=50, max_value=5000, value=500, step=50))

    dimensions: dict[str, tuple[str, ...]] = {}
    dim_cols = st.columns(len(_FILTER_DIMS))
    for idx, column in enumerate(_FILTER_DIMS):
        chosen = dim_cols[idx].multiselect(
            column.replace("_", " ").title(),
            _distinct_values(conn, column, objs),
            key=f"rem_dim_{column}",
        )
        if chosen:
            dimensions[column] = tuple(chosen)

    check_pool = FIXABLE_CHECK_COLUMNS if fixable_only else TRACKED_CHECK_COLUMNS
    checks = st.multiselect("Broken on check(s)", check_pool, key="rem_checks")

    filters = wl.WorklistFilters(
        remediation_state=None if state == "(all)" else state,
        dimensions=dimensions,
        check_columns=tuple(checks),
        fixable_only=fixable_only,
        limit=limit,
    )
    try:
        sql, params = wl.build_worklist_query(filters, objs)
    except ValueError as exc:
        st.error(str(exc))
        return

    df, error = _run(conn, sql, params)
    if error:
        st.error(f"Could not read worklist: {error}")
        return
    if df is None or df.empty:
        st.info("No terminals match the current filters.")
        return
    st.caption(f"{len(df):,} terminal(s) shown")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "Download worklist (CSV)",
        df.to_csv(index=False).encode("utf-8"),
        file_name="dcc_remediation_worklist.csv",
        mime="text/csv",
    )


def render_remediation() -> None:
    """Entry point wired into the console's tab strip."""
    st.subheader("DCC Remediation")
    st.caption(
        "Identify and analyse broken terminals from the Snowflake daily snapshot. "
        "Live one-by-one fixing is the next phase and is not enabled here yet."
    )
    _render_connection_panel()

    conn = _connection()
    if conn is None:
        st.info("Connect to Snowflake above to load the broken-terminal snapshot.")
        return

    try:
        objs = _objects(conn)
    except ValueError as exc:
        st.error(f"Invalid database/schema: {exc}")
        return

    with st.container(border=True):
        _render_init(conn, objs)
    with st.container(border=True):
        _render_kpis(conn, objs)
        _render_refresh(conn, objs)
    st.divider()
    _render_analytics(conn, objs)
    st.divider()
    _render_worklist(conn, objs)
    st.divider()
    st.info(
        "Next phase: select a terminal, review its live config, apply the mapped fix "
        "through the stored procedure, verify, and log it to APP_FIX_LOG — one at a time."
    )
    st.caption(
        f"Fixes are logged in your own `{objs.schema_fqn}.APP_FIX_LOG`, so the re-fix "
        "guard is per-schema. A shared fix log is recommended before multiple people "
        "run live fixes against the same estate."
    )
