"""Batch fixer UI: filter → select terminals → fix every broken check they have.

Drives the same primitives as the one-by-one panel (:mod:`.fixer` + :mod:`.writes`), so a
batch run logs and registers outcomes identically — just non-interactively, over many
calls. Planning (what to run, de-duplicated) is pure in :mod:`.batch`; this module adds
the Streamlit controls and the execution loop.

Two run modes:

* **Rehearse** — pre-check + dry run every call (no live writes); safe for anyone.
* **Apply live** — the full pre-check → dry run → apply → verify cycle, behind the same
  gate as the single fixer (operator, Mode armed, and on PROD a CAN_PROD operator + a
  change/CAB reference), plus one batch-wide authorisation of the exact statements.

Bit 16 (receipt template) is never auto-applied here — see :mod:`.batch`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from ..execution import build_call
from ..state import add_result
from . import RemediationObjects, agent_panel, batch, fixer, fixlog, writes
from .fix_panel import operator_status
from .sf_connection import SnowflakeConnection

# Guard-rails: one batch run is capped so a mis-click can't fan out across the fleet.
MAX_BATCH_TERMINALS = 200
MAX_BATCH_ITEMS = 300

_RESULTS_KEY = "rem_batch_results"


@dataclass
class ItemResult:
    """One line in the post-run summary (one procedure call)."""

    flag_name: str
    target: str
    terminals: int
    outcome: str
    verified: bool | None
    detail: str


def _query(conn: SnowflakeConnection, sql: str, params: list | None = None):
    try:
        return conn.query(sql, params), None
    except Exception as exc:
        return None, str(exc)


def _last_live(
    conn: SnowflakeConnection, objs: RemediationObjects, step: fixer.FixStep, environment: str
) -> dict | None:
    """Latest live APPLIED/ROLLED_BACK row for this item's primary terminal + check."""
    primary = step.worklist_target
    frame, _ = _query(
        conn, fixlog.build_last_live_outcome_query(objs), [primary, step.check_column, environment]
    )
    return None if frame is None or frame.empty else frame.iloc[0].to_dict()


# --------------------------------------------------------------------------- #
# execution (mirrors fix_panel's per-step pipeline, run head-less in a loop)
# --------------------------------------------------------------------------- #


