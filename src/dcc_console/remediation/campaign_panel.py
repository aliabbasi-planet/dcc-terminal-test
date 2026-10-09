"""Multi-check remediation campaigns, logged through the shared batch pipeline."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import streamlit as st

from . import RemediationObjects, batch, batch_panel, fix_panel, fixer, mapping, writes
from .sf_connection import SnowflakeConnection
from .worklist import WorklistFilters, build_worklist_query

_CAMPAIGN_RESULTS_KEY = "rem_campaign_results"
_CAMPAIGN_TARGETS_KEY = "rem_campaign_available_targets"


def _is_broken(value: object) -> bool:
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def _campaign_plan(
    conn: SnowflakeConnection,
    rows: list[dict],
    checks: set[str],
    target_selections: dict[str, set[str]],
):
    """Plan only explicitly selected check/target pairs and confirm Bit 16 templates."""
    scoped_rows = []
    for row in rows:
        scoped = dict(row)
        for column in mapping.TRACKED_CHECK_COLUMNS:
            flag = mapping.BY_CHECK[column]
            target_kind = flag.target_id_kind
            target_id = str(row.get(target_kind) or "") if target_kind else ""
            if column not in checks or target_id not in target_selections.get(target_kind, set()):
                scoped[column] = 0
        if any(_is_broken(scoped.get(column)) for column in checks):
            scoped_rows.append(scoped)

    plan = batch.plan_batch(scoped_rows)
    items = list(plan.items)
    missing_templates: list[str] = []
    for template in plan.templates:
        row = template.covered_rows[0]
        steps, _ = fixer.plan_for_row(row)
        step = next(
            (candidate for candidate in steps if candidate.check_column == template.check_column),
            None,
        )
        if step is None:
            missing_templates.append(template.flag_name)
            continue
        chosen = fix_panel._choose_template(conn, row, step)
        if chosen.config_value is None:
            missing_templates.append(
                f"{template.flag_name} for instance {template.target_identifier}"
            )
            continue
        items.append(batch.BatchItem(chosen, template.covered_rows))

    return replace(plan, items=tuple(items), templates=()), missing_templates


def _render_plan(plan: batch.BatchPlan) -> None:
    st.caption(f"Planned executions: **{len(plan.items):,}** fix(es).")

    if plan.items:
        st.caption("Campaign steps planned (one call per unique target/check)")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Check": item.step.flag.flag_name,
                        "Bit": item.step.definition.bit,
                        "Target": f"{item.step.target_kind} {item.step.target_identifier}",
                        "Value": item.step.config_value,
                        "Terminals covered": len(item.covered_rows),
                    }
                    for item in plan.items
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    if plan.manual:
        with st.expander(f"Manual follow-up ({len(plan.manual)})"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Terminal": item.terminal_identifier,
                            "Check": item.flag_name,
                            "Why": item.reason,
                        }
                        for item in plan.manual
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )


def render_campaign_panel(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    *,
    armed_live: bool,
) -> None:
    """Select remediation checks and targets explicitly, then run the guarded lifecycle."""
    writes.show_notices()
    st.subheader("Configuration test campaign")
    st.caption(
        "Select actionable broken checks and their targets, then run the corrective fixes "
        "together. Targets load on request and none are selected automatically."
    )
    campaign_ref = st.text_input(
        "CAB reference (required; groups campaign log records)",
        key="rem_campaign_ref",
        max_chars=120,
        placeholder="e.g. CHG0043215",
        help=(
            "Stored in CHANGE_REF for every APP_FIX_LOG attempt and every eligible verified "
            "DCC_FIX_REGISTRY entry."
        ),
    ).strip()

    selected_checks = st.multiselect(
        "Configuration checks",
        mapping.FIXABLE_CHECK_COLUMNS,
        default=list(mapping.FIXABLE_CHECK_COLUMNS),
        format_func=lambda column: mapping.BY_CHECK[column].flag_name,
        key="rem_campaign_checks",
    )
    if not selected_checks:
        st.info("Select at least one fixable configuration check.")
        return

    selected_flags = [mapping.BY_CHECK[column] for column in selected_checks]
    if st.button("Load available targets", key="rem_campaign_load_targets"):
        filters = WorklistFilters(remediation_state="ACTIONABLE", fixable_only=True, limit=5000)
        sql, params = build_worklist_query(filters, objs)
        with st.spinner("Loading actionable remediation targets..."):
            try:
                targets = conn.query(sql, params)
                st.session_state[_CAMPAIGN_TARGETS_KEY] = (
                    [] if targets is None else targets.to_dict("records")
                )
            except Exception as exc:
                st.session_state[_CAMPAIGN_TARGETS_KEY] = []
                st.error(f"Could not load campaign targets: {exc}")
        st.rerun()

    rows = st.session_state.get(_CAMPAIGN_TARGETS_KEY)
    if rows is None:
        st.info("Load available targets to choose the terminals, instances, or locations.")
        return
    if not rows:
        st.info("No actionable targets are available in the current snapshot.")
        return

    target_selections: dict[str, set[str]] = {}
    target_labels = {
        mapping.TERMINAL: "Active terminals",
        mapping.INSTANCE: "Instances",
        mapping.LOCATION: "Locations",
    }
    for target_kind in (mapping.TERMINAL, mapping.INSTANCE, mapping.LOCATION):
        flags = [flag for flag in selected_flags if flag.target_id_kind == target_kind]
        if not flags:
            continue
        target_columns = {flag.check_column for flag in flags}
        choices = sorted(
            {
                str(row[target_kind])
                for row in rows
                if row.get(target_kind) is not None
                and any(_is_broken(row.get(column)) for column in target_columns)
            }
        )
        if choices:
            selected = st.multiselect(
                target_labels[target_kind],
                choices,
                key=f"rem_campaign_targets_{target_kind}",
            )
        else:
            selected = []
            st.caption(f"No broken checks are available for {target_labels[target_kind].lower()}.")
        target_selections[target_kind] = set(selected)

    plan, missing_templates = _campaign_plan(
        conn, rows, set(selected_checks), target_selections
    )
    _render_plan(plan)
    if missing_templates:
        st.warning(
            "Choose and confirm a receipt template for every Bit 16 campaign target before "
            "running the campaign."
        )
    if (
        len(plan.selected_terminals) > batch_panel.MAX_BATCH_TERMINALS
        or len(plan.items) > batch_panel.MAX_BATCH_ITEMS
    ):
        st.error(
            f"Campaign exceeds the safety cap of {batch_panel.MAX_BATCH_TERMINALS} terminals / "
            f"{batch_panel.MAX_BATCH_ITEMS} procedure calls. Narrow the worklist or selection."
        )
        return
    sql_conn = st.session_state.get("connection")
    environment = str(st.session_state.get("connected_env") or "").upper()
    login = str(st.session_state.get("connected_login") or "")
    if sql_conn is None or not environment:
        st.info("Connect to SQL Server in the sidebar to rehearse or apply this campaign.")
        return

    user, is_operator, can_prod, operator_error = fix_panel.operator_status(conn, objs)
    if operator_error:
        st.caption(f"Operator check failed: {operator_error}")
    is_prod = environment == fixer.PROD_ENVIRONMENT
    mode = st.radio(
        "Campaign mode",
        ["Rehearse (dry run only)", "Apply live"],
        horizontal=True,
        key="rem_campaign_mode",
    )
    apply_live = mode.startswith("Apply")
    authorised = False

    if apply_live:
        where = f"{environment} · {sql_conn.server}/{sql_conn.database}"
        (st.warning if is_prod else st.info)(
            f"**{where}{' — PRODUCTION' if is_prod else ''}.** The campaign will run the "
            "pre-check → dry run → apply → verify workflow for each planned step."
        )
        with st.expander(f"Exact statements Apply live will run ({len(plan.items)})"):
            for item in plan.items:
                _, _, rendered = batch_panel.build_call(
                    item.step.definition,
                    item.step.target_identifier,
                    item.step.config_value,
                    simulation=False,
                )
                st.code(rendered, language="sql")
        authorised = st.checkbox(
            f"I reviewed these {len(plan.items)} statements and authorise this campaign on "
            f"{environment}",
            key="rem_campaign_authorise",
        )

    blockers = []
    if not campaign_ref:
        blockers.append("CAB reference is required")
    if not plan.has_work:
        blockers.append("select a target with a broken selected check")
    if missing_templates:
        blockers.append("every selected Bit 16 target needs a confirmed receipt template")
    if apply_live:
        if not is_operator:
            blockers.append("not on the live-fix operator list")
        if not armed_live:
            blockers.append("console Live mode is not armed")
        if is_prod and not can_prod:
            blockers.append("operator is not PROD-approved (CAN_PROD)")
        if is_prod and not campaign_ref:
            blockers.append("PROD change/CAB reference is required")
        if not authorised:
            blockers.append("campaign statements have not been authorised")
    if blockers:
        st.caption("Campaign cannot run: " + "; ".join(blockers) + ".")

    if st.button(
        f"{'Apply' if apply_live else 'Rehearse'} campaign · {len(plan.items)} fix(es)",
        type="primary",
        use_container_width=True,
        disabled=bool(blockers),
        key="rem_campaign_run",
    ):
        with st.spinner("Running campaign..."):
            results = batch_panel._execute(
                conn,
                objs,
                plan,
                sql_conn=sql_conn,
                environment=environment,
                login=login,
                user=user,
                is_operator=is_operator,
                can_prod=can_prod,
                armed_live=armed_live,
                apply_live=apply_live,
                change_ref=campaign_ref,
                campaign_mode=True,
            )
        st.session_state[_CAMPAIGN_RESULTS_KEY] = results
        st.session_state["rem_campaign_last_ref"] = campaign_ref
        st.rerun()

    previous = st.session_state.get(_CAMPAIGN_RESULTS_KEY) or []
    if previous:
        last_ref = st.session_state.get("rem_campaign_last_ref", "")
        st.caption(f"Latest campaign reference: `{last_ref}`")
        batch_panel._render_results(previous)
