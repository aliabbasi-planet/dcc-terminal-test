"""Streamlit tab: DCC Remediation (identify, analyse and fix broken terminals).

Connect to Snowflake (SSO), point at your own DEV_CORE schema (initialised or
upgraded in one click), see how fresh the daily snapshot is, review KPIs and
breakdowns, and filter the worklist of broken terminals. Selecting a worklist row
opens the one-by-one fixer (:mod:`.fix_panel`): pre-check → dry run → apply live →
verify → logged, one check at a time.

Live writes run on DEV/UAT for any operator in the shared ``FIX_OPERATORS`` table and,
behind a stronger gate (a PROD-approved ``CAN_PROD`` operator plus a typed change/CAB
reference), on PROD. Every attempt is logged to the team's shared ``APP_FIX_LOG``; a
verified PROD fix is also recorded in ``DCC_FIX_REGISTRY`` so it survives the daily
refresh, and a confirmed rollback removes it.
"""

from __future__ import annotations

import os

import pandas as pd
import plotly.express as px
import streamlit as st

from . import (
    DEFAULT_SHARED_SCHEMA,
    MAINTENANCE_SOURCE,
    RemediationObjects,
    agent_panel,
    batch_panel,
    campaign_panel,
    ddl,
    fix_panel,
    fixer,
    parse_schema_fqn,
    writes,
)
from . import analytics as an
from . import reconcile as rec
from . import snapshot as snap
from . import worklist as wl
from .mapping import DIMENSION_COLUMNS, FIXABLE_CHECK_COLUMNS, TRACKED_CHECK_COLUMNS
from .sf_connection import SnowflakeConnection, SnowflakeSettings, connector_available

_CONN_KEY = "rem_sf_conn"
_FLASH_KEY = "rem_flash"
_GRID_LAST_KEY = "rem_grid_last"
_FILTER_DIMS = ("COUNTRY_NAME", "REGION", "TERMINAL_BRAND_NAME", "ACQUIRER_NAME")
# Columns shown in the selectable grid (the CSV download keeps every column).
_GRID_COLUMNS = (
    "TERMINAL_IDENTIFIER",
    "BROKEN_CHECKS",
    "REMEDIATION_STATE",
    "MERCHANT_NAME",
    "LOCATION_NAME",
    "COUNTRY_NAME",
    "ACQUIRER_NAME",
    "TERMINAL_BRAND_NAME",
    "TERMINAL_MODEL_NAME",
    "INSTANCE_IDENTIFIER",
    "LOCATION_NO",
)
# Every widget below carries an explicit ``rem_*`` key: Streamlit derives unkeyed widget
# IDs from type + label + params, so e.g. an unkeyed "Disconnect" button here collides
# with the sidebar's SQL-Server "Disconnect" button (StreamlitDuplicateElementId).


def _flash(level: str, message: str) -> None:
    """Queue a message for the next run — ``st.rerun()`` discards this run's output."""
    st.session_state[_FLASH_KEY] = (level, message)


def _show_flash() -> None:
    pending = st.session_state.pop(_FLASH_KEY, None)
    if pending:
        level, message = pending
        getattr(st, level)(message)


def _connection() -> SnowflakeConnection | None:
    conn = st.session_state.get(_CONN_KEY)
    return conn if conn is not None and conn.connection is not None else None