def _run_item(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    item: batch.BatchItem,
    *,
    sql_conn,
    environment: str,
    login: str,
    user: str,
    is_operator: bool,
    can_prod: bool,
    armed_live: bool,
    apply_live: bool,
    change_ref: str,
    batch_id: str,
    campaign_mode: bool = False,
) -> ItemResult:
    """Run one call through pre-check → (dry run) → (apply + verify), logging each step."""
    step = item.step
    covered = list(item.covered_rows)
    correlation = fixer.new_correlation_id()
    run_label = "Campaign" if campaign_mode else "Batch"
    notes = f"{run_label} {batch_id}"
    target = f"{step.target_kind.split('_')[0].lower()} {step.target_identifier}"
    log_context = dict(
        step=step,
        covered_rows=covered,
        environment=environment,
        server=sql_conn.server,
        database_name=sql_conn.database,
        operator=user,
        sql_login=login,
        correlation_id=correlation,
        change_ref=change_ref,
        notes=notes,
    )

    def result(outcome: str, verified: bool | None, detail: str) -> ItemResult:
        return ItemResult(step.flag.flag_name, target, len(covered), outcome, verified, detail)

    # 1 · pre-check -------------------------------------------------------------
    pre = fixer.precheck(sql_conn, step, environment)
    recorded = fixer.precheck_outcome(pre)
    if recorded is not None:  # ALREADY_OK or NOT_FOUND — settled by the read, nothing to apply
        writes.write_log(
            conn,
            objs,
            fixer.log_records(
                **log_context,
                outcome=recorded.outcome,
                mode=recorded.mode,
                verified=recorded.verified,
                pre=pre,
                error=recorded.error,
            ),
            summary=f"batch pre-check of {target}",
        )
        if fixer.should_register(recorded):
            writes.register_fix(
                conn,
                objs,
                fixer.registry_records(
                    step=step,
                    covered_rows=covered,
                    environment=environment,
                    operator=user,
                    correlation_id=correlation,
                    resolution=fixer.registry_resolution(recorded),
                    change_ref=change_ref,
                ),
                summary=f"batch pre-check of {target}",
            )
        return result(recorded.outcome, recorded.verified, pre.detail)
    if pre.status != fixer.NEEDS_FIX:  # UNKNOWN — a read problem; never treat as a fix
        return result("SKIPPED", None, pre.detail)

    # 2 · dry run ---------------------------------------------------------------
    try:
        dry_result = fixer.run_step(
            sql_conn,
            step,
            simulation=True,
            environment=environment,
            login=login,
            correlation_id=correlation,
        )
    except Exception as exc:
        return result("ERROR", None, f"Dry run did not start: {exc}")
    add_result(dry_result)
    dry = fixer.DryRun(step.signature(environment), dry_result.status, time.time())
    recorded = fixer.dry_run_outcome(dry_result)
    writes.write_log(
        conn,
        objs,
        fixer.log_records(
            **log_context,
            outcome=recorded.outcome,
            mode=recorded.mode,
            verified=recorded.verified,
            result=dry_result,
            pre=pre,
            error=recorded.error,
        ),
        summary=f"batch dry run on {target}",
    )
    if dry_result.status != "PASS":
        return result("DRY_RUN_FAILED", None, dry_result.error or dry_result.status)
    if not apply_live:
        return result("REHEARSED", None, "Dry run passed; not applied (rehearse mode).")

    # 3 · apply live ------------------------------------------------------------
    gate = fixer.live_gate(
        step=step,
        environment=environment,
        is_operator=is_operator,
        armed_live=armed_live,
        pre=pre,
        dry_run=dry,
        last_live=_last_live(conn, objs, step, environment),
        source_last_altered=covered[0].get("SOURCE_LAST_ALTERED"),
        can_prod=can_prod,
        change_ref=change_ref,
    )
    if not gate.allowed:
        return result("BLOCKED", None, "; ".join(gate.reasons))
    try:
        live_result = fixer.run_step(
            sql_conn,
            step,
            simulation=False,
            environment=environment,
            login=login,
            correlation_id=correlation,
            change_ref=change_ref,
        )
        post = fixer.precheck(sql_conn, step, environment)
    except Exception as exc:
        return result("ERROR", None, f"Live call did not run: {exc}")
    add_result(live_result)
    recorded = fixer.live_outcome(live_result, post)
    writes.write_log(
        conn,
        objs,
        fixer.log_records(
            **log_context,
            outcome=recorded.outcome,
            mode=recorded.mode,
            verified=recorded.verified,
            result=live_result,
            pre=pre,
            post=post,
            error=recorded.error,
        ),
        summary=f"batch live fix on {target}",
        keep=live_result if recorded.outcome == "APPLIED" and recorded.verified else None,
    )
    if fixer.should_register(recorded):
        writes.register_fix(
            conn,
            objs,
            fixer.registry_records(
                step=step,
                covered_rows=covered,
                environment=environment,
                operator=user,
                correlation_id=correlation,
                resolution=fixer.registry_resolution(recorded),
                change_ref=change_ref,
                result=live_result,
            ),
            summary=f"batch live fix on {target}",
        )
    return result(recorded.outcome, recorded.verified, recorded.error or post.detail)


