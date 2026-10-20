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
    batch_upload,
    ddl,
    fix_panel,
    fixer,
    parse_schema_fqn,
    registry,
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
            "Your role scopes what you can do."
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
            "Database",
            value=os.getenv("SNOWFLAKE_DATABASE", "AI_WORKSPACE"),
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
        f"Target schema `{objs.schema_fqn}`.{shared_note} Existing ready objects are verified "
        "without changes. Missing objects or upgrades require schema-owner permissions."
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
    if st.button(
        "Initialise / verify my schema",
        use_container_width=True,
        key="rem_init",
        disabled=error is not None,
    ):
        if status.ready:
            st.success(f"Schema {objs.schema_fqn} is ready. Verified without changes.")
            return True
        include_shared = not status.shared_ready
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

    fix_confirmed_df, fix_confirmed_error = _run(
        conn,
        f"SELECT COUNT(*) AS FIX_CONFIRMED_PROD FROM {objs.registry_table} "  # noqa: S608
        f"WHERE ENVIRONMENT = 'PROD' AND STATUS = '{registry.STATUS_CONFIRMED}' "
        "AND RESOLUTION = 'APPLIED'",
    )
    fix_confirmed = (
        f"{int(fix_confirmed_df.iloc[0]['FIX_CONFIRMED_PROD'] or 0):,}"
        if not fix_confirmed_error and fix_confirmed_df is not None and not fix_confirmed_df.empty
        else "—"
    )
    fixed_terminals_df, fixed_terminals_error = _run(
        conn,
        f"SELECT COUNT(DISTINCT TERMINAL_IDENTIFIER) AS UNIQUE_FIXED_TERMINALS_PROD "  # noqa: S608
        f"FROM {objs.confirmed_table} "  # noqa: S608
        "WHERE ENVIRONMENT = 'PROD' AND RESOLUTION = 'APPLIED'",
    )
    fixed_terminals = (
        f"{int(fixed_terminals_df.iloc[0]['UNIQUE_FIXED_TERMINALS_PROD'] or 0):,}"
        if not fixed_terminals_error
        and fixed_terminals_df is not None
        and not fixed_terminals_df.empty
        else "—"
    )

    first_row = st.columns(4)
    first_row[0].metric(
        "Actionable broken",
        count("BROKEN_ACTIONABLE"),
        help=(
            "Terminals in the latest snapshot that still have at least one actionable broken check."
        ),
    )
    first_row[1].metric(
        "Fixable",
        count("FIXABLE_TERMINALS"),
        help=(
            "Actionable terminals with at least one unresolved broken check that this app can "
            "fix through its supported remediation procedures."
        ),
    )
    first_row[2].metric(
        "Awaiting refresh",
        count("AWAITING_REFRESH"),
        help=(
            "Terminals with verified PROD fixes and no other open fixable checks, "
            "awaiting the next source snapshot."
        ),
    )
    first_row[3].metric(
        "Latest snapshot",
        str(row.get("LATEST_SNAPSHOT_DATE") or "—"),
        help="Most recent SNAPSHOT_DATE loaded into this user's HEALTH_DAILY_SNAPSHOT table.",
    )

    second_row = st.columns(4)
    second_row[0].metric(
        "Live applied · PROD",
        count("TOTAL_LIVE_FIXES"),
        help=(
            "Distinct successful LIVE procedure calls in PROD with OUTCOME=APPLIED. "
            "SKIPPED_ALREADY_OK pre-checks are counted separately. One call can cover "
            "multiple terminals."
        ),
    )
    second_row[1].metric(
        "Already OK · PROD",
        count("ALREADY_OK_PROD"),
        help=(
            "Distinct PROD pre-checks logged as SKIPPED_ALREADY_OK. The target already "
            "had the requested setting; the procedure was not applied."
        ),
    )
    second_row[2].metric(
        "Dry runs · PROD",
        count("PROD_SIMULATIONS"),
        help=(
            "Distinct PROD simulation attempts in APP_FIX_LOG, including failed dry runs. "
            "These execute with simulation enabled and do not commit the fix."
        ),
    )
    second_row[3].metric(
        "Registered · PROD",
        count("REGISTERED_FIXES"),
        help=(
            "Current PROD terminal/check entries in DCC_FIX_REGISTRY with status FIXED or "
            "CONFIRMED."
        ),
    )

    third_row = st.columns(3)
    third_row[0].metric(
        "Confirmed · PROD",
        count("CONFIRMED_FIXES"),
        help=(
            "All registry resolutions confirmed by Cortex. Includes APPLIED fixes and "
            "ALREADY_OK checks where the app did not change the value."
        ),
    )
    third_row[1].metric(
        "Rehearsal simulations · UAT/DEV",
        count("REHEARSAL_FIXES"),
        help=(
            "Distinct UAT and DEV simulation attempts in APP_FIX_LOG, including failed dry "
            "runs. These do not commit fixes."
        ),
    )
    third_row[2].metric(
        "Live applied · UAT/DEV",
        count("LIVE_APPLIED_UAT_DEV"),
        help=(
            "Distinct successful LIVE procedure calls applied in UAT or DEV. These are "
            "committed outside PROD and are not included in PROD fix counts."
        ),
    )

    app_fix_row = st.columns(2)
    app_fix_row[0].metric(
        "Fix confirmed · PROD",
        fix_confirmed,
        help="Confirmed PROD registry entries with RESOLUTION=APPLIED; excludes ALREADY_OK.",
    )
    app_fix_row[1].metric(
        "Terminals fixed · PROD",
        fixed_terminals,
        help=(
            "Distinct PROD terminals with an APPLIED row in DCC_CONFIRMED_FIXES. "
            "A terminal with multiple fixes is counted once."
        ),
    )


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
        cols[0].metric(
            "Pending confirmation",
            count("PENDING_CONFIRMATION"),
            help=(
                "All FIXED PROD registry entries awaiting Cortex confirmation; "
                "includes APPLIED and ALREADY_OK."
            ),
        )
        cols[1].metric(
            "Confirmed (registry)",
            count("CONFIRMED_IN_REGISTRY"),
            help="All confirmed registry entries; includes APPLIED and ALREADY_OK resolutions.",
        )
        cols[2].metric(
            "Handed off to profit",
            count("HANDED_OFF"),
            help="All rows in DCC_CONFIRMED_FIXES; includes APPLIED and ALREADY_OK resolutions.",
        )
        applied_df, applied_error = _run(
            conn,
            f"SELECT\n"  # noqa: S608
            f"    (SELECT COUNT(*) FROM {objs.registry_table}\n"
            f"        WHERE ENVIRONMENT = 'PROD' AND STATUS = '{rec.registry.STATUS_FIXED}' "
            "AND RESOLUTION = 'APPLIED') AS PENDING_CONFIRMATION_APPLIED,\n"
            f"    (SELECT COUNT(*) FROM {objs.registry_table}\n"
            f"        WHERE ENVIRONMENT = 'PROD' AND STATUS = '{rec.registry.STATUS_CONFIRMED}' "
            "AND RESOLUTION = 'APPLIED') AS CONFIRMED_IN_REGISTRY_APPLIED,\n"
            f"    (SELECT COUNT(*) FROM {objs.confirmed_table}\n"
            "        WHERE ENVIRONMENT = 'PROD' AND RESOLUTION = 'APPLIED') AS HANDED_OFF_APPLIED",
        )

        def applied_count(column: str) -> str:
            if applied_error or applied_df is None or applied_df.empty:
                return "—"
            return f"{int(applied_df.iloc[0].get(column) or 0):,}"

        st.caption("App-applied fixes only (RESOLUTION = APPLIED; excludes ALREADY_OK)")
        app_fix_cols = st.columns(3)
        app_fix_cols[0].metric(
            "Pending confirmation · App fixes",
            applied_count("PENDING_CONFIRMATION_APPLIED"),
            help=(
                "PROD registry entries fixed by the app that are still awaiting "
                "Cortex confirmation."
            ),
        )
        app_fix_cols[1].metric(
            "Confirmed · App fixes",
            applied_count("CONFIRMED_IN_REGISTRY_APPLIED"),
            help="App-applied PROD registry entries confirmed healthy by Cortex.",
        )
        app_fix_cols[2].metric(
            "Handed off · App fixes",
            applied_count("HANDED_OFF_APPLIED"),
            help="App-applied rows in DCC_CONFIRMED_FIXES; excludes ALREADY_OK.",
        )
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
        figure = px.bar(
            dim_df,
            x="TERMINALS",
            y="CATEGORY",
            orientation="h",
            text="TERMINALS",
        )
        figure.update_traces(textposition="outside", cliponaxis=False)
        figure.update_layout(
            height=max(520, 32 * len(dim_df)),
            margin={"l": 360, "r": 50, "t": 20, "b": 45},
            xaxis_title="Actionable broken terminals",
            yaxis_title=None,
            yaxis={"categoryorder": "total ascending", "automargin": True},
            showlegend=False,
        )
        st.plotly_chart(figure, use_container_width=True)