def _objects(conn: SnowflakeConnection) -> RemediationObjects:
    """Build the FQN set for this user's own schema plus the team's shared fix log."""
    shared_database, shared_schema = parse_schema_fqn(
        conn.settings.shared_schema or f"{conn.settings.database}.{conn.settings.schema}"
    )
    return RemediationObjects(
        conn.settings.database, conn.settings.schema, shared_database, shared_schema
    )


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
        account = c1.text_input(
            "Account", value=os.getenv("SNOWFLAKE_ACCOUNT", ""), key="rem_sf_account"
        )
        user = c2.text_input("User", value=os.getenv("SNOWFLAKE_USER", ""), key="rem_sf_user")
        role = c1.text_input(
            "Role", value=os.getenv("SNOWFLAKE_ROLE", "DATA_SCIENTIST"), key="rem_sf_role"
        )
        warehouse = c2.text_input(
            "Warehouse",
            value=os.getenv("SNOWFLAKE_WAREHOUSE", "DATA_SCIENCE"),
            key="rem_sf_warehouse",
        )
        database = c1.text_input(
            "Database (your DEV_CORE)",
            value=os.getenv("SNOWFLAKE_DATABASE", "DEV_CORE_AAB"),
            key="rem_sf_database",
        )
        schema = c2.text_input(
            "Schema", value=os.getenv("SNOWFLAKE_SCHEMA", "DCC_REMEDIATION"), key="rem_sf_schema"
        )
        shared = st.text_input(
            "Shared fix log (DATABASE.SCHEMA)",
            value=os.getenv("DCC_SHARED_SCHEMA", DEFAULT_SHARED_SCHEMA),
            key="rem_sf_shared",
            help="One fix log and operator list for the whole team, so nobody re-fixes a "
            "terminal someone else already fixed.",
        )
        if st.button(
            "Connect to Snowflake", type="primary", use_container_width=True, key="rem_sf_connect"
        ):
            settings = SnowflakeSettings(
                account=account,
                user=user,
                role=role,
                warehouse=warehouse,
                database=database,
                schema=schema,
                shared_schema=shared,
            )
            conn = SnowflakeConnection(settings)
            ok, message = conn.connect()
            if ok:
                previous = _connection()
                if previous is not None:  # reconnecting: don't leak the old session
                    previous.close()
                st.session_state[_CONN_KEY] = conn
                _flash("success", message)
                st.rerun()
            else:
                st.error(message)
        if connected and st.button("Disconnect", use_container_width=True, key="rem_sf_disconnect"):
            _connection().close()
            st.session_state[_CONN_KEY] = None
            st.rerun()


def _render_init(conn: SnowflakeConnection, objs: RemediationObjects) -> bool:
    """Render the initialise panel; return True once every object exists and is current."""
    shared_note = (
        f" The fix log and operator list are shared in `{objs.shared_fqn}`."
        if objs.uses_shared_log
        else ""
    )
    st.caption(
        f"Target schema `{objs.schema_fqn}`.{shared_note} Initialise creates or upgrades the "
        "tables/views and seeds the flag reference (idempotent — safe to re-run)."
    )
    df, error = _run(conn, ddl.build_objects_present_query(objs))
    status = ddl.schema_status({} if error or df is None or df.empty else df.iloc[0].to_dict())
    if error:
        st.error(f"Could not check `{objs.schema_fqn}`: {error}")
    elif not status.ready:
        st.warning(
            f"Schema needs initialising or an upgrade — missing: {', '.join(status.missing)}. "
            "Click **Initialise** below."
        )
    if st.button("Initialise / verify my schema", use_container_width=True, key="rem_init"):
        # Teammates only touch the shared schema when something there is missing; the
        # owner (shared == own schema) always re-applies it, which is idempotent.
        include_shared = not objs.uses_shared_log or not status.shared_ready
        with st.spinner(f"Creating objects in {objs.schema_fqn}..."):
            try:
                for statement in ddl.build_initialise_statements(objs, include_shared):
                    conn.execute(statement)
                conn.execute(ddl.build_seed_merge(objs))
                _flash("success", f"Schema {objs.schema_fqn} is ready.")
            except Exception as exc:
                hint = (
                    f" If this is a permissions error on `{objs.shared_fqn}`, ask its owner "
                    "to initialise it or grant you access."
                    if include_shared and objs.uses_shared_log
                    else ""
                )
                _flash("error", f"Initialise failed: {exc}{hint}")
        st.rerun()
    return error is None and status.ready