def _execute(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    plan: batch.BatchPlan,
    *,
    sql_conn,
    environment: str,
    login: str,
    user: str,
    is_operator: bool,
    can_prod: bool,
    armed_live: bool,
    apply_live: bool,
    change_ref: str,
    campaign_mode: bool = False,
) -> list[ItemResult]:
    batch_id = fixer.new_correlation_id()[:12]
    results: list[ItemResult] = []
    progress = st.progress(0.0, text="Starting batch…")
    total = len(plan.items)
    for i, item in enumerate(plan.items, start=1):
        progress.progress(i / total, text=f"{item.step.flag.flag_name} ({i}/{total})")
        results.append(
            _run_item(
                conn,
                objs,
                item,
                sql_conn=sql_conn,
                environment=environment,
                login=login,
                user=user,
                is_operator=is_operator,
                can_prod=can_prod,
                armed_live=armed_live,
                apply_live=apply_live,
                change_ref=change_ref,
                batch_id=batch_id,
                campaign_mode=campaign_mode,
            )
        )
    progress.empty()
    return results


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _render_plan(plan: batch.BatchPlan) -> None:
    cols = st.columns(4)
    cols[0].metric("Terminals selected", f"{len(plan.selected_terminals):,}")
    cols[1].metric("Fixes to run", f"{len(plan.items):,}")
    cols[2].metric("Need manual template", f"{len(plan.templates):,}")
    cols[3].metric("Manual follow-up", f"{len(plan.manual):,}")

    extra = plan.terminals_with_extra_fixes
    if extra:
        st.info(
            f"**{len(extra)}** selected terminal(s) are broken on more than one fixable check — "
            "batch fixes **all** of them, not just the check you filtered on."
        )
    if plan.items:
        rows = []
        for it in plan.items:
            kind = it.step.target_kind.split("_")[0].title()
            rows.append(
                {
                    "Check": it.step.flag.flag_name,
                    "Bit": it.step.definition.bit,
                    "Target": f"{kind} {it.step.target_identifier}",
                    "Value": it.step.config_value,
                    "Terminals covered": len(it.covered_rows),
                }
            )
        st.caption("Fixes batch will run")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    if plan.templates:
        with st.expander(f"Templates needing a manual choice ({len(plan.templates)})"):
            st.caption(
                "Bit 16 templates are chosen per terminal and confirmed by an operator — fix "
                "these on the **Single fix** page."
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Check": t.flag_name,
                            "Instance": t.target_identifier,
                            "Terminals": len(t.covered_rows),
                        }
                        for t in plan.templates
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
    if plan.manual:
        with st.expander(f"Not fixable by the procedure ({len(plan.manual)})"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Terminal": m.terminal_identifier, "Check": m.flag_name, "Why": m.reason}
                        for m in plan.manual
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )


def _render_results(results: list[ItemResult]) -> None:
    if not results:
        return
    frame = pd.DataFrame(
        [
            {
                "Check": r.flag_name,
                "Target": r.target,
                "Terminals": r.terminals,
                "Outcome": r.outcome,
                "Verified": r.verified,
                "Detail": r.detail,
            }
            for r in results
        ]
    )
    applied = sum(1 for r in results if r.outcome == "APPLIED" and r.verified)
    rehearsed = sum(1 for r in results if r.outcome == "REHEARSED")
    skipped = sum(1 for r in results if r.outcome in ("SKIPPED_ALREADY_OK", "SKIPPED", "NOT_FOUND"))
    blocked = sum(
        1 for r in results if r.outcome in ("BLOCKED", "ERROR", "DRY_RUN_FAILED", "FAILED")
    )
    st.caption(
        f"Applied & verified {applied} · rehearsed {rehearsed} · skipped {skipped} · "
        f"blocked/failed {blocked}"
    )
    st.dataframe(frame, hide_index=True, use_container_width=True)