def _render_activity(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    """Recent PROD fixes and environment-specific simulations from the shared fix log."""
    df, error = _run(conn, an.build_fix_activity_query(objs))
    if error or df is None or df.empty:
        st.caption("No fix activity logged in the last 14 days.")
        return
    activity_labels = {
        "LIVE_APPLIED_PROD": "Live applied · PROD",
        "ALREADY_OK_PROD": "Pre-check already OK · PROD",
        "DRY_RUN_PROD": "Dry run · PROD",
        "DRY_RUN_UAT": "Rehearsal · UAT",
        "DRY_RUN_DEV": "Rehearsal · DEV",
        "LIVE_APPLIED_UAT_DEV": "Live applied · UAT/DEV",
    }
    available = [column for column in activity_labels if column in df.columns]
    if not available:
        st.caption("No fix activity logged in the last 14 days.")
        return
    st.caption("Fix activity by environment and outcome (last 14 days)")
    chart = df.melt(
        id_vars="FIX_DATE",
        value_vars=available,
        var_name="ACTIVITY",
        value_name="CALLS",
    )
    chart["ACTIVITY"] = chart["ACTIVITY"].map(activity_labels)
    figure = px.bar(
        chart,
        x="FIX_DATE",
        y="CALLS",
        color="ACTIVITY",
        barmode="group",
        labels={"FIX_DATE": "Date", "CALLS": "Procedure calls", "ACTIVITY": "Activity"},
    )
    figure.update_layout(height=380, margin={"l": 55, "r": 25, "t": 20, "b": 45})
    figure.update_xaxes(tickformat="%d %b", dtick="D1")
    st.plotly_chart(figure, use_container_width=True)


def _render_fix_coverage(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    st.subheader("PROD fix coverage")
    st.caption(
        "Unique target IDs currently in DCC_FIX_REGISTRY, grouped by target type. "
        "A target can cover multiple terminals and checks."
    )
    df, error = _run(conn, an.build_fix_coverage_query(objs))
    if error:
        st.info(f"PROD fix coverage unavailable: {error}")
    elif df is None or df.empty:
        st.caption("No registered PROD fixes.")
    else:
        coverage = df.rename(
            columns={
                "TARGET_TYPE": "Target type",
                "REGISTERED_TARGETS": "Registered targets",
                "CONFIRMED_TARGETS": "Confirmed targets",
                "TERMINALS_COVERED": "Terminals covered",
            }
        )
        st.dataframe(coverage, use_container_width=True, hide_index=True)

    st.subheader("PROD fix coverage by bit")
    st.caption(
        "Call counts come from APP_FIX_LOG; registered and confirmed counts are terminal/check "
        "entries from DCC_FIX_REGISTRY. A live call can cover multiple terminals."
    )
    bit_df, bit_error = _run(conn, an.build_bit_fix_coverage_query(objs))
    if bit_error:
        st.info(f"PROD fix coverage by bit unavailable: {bit_error}")
    elif bit_df is None or bit_df.empty:
        st.caption("No PROD fix activity by bit.")
    else:
        bit_labels = {
            1: "Location Xpress CO features",
            2: "Terminal config download version",
            8: "Instance handler flags",
            16: "Instance receipt template",
        }
        by_bit = bit_df.copy()
        by_bit["FIX_AREA"] = by_bit["FIX_BIT"].map(
            lambda bit: bit_labels.get(int(bit), "Other")
        )
        by_bit = by_bit[
            [
                "FIX_BIT",
                "FIX_AREA",
                "LIVE_APPLIED_CALLS",
                "ALREADY_OK_CALLS",
                "DRY_RUN_ATTEMPTS",
                "REGISTERED_CHECKS",
                "PENDING_CONFIRMATION_CHECKS",
                "CONFIRMED_CHECKS",
                "TERMINALS_COVERED",
            ]
        ].rename(
            columns={
                "FIX_BIT": "Bit",
                "FIX_AREA": "Fix area",
                "LIVE_APPLIED_CALLS": "Live applied calls",
                "ALREADY_OK_CALLS": "Already OK calls",
                "DRY_RUN_ATTEMPTS": "Dry-run attempts",
                "REGISTERED_CHECKS": "Registered checks",
                "PENDING_CONFIRMATION_CHECKS": "Pending confirmation",
                "CONFIRMED_CHECKS": "Confirmed checks",
                "TERMINALS_COVERED": "Terminals covered",
            }
        )
        st.dataframe(by_bit, use_container_width=True, hide_index=True)

    st.subheader("PROD terminals fixed by bit")
    st.caption(
        "Distinct terminals with an APPLIED PROD fix in DCC_FIX_REGISTRY; excludes ALREADY_OK. "
        "A terminal fixed under multiple bits appears once under each bit."
    )
    applied_by_bit_df, applied_by_bit_error = _run(
        conn,
        f"SELECT FIX_BIT, COUNT(DISTINCT TERMINAL_IDENTIFIER) AS APP_FIXED_TERMINALS_BY_BIT\n"
        f"FROM {objs.registry_table}\n"  # noqa: S608
        f"WHERE ENVIRONMENT = 'PROD' AND STATUS IN ('{registry.STATUS_FIXED}', "
        f"'{registry.STATUS_CONFIRMED}')\n"
        "  AND RESOLUTION = 'APPLIED' AND FIX_BIT IN (1, 2, 8, 16)\n"
        "GROUP BY FIX_BIT",
    )
    if applied_by_bit_error:
        st.info(f"App-applied PROD terminal counts by bit unavailable: {applied_by_bit_error}")
    else:
        app_fixed_by_bit = (
            {}
            if applied_by_bit_df is None or applied_by_bit_df.empty
            else {
                int(row["FIX_BIT"]): int(row["APP_FIXED_TERMINALS_BY_BIT"] or 0)
                for _, row in applied_by_bit_df.iterrows()
            }
        )
        app_fixed_coverage = pd.DataFrame(
            [
                {
                    "Bit": bit,
                    "Fix area": bit_labels[bit],
                    "Terminals fixed": app_fixed_by_bit.get(bit, 0),
                }
                for bit in (1, 2, 8, 16)
            ]
        )
        st.dataframe(app_fixed_coverage, use_container_width=True, hide_index=True)


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
    cache_distinct_values: bool = False,
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
        if cache_distinct_values:
            cache = st.session_state.setdefault("rem_batch_filter_values", {})
            cache_key = (id(conn), objs.v_current_broken, column)
            if cache_key not in cache:
                cache[cache_key] = _distinct_values(conn, column, objs)
            options = cache[cache_key]
        else:
            options = _distinct_values(conn, column, objs)
        chosen = dim_cols[idx].multiselect(
            column.replace("_", " ").title(),
            options,
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
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    filters: wl.WorklistFilters,
    *,
    show_empty: bool = True,
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
        if show_empty:
            st.info("No terminals match the current filters.")
        return None
    return df


def _uploaded_batch_rows(
    conn: SnowflakeConnection, objs: RemediationObjects, content: bytes
) -> tuple[pd.DataFrame, list[str]]:
    """Resolve uploaded requests to actionable, currently broken Snowflake rows."""
    targets = batch_upload.parse_workbook(content)
    query, params = wl.build_uploaded_targets_query(
        tuple((target.terminal_identifier, target.check_column) for target in targets),
        objs,
    )
    frame = conn.query(query, params)
    by_terminal = {
        str(row.get("TERMINAL_IDENTIFIER")): row for row in frame.to_dict("records")
    }
    checks_by_terminal: dict[str, set[str]] = {}
    warnings: list[str] = []
    for target in targets:
        row = by_terminal.get(target.terminal_identifier)
        if row is None or str(row.get(target.check_column) or "") != "1":
            warnings.append(
                f"Excel row {target.row_number}: terminal {target.terminal_identifier} "
                f"is not currently ACTIONABLE and broken on {target.check_column}; skipped."
            )
            continue
        mismatches = []
        for column, uploaded_value in target.metadata.items():
            actual_value = row.get(column)
            if not uploaded_value:
                continue
            if actual_value is None:
                mismatches.append(column)
                continue
            if column == "LOCATION_NO":
                matches = (
                    batch_upload.normalise_location_no(uploaded_value)
                    == batch_upload.normalise_location_no(actual_value)
                )
            else:
                uploaded_text = str(uploaded_value).strip().casefold()
                actual_text = str(actual_value).strip().casefold()
                matches = uploaded_text == actual_text
            if not matches:
                mismatches.append(column)
        if mismatches:
            warnings.append(
                f"Excel row {target.row_number}: uploaded metadata differs from the current "
                f"snapshot ({', '.join(mismatches)}); skipped."
            )
            continue
        checks_by_terminal.setdefault(target.terminal_identifier, set()).add(target.check_column)

    rows = [
        batch_upload.scope_row_to_checks(by_terminal[terminal_id], tuple(checks))
        for terminal_id, checks in checks_by_terminal.items()
    ]
    return pd.DataFrame(rows), warnings


def _load_batch_targets(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    filters: wl.WorklistFilters,
    uploaded_file,
) -> tuple[pd.DataFrame | None, list[str]]:
    """Load from the workbook exclusively, or use filters when no workbook is supplied."""
    if uploaded_file is not None:
        return _uploaded_batch_rows(conn, objs, uploaded_file.getvalue())
    return _run_worklist(conn, objs, filters), []


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
_PAGES = ("Overview", "Single fix", "Batch fix")


def _render_overview(conn: SnowflakeConnection, objs: RemediationObjects) -> None:
    """Main page: fleet statistics, recent activity, handoff, and the agent."""
    st.caption("Fleet health, recent activity, and the daily fix-confirmation handoff.")
    _render_kpis(conn, objs)
    with st.container(border=True):
        _render_refresh(conn, objs)
        st.divider()
        _render_reconcile(conn, objs)
    st.divider()
    _render_analytics(conn, objs)
    _render_activity(conn, objs)
    _render_fix_coverage(conn, objs)
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
    """Batch-fix page: filter terminals, then select fixes at their actual target scope."""
    st.caption(
        "Filter broken terminals, then choose procedure calls by their real target scope: "
        "Location (Bit 1), Terminal (Bit 2), or Instance (Bit 8). Shared-target calls include "
        "all matching broken terminals in the coverage table. Bit 16 is also instance-level "
        "and requires confirming a suggested receipt template or selecting an override. "
        "Every attempt is logged."
    )
    with st.form("rem_batch_filter_form"):
        filters = _render_filters(
            conn,
            objs,
            key_prefix="rembatch",
            include_target_filters=True,
            cache_distinct_values=True,
        )
        terminal_limit = int(
            st.number_input(
                "Terminal limit per run",
                min_value=1,
                max_value=5000,
                value=batch_panel.MAX_BATCH_TERMINALS,
                step=1,
                key="rem_batch_terminal_limit",
                help=(
                    "Set the maximum number of new terminals to add per load. Existing "
                    "rows are preserved and duplicate terminal IDs are not added. The separate "
                    "300-fix cap still applies."
                ),
            )
        )
        uploaded_file = st.file_uploader(
            "Add targets from Excel (.xlsx)",
            type=["xlsx"],
            key="rem_batch_upload_file",
            help=(
                "The first column contains the check to fix; remaining columns must use supported "
                "metadata headers such as instance_identifier, location_no, location_desc, "
                "country, bank_merchant_id, acquirer_name and TerminalID. "
                "When a file is supplied, only validated file targets are loaded; filter results "
                "are not added. Rows must match current actionable broken data."
            ),
        )
        load_terminals = st.form_submit_button(
            "Load / add terminals",
            type="primary",
            help=(
                "Apply the filters and optional Excel target list, then add matching broken "
                "targets. Changing filters or uploading a file alone does not query or change "
                "the Batch table. Existing rows and unchecked selections are preserved."
            ),
        )
    df = None
    if load_terminals:
        try:
            df, load_warnings = _load_batch_targets(conn, objs, filters, uploaded_file)
            if load_warnings:
                with st.expander(
                    f"Skipped Excel rows ({len(load_warnings)})", expanded=False
                ):
                    for warning in load_warnings:
                        st.caption(warning)
            if uploaded_file is not None and (df is None or df.empty):
                st.warning("The Excel upload produced no actionable broken targets.")
        except ValueError as exc:
            st.error(f"Excel file validation failed: {exc}")
        except Exception as exc:
            st.error(f"Could not load Batch targets: {exc}")
    batch_panel.render_batch_panel(
        conn,
        objs,
        df,
        armed_live=armed_live,
        terminal_limit=terminal_limit,
    )


def render_remediation(armed_live: bool = False) -> None:
    """Entry point wired into the console's tab strip / PROD-streamlined layout.

    ``armed_live`` is the console's own Mode control: Live selected, the environment
    name typed, and the readiness probes passed. Apply live needs it (and much more).

    The experience is split into three pages — **Overview**, **Single fix**,
    and **Batch fix**.
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

    if st.session_state.get(_PAGE_KEY) not in _PAGES:
        st.session_state[_PAGE_KEY] = _PAGES[0]
    page = st.radio(
        "Section", _PAGES, horizontal=True, key=_PAGE_KEY, label_visibility="collapsed"
    )
    if page == "Single fix":
        _render_single(conn, objs, armed_live)
    elif page == "Batch fix":
        _render_batch(conn, objs, armed_live)
    else:
        _render_overview(conn, objs)

    st.caption(
        f"Every attempt is logged to the shared `{objs.fix_log_table}`; only PROD outcomes "
        "change the worklist. Live-fix operators are listed in "
        f"`{objs.operators_table}`."
    )