def _render_kpis(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    df, error = _run(conn, an.build_kpis_query(objs))
    if error:
        st.info("KPIs unavailable — initialise the schema and refresh the snapshot.")
        return
    if df is None or df.empty:
        st.info("No snapshot loaded yet. Use **Refresh snapshot** below.")
        return
    row = df.iloc[0]

    def count(column: str) -> str:
        value = row.get(column)
        return f"{int(value or 0):,}"

    cols = st.columns(7)
    cols[0].metric("Actionable broken", count("BROKEN_ACTIONABLE"))
    cols[1].metric("Awaiting refresh", count("AWAITING_REFRESH"))
    cols[2].metric("Live fixes · PROD", count("TOTAL_LIVE_FIXES"))
    cols[3].metric("Registered · PROD", count("REGISTERED_FIXES"))
    cols[4].metric("Confirmed · PROD", count("CONFIRMED_FIXES"))
    cols[5].metric("Rehearsals · UAT/DEV", count("REHEARSAL_FIXES"))
    cols[6].metric("Latest snapshot", str(row.get("LATEST_SNAPSHOT_DATE") or "—"))


def _render_refresh(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    st.caption(
        f"The snapshot is loaded from `{MAINTENANCE_SOURCE}` (refreshed daily ~06:00). "
        "Refreshing re-reads it now and upserts today's broken-terminal rows."
    )
    if st.button("Refresh snapshot now", use_container_width=True, key="rem_refresh"):
        with st.spinner("Refreshing broken-terminal snapshot..."):
            try:
                conn.execute(snap.build_refresh_merge(objs))
                _flash("success", "Snapshot refreshed.")
            except Exception as exc:
                _flash("error", f"Refresh failed: {exc}")
        st.rerun()


def _render_reconcile(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    """Confirm registered PROD fixes against Cortex and hand confirmed ones to the profit app."""
    st.caption(
        "Confirm fixes against the Cortex source: when a registered PROD fix shows healthy there "
        "(and the source reloaded after the fix), it is handed off to the profit table "
        f"`{objs.confirmed_table}` and kept as CONFIRMED. Runs daily; run it now too."
    )
    df, error = _run(conn, rec.build_pending_summary_query(objs))
    if error:
        st.info("Confirmation summary unavailable — initialise the schema first.")
    elif df is not None and not df.empty:
        row = df.iloc[0]

        def count(column: str) -> str:
            return f"{int(row.get(column) or 0):,}"

        cols = st.columns(3)
        cols[0].metric("Pending confirmation", count("PENDING_CONFIRMATION"))
        cols[1].metric("Confirmed (registry)", count("CONFIRMED_IN_REGISTRY"))
        cols[2].metric("Handed off to profit", count("HANDED_OFF"))
    if st.button("Reconcile confirmed fixes now", use_container_width=True, key="rem_reconcile"):
        with st.spinner("Confirming fixes against the Cortex source..."):
            try:
                conn.execute(rec.build_reconcile_block(objs))
                _flash(
                    "success",
                    "Reconcile complete — confirmed fixes were handed off to the profit table.",
                )
            except Exception as exc:
                _flash("error", f"Reconcile failed: {exc}")
        st.rerun()


def _render_analytics(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    st.subheader("Breakdown")
    flags_df, error = _run(conn, an.build_flag_totals_query(objs))
    if error:
        st.error(f"Could not read flag totals: {error}")
    elif flags_df is not None and not flags_df.empty:
        totals = flags_df.iloc[0].fillna(0).astype(int)  # SUM over zero rows is NULL
        totals = totals[totals > 0].sort_values(ascending=False)
        if totals.empty:
            st.caption("No actionable broken terminals in the current snapshot.")
        else:
            st.caption("Actionable broken terminals per check")
            chart_data = totals.rename_axis("check").reset_index(name="terminals")
            figure = px.bar(
                chart_data,
                x="terminals",
                y="check",
                orientation="h",
                text="terminals",
            )
            figure.update_traces(textposition="outside", cliponaxis=False)
            figure.update_layout(
                height=max(360, 34 * len(chart_data)),
                margin={"l": 360, "r": 50, "t": 20, "b": 45},
                xaxis_title="Actionable broken terminals",
                yaxis_title=None,
                yaxis={"categoryorder": "total ascending", "automargin": True},
                showlegend=False,
            )
            st.plotly_chart(figure, use_container_width=True)

    dimension = st.selectbox("Group by dimension", DIMENSION_COLUMNS, index=0, key="rem_group_by")
    dim_df, dim_error = _run(conn, an.build_breakdown_query(dimension, objs, top_n=20))
    if dim_error:
        st.error(f"Could not read breakdown: {dim_error}")
    elif dim_df is not None and not dim_df.empty:
        st.bar_chart(dim_df.set_index("CATEGORY"))


def _render_activity(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    """Recent remediation throughput (last 14 days) from the shared fix log."""
    df, error = _run(conn, an.build_fix_activity_query(objs))
    if error or df is None or df.empty:
        st.caption("No fix activity logged in the last 14 days.")
        return
    st.caption("Fix activity — live applied vs rehearsed (last 14 days)")
    chart = df.rename(columns={"FIX_DATE": "date"}).set_index("date")
    st.bar_chart(chart[[c for c in ("LIVE_APPLIED", "REHEARSED") if c in chart.columns]])


def _distinct_values(conn: SnowflakeConnection, column: str, objs: RemediationObjects) -> list[str]:
    df, error = _run(conn, wl.build_distinct_values_query(column, objs))
    if error or df is None or df.empty:
        return []
    return [str(v) for v in df["VALUE"].tolist()]


def _render_filters(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    *,
    key_prefix: str,
    include_target_filters: bool = False,
) -> wl.WorklistFilters:
    """Shared filters, with optional exact target IDs for the Single-fix page."""
    top = st.columns(3)
    state = top[0].selectbox(
        "State", ["ACTIONABLE", "AWAITING_REFRESH", "(all)"], index=0, key=f"{key_prefix}_state"
    )
    fixable_only = top[1].checkbox(
        "Fixable checks only", value=True, key=f"{key_prefix}_fixable_only"
    )
    limit = int(
        top[2].number_input(
            "Max rows",
            min_value=50,
            max_value=5000,
            value=500,
            step=50,
            key=f"{key_prefix}_max_rows",
        )
    )
    dimensions: dict[str, tuple[str, ...]] = {}
    dim_cols = st.columns(len(_FILTER_DIMS))
    for idx, column in enumerate(_FILTER_DIMS):
        chosen = dim_cols[idx].multiselect(
            column.replace("_", " ").title(),
            _distinct_values(conn, column, objs),
            key=f"{key_prefix}_dim_{column}",
        )
        if chosen:
            dimensions[column] = tuple(chosen)
    identifiers: dict[str, str] = {}
    if include_target_filters:
        st.caption("Optional exact target filters")
        target_cols = st.columns(3)
        for idx, (column, label) in enumerate((
            ("LOCATION_NO", "Location number"),
            ("INSTANCE_IDENTIFIER", "Instance identifier"),
            ("TERMINAL_IDENTIFIER", "Terminal identifier"),
        )):
            value = target_cols[idx].text_input(
                label,
                key=f"{key_prefix}_target_{column}",
                placeholder=f"Exact {label.lower()}",
            ).strip()
            if value:
                identifiers[column] = value
    check_pool = FIXABLE_CHECK_COLUMNS if fixable_only else TRACKED_CHECK_COLUMNS
    checks = st.multiselect("Broken on check(s)", check_pool, key=f"{key_prefix}_checks")
    return wl.WorklistFilters(
        remediation_state=None if state == "(all)" else state,
        dimensions=dimensions,
        identifiers=identifiers,
        check_columns=tuple(checks),
        fixable_only=fixable_only,
        limit=limit,
    )


def _run_worklist(
    conn: SnowflakeConnection, objs: RemediationObjects, filters: wl.WorklistFilters
) -> pd.DataFrame | None:
    """Run the worklist query for ``filters``; returns the rows (or None on empty/error)."""
    try:
        sql, params = wl.build_worklist_query(filters, objs)
    except ValueError as exc:
        st.error(str(exc))
        return None
    df, error = _run(conn, sql, params)
    if error:
        st.error(f"Could not read worklist: {error}")
        return None
    if df is None or df.empty:
        st.info("No terminals match the current filters.")
        return None
    return df


def _render_worklist_grid(
    conn: SnowflakeConnection, objs: RemediationObjects, df: pd.DataFrame
) -> pd.DataFrame:
    """Selectable grid + CSV download for the Single-fix page; sets the fixer selection."""
    st.caption(f"{len(df):,} terminal(s) shown — click a row to open it in the fixer below.")
    view = df.copy()
    view.insert(1, "BROKEN_CHECKS", [fixer.describe_checks(r) for r in df.to_dict("records")])
    event = st.dataframe(
        view[[c for c in _GRID_COLUMNS if c in view.columns]],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="rem_worklist_grid",
    )
    rows = ((event or {}).get("selection") or {}).get("rows") or []
    picked = str(view.iloc[rows[0]]["TERMINAL_IDENTIFIER"]) if rows else None
    # Only a *new* click moves the selection, so "Next terminal" is not undone by the
    # grid still highlighting the previous row.
    if picked and picked != st.session_state.get(_GRID_LAST_KEY):
        st.session_state[_GRID_LAST_KEY] = picked
        st.session_state[fix_panel.SELECTED_KEY] = picked
    st.download_button(
        "Download worklist (CSV)",
        df.to_csv(index=False).encode("utf-8"),
        file_name="dcc_remediation_worklist.csv",
        mime="text/csv",
        key="rem_download",
    )
    return df


def _render_fixer(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    worklist: pd.DataFrame | None,
    armed_live: bool,
) -> None:
    tid = st.session_state.get(fix_panel.SELECTED_KEY)
    if not tid:
        st.info("Select a terminal in the worklist to fix it one check at a time.")
        return
    sql, _ = wl.build_terminal_detail_query(objs)
    df, error = _run(conn, sql, [tid])
    if error:
        st.error(f"Could not read terminal {tid}: {error}")
        return
    if df is None or df.empty:
        st.info(f"Terminal {tid} is no longer in the latest snapshot — nothing to fix.")
        if st.button("Clear selection", key="rem_clear_selection"):
            st.session_state.pop(fix_panel.SELECTED_KEY, None)
            st.rerun()
        return
    ids = [] if worklist is None else [str(v) for v in worklist["TERMINAL_IDENTIFIER"]]
    next_terminal = None
    if ids:
        position = ids.index(tid) + 1 if tid in ids else 0
        next_terminal = ids[position] if position < len(ids) else None
    with st.container(border=True):
        fix_panel.render_fix_panel(
            conn, objs, df.iloc[0].to_dict(), armed_live=armed_live, next_terminal=next_terminal
        )


_PAGE_KEY = "rem_page"
_PAGES = ("Overview", "Single fix", "Batch fix", "Campaign")


def _render_overview(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    """Main page: insightful fleet statistics, recent activity, handoff, and the agent."""
    st.caption("Fleet health, recent activity, and the daily fix-confirmation handoff.")
    _render_kpis(conn, objs)
    with st.container(border=True):
        _render_refresh(conn, objs)
        st.divider()
        _render_reconcile(conn, objs)
    st.divider()
    _render_analytics(conn, objs)
    _render_activity(conn, objs)
    st.divider()
    agent_panel.render_account_agent(conn, context_key="overview", expanded=True)


def _render_single(conn: SnowflakeConnection, objs: RemediationObjects, armed_live: bool) -> None:
    """Single-fix page: filter + selectable worklist + the one-by-one fixer."""
    st.caption(
        "Fix one terminal at a time: pre-check → dry run → apply live (DEV/UAT, or PROD "
        "behind the change-reference gate) → verify → logged → registered."
    )
    filters = _render_filters(
        conn, objs, key_prefix="rem", include_target_filters=True
    )
    df = _run_worklist(conn, objs, filters)
    worklist = _render_worklist_grid(conn, objs, df) if df is not None else None
    st.divider()
    _render_fixer(conn, objs, worklist, armed_live)
    if not st.session_state.get(fix_panel.SELECTED_KEY):
        agent_panel.render_account_agent(conn, context_key="single")


def _render_batch(conn: SnowflakeConnection, objs: RemediationObjects, armed_live: bool) -> None:
    """Batch-fix page: filter + select terminals + fix every broken check they have."""
    st.caption(
        "Fix many terminals at once: filter, choose terminals, and apply every fixable broken "
        "check they have — not only the check you filtered on. Every attempt is logged."
    )
    filters = _render_filters(conn, objs, key_prefix="rembatch")
    df = _run_worklist(conn, objs, filters)
    batch_panel.render_batch_panel(conn, objs, df, armed_live=armed_live)


def _render_campaign(conn: SnowflakeConnection, objs: RemediationObjects, armed_live: bool) -> None:
    """Run explicitly selected checks and targets as one CAB-referenced campaign."""
    campaign_panel.render_campaign_panel(conn, objs, armed_live=armed_live)


def render_remediation(armed_live: bool = False) -> None:
    """Entry point wired into the console's tab strip / PROD-streamlined layout.

    ``armed_live`` is the console's own Mode control: Live selected, the environment
    name typed, and the readiness probes passed. Apply live needs it (and much more).

    The experience is split into four pages — **Overview**, **Single fix**,
    **Batch fix** and **Campaign**.
    """
    st.subheader("DCC Remediation")
    _show_flash()
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

    writes.render_pending_logs(conn)
    with st.container(border=True):
        ready = _render_init(conn, objs)
    if not ready:  # every read below needs the schema; don't cascade errors
        return

    st.session_state.setdefault(_PAGE_KEY, _PAGES[0])
    page = st.radio(
        "Section", _PAGES, horizontal=True, key=_PAGE_KEY, label_visibility="collapsed"
    )
    if page == "Single fix":
        _render_single(conn, objs, armed_live)
    elif page == "Batch fix":
        _render_batch(conn, objs, armed_live)
    elif page == "Campaign":
        _render_campaign(conn, objs, armed_live)
    else:
        _render_overview(conn, objs)

    st.caption(
        f"Every attempt is logged to the shared `{objs.fix_log_table}`; only PROD outcomes "
        "change the worklist. Live-fix operators are listed in "
        f"`{objs.operators_table}`."
    )