def render_batch_panel(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    worklist: pd.DataFrame | None,
    *,
    armed_live: bool,
) -> None:
    """Select terminals and fix all their broken checks using the batch workflow."""
    writes.show_notices()
    if worklist is None or worklist.empty:
        st.info("No terminals match the current filters. Adjust the filters above.")
        return

    rows_all = worklist.to_dict("records")
    ids_all = [str(r["TERMINAL_IDENTIFIER"]) for r in rows_all]
    default = ids_all[:MAX_BATCH_TERMINALS]
    selected_ids = st.multiselect(
        "Terminals in this batch",
        ids_all,
        default=default,
        key="rem_batch_terminals",
        help="Batch fixes every fixable broken check on each selected terminal.",
    )
    selected = set(selected_ids)
    selected_rows = [r for r in rows_all if str(r["TERMINAL_IDENTIFIER"]) in selected]
    if not selected_rows:
        st.info("Select at least one terminal to plan a batch.")
        return

    plan = batch.plan_batch(selected_rows)
    _render_plan(plan)
    agent_panel.render_account_agent(conn, context_key="batch")

    if not plan.has_work:
        st.info("Nothing to run for this selection (see manual / template items above).")
        return

    over_cap = (
        len(plan.selected_terminals) > MAX_BATCH_TERMINALS or len(plan.items) > MAX_BATCH_ITEMS
    )
    if over_cap:
        st.error(
            f"This batch is too large ({len(plan.selected_terminals)} terminals, "
            f"{len(plan.items)} fixes). The cap is {MAX_BATCH_TERMINALS} terminals / "
            f"{MAX_BATCH_ITEMS} fixes — narrow the filters or deselect some terminals."
        )

    sql_conn = st.session_state.get("connection")
    environment = str(st.session_state.get("connected_env") or "").upper()
    login = str(st.session_state.get("connected_login") or "")
    if sql_conn is None or not environment:
        st.info("Connect to SQL Server in the sidebar to rehearse or apply this batch.")
        return
    user, is_operator, can_prod, operator_error = operator_status(conn, objs)
    if operator_error:
        st.caption(f"Operator check failed: {operator_error}")
    is_prod = environment == fixer.PROD_ENVIRONMENT

    st.divider()
    mode = st.radio(
        "Run mode",
        ["Rehearse (dry run only)", "Apply live"],
        horizontal=True,
        key="rem_batch_mode",
    )
    apply_live = mode.startswith("Apply")

    change_ref = ""
    authorised = False
    if apply_live:
        where = f"{environment} · {sql_conn.server}/{sql_conn.database}"
        (st.warning if is_prod else st.info)(
            f"**{where}{' — PRODUCTION' if is_prod else ''}.** Apply live runs the gated "
            "pre-check → dry run → apply → verify cycle on every fix below."
        )
        if is_prod:
            change_ref = st.text_input(
                "Change / CAB reference (required for PROD)",
                key="rem_batch_change_ref",
                placeholder="e.g. CHG0043215",
            ).strip()
        with st.expander(f"Exact statements Apply live will run ({len(plan.items)})"):
            for it in plan.items:
                _, _, rendered = build_call(
                    it.step.definition,
                    it.step.target_identifier,
                    it.step.config_value,
                    simulation=False,
                )
                st.code(rendered, language="sql")
        authorised = st.checkbox(
            f"I have reviewed these {len(plan.items)} statement(s) and authorise applying them "
            f"live on {environment}",
            key="rem_batch_authorise",
        )

    if apply_live:
        blockers = []
        if not is_operator:
            blockers.append("not on the operator list")
        if not armed_live:
            blockers.append("console Mode not armed for live")
        if is_prod and not can_prod:
            blockers.append("operator is not PROD-approved (CAN_PROD)")
        if is_prod and not change_ref:
            blockers.append("change/CAB reference required")
        if not authorised:
            blockers.append("statements not yet authorised")
        if blockers:
            st.caption("Apply live is disabled: " + "; ".join(blockers) + ".")

    run_disabled = over_cap or (
        apply_live
        and not (
            is_operator
            and armed_live
            and authorised
            and (not is_prod or (can_prod and change_ref))
        )
    )
    label = (
        f"Apply {len(plan.items)} fix(es) live on {environment}"
        if apply_live
        else f"Rehearse {len(plan.items)} fix(es)"
    )
    if st.button(
        label,
        type="primary",
        use_container_width=True,
        disabled=run_disabled,
        key="rem_batch_run",
    ):
        with st.spinner("Running batch…"):
            results = _execute(
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
                change_ref=change_ref,
            )
        st.session_state[_RESULTS_KEY] = results
        st.rerun()

    _render_results(st.session_state.get(_RESULTS_KEY) or [])
