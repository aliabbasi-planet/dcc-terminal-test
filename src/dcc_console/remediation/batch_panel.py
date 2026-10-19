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

Bit 16 receipt templates use the workbook allowlist and require operator confirmation per instance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

import pandas as pd
import streamlit as st

from ..execution import build_call
from ..state import add_result
from . import RemediationObjects, agent_panel, batch, batch_upload, fixer, fixlog, writes
from . import worklist as wl
from .fix_panel import _choose_template, _load_nfc_providers, operator_status
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
        detail = f"Dry run did not start: {exc}"
        writes.write_log(
            conn,
            objs,
            fixer.log_records(
                **log_context,
                outcome="FAILED",
                mode="SIMULATION",
                verified=None,
                pre=pre,
                error=detail,
            ),
            summary=f"batch dry run start failure on {target}",
        )
        return result("ERROR", None, detail)
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
    cols[0].metric(
        "Terminals affected", f"{len(plan.selected_terminals):,}",
        help="Distinct broken terminals covered by the selected target-scope fix groups.",
    )
    cols[1].metric(
        "Fixes to run", f"{len(plan.items):,}",
        help=(
            "Planned automatic procedure calls for unresolved broken checks. Shared "
            "instance/location checks are grouped into one call. Each live fix also "
            "requires a dry run; already-correct targets are skipped after pre-check."
        ),
    )
    cols[2].metric(
        "Need manual template", f"{len(plan.templates):,}",
        help=(
            "Bit 16 target/check groups needing an operator-selected receipt template. "
            "Confirm a suggested template or choose another above to include them in Batch."
        ),
    )
    cols[3].metric(
        "Manual follow-up", f"{len(plan.manual):,}",
        help=(
            "Terminal/check findings that cannot be planned automatically, such as "
            "unsupported checks or missing target identifiers. These require investigation "
            "and are not included in Fixes to run."
        ),
    )

    extra = plan.terminals_with_extra_fixes
    if extra:
        st.info(
            f"**{len(extra)}** affected terminal(s) are covered by more than one selected fix "
            "group. Shared Instance/Location groups may also cover other broken terminals "
            "shown in the coverage table."
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
                "Bit 16 is instance-level and requires an operator-selected receipt template. "
                "Choose and confirm a template above before this group can be run."
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
    terminal_limit: int = MAX_BATCH_TERMINALS,
) -> None:
    """Select terminals and fix all their broken checks using the batch workflow."""
    writes.show_notices()
    available_rows = [] if worklist is None else worklist.to_dict("records")
    if "rem_batch_loaded_rows" not in st.session_state:
        st.session_state["rem_batch_loaded_rows"] = []
        st.session_state["rem_batch_loaded_ids"] = []
        st.session_state["rem_batch_selected_fix_keys"] = []
        st.session_state["rem_batch_known_fix_keys"] = []
        st.session_state["rem_batch_scope_rows"] = {}
        st.session_state["rem_batch_editor_revision"] = 1
    rows_all = list(st.session_state["rem_batch_loaded_rows"])
    ids_all = [str(r["TERMINAL_IDENTIFIER"]) for r in rows_all]
    if available_rows:
        indexes = {terminal_id: index for index, terminal_id in enumerate(ids_all)}
        added_ids: list[str] = []
        changed = False
        for incoming in available_rows:
            row = dict(incoming)
            terminal_id = str(row["TERMINAL_IDENTIFIER"])
            current_index = indexes.get(terminal_id)
            if current_index is None:
                if len(added_ids) >= terminal_limit:
                    continue
                indexes[terminal_id] = len(rows_all)
                rows_all.append(row)
                ids_all.append(terminal_id)
                added_ids.append(terminal_id)
                changed = True
                continue

            current = rows_all[current_index]
            incoming_checks = row.get("_BATCH_CHECK_COLUMNS")
            current_checks = current.get("_BATCH_CHECK_COLUMNS")
            if incoming_checks is not None:
                checks = set(incoming_checks)
                if current_checks is not None:
                    checks.update(current_checks)
                updated = batch_upload.scope_row_to_checks(row, tuple(checks))
                if current_checks is None or set(current_checks) != checks:
                    rows_all[current_index] = updated
                    changed = True
            elif current_checks is not None or current != row:
                rows_all[current_index] = row
                changed = True

        if changed:
            st.session_state["rem_batch_loaded_rows"] = rows_all
            st.session_state["rem_batch_loaded_ids"] = ids_all
            st.session_state["rem_batch_authorise"] = False
    terminal_limit = max(terminal_limit, len(rows_all))

    planning_rows = [
        batch_upload.scope_row_to_checks(row, tuple(row["_BATCH_CHECK_COLUMNS"]))
        if row.get("_BATCH_CHECK_COLUMNS") is not None
        else row
        for row in rows_all
    ]
    planning_rows = _load_nfc_providers(conn, planning_rows)
    candidate_plan = batch.plan_batch(planning_rows)
    template_items = []
    for template in candidate_plan.templates:
        steps, _ = fixer.plan_for_row(template.covered_rows[0])
        step = next(step for step in steps if step.check_column == template.check_column)
        template_items.append(batch.BatchItem(step, template.covered_rows))
    items = (*candidate_plan.items, *template_items)
    scope_cache: dict = st.session_state["rem_batch_scope_rows"]
    shared_items = [
        item for item in items
        if item.step.target_kind in ("INSTANCE_IDENTIFIER", "LOCATION_NO")
    ]
    missing_coverage = [item for item in shared_items if item.key not in scope_cache]
    coverage_error = None
    if missing_coverage:
        targets = tuple(
            (item.step.target_kind, item.step.target_identifier, item.step.check_column)
            for item in missing_coverage
        )
        try:
            coverage_sql, coverage_params = wl.build_shared_targets_query(targets, objs)
            coverage_frame = conn.query(coverage_sql, coverage_params)
        except Exception as exc:
            coverage_frame = None
            coverage_error = f"Could not verify all terminals covered by shared targets: {exc}"
        if coverage_frame is not None:
            coverage_rows = coverage_frame.to_dict("records")
            for item in missing_coverage:
                step = item.step
                covered = [
                    row for row in coverage_rows
                    if str(row.get(step.target_kind) or "") == step.target_identifier
                    and str(row.get(step.check_column) or "") == "1"
                ]
                row_ids = {str(row.get("TERMINAL_IDENTIFIER")) for row in covered}
                for row in item.covered_rows:
                    if str(row.get("TERMINAL_IDENTIFIER")) not in row_ids:
                        covered.append(row)
                        row_ids.add(str(row.get("TERMINAL_IDENTIFIER")))
                if not covered:
                    coverage_error = (
                        f"Coverage lookup returned no broken terminals for "
                        f"{step.flag.flag_name} on {step.target_identifier}."
                    )
                    covered = list(item.covered_rows)
                scope_cache[item.key] = tuple(
                    sorted(covered, key=lambda row: str(row.get("TERMINAL_IDENTIFIER")))
                )
        st.session_state["rem_batch_scope_rows"] = scope_cache

    covered_items = tuple(
        replace(item, covered_rows=scope_cache.get(item.key, item.covered_rows))
        for item in items
    )
    executable_items = [item for item in covered_items if not item.step.needs_template]
    pending_templates = []
    receipt_items = [item for item in covered_items if item.step.needs_template]
    if receipt_items:
        with st.expander(
            f"Bit 16 receipt templates ({len(receipt_items)} instances)", expanded=True
        ):
            for item in receipt_items:
                with st.container(border=True):
                    st.markdown(f"**Instance {item.step.target_identifier}**")
                    chosen = _choose_template(
                        conn,
                        item.covered_rows[0],
                        item.step,
                        candidate_rows=item.covered_rows,
                        batch_selection=True,
                    )
                if chosen.config_value is not None:
                    executable_items.append(replace(item, step=chosen))
                else:
                    pending_templates.append(batch.TemplateItem(
                        item.step.check_column, item.step.flag.flag_name, item.step.target_kind,
                        item.step.target_identifier, item.covered_rows,
                    ))
    candidate_plan = replace(
        candidate_plan, items=tuple(executable_items), templates=tuple(pending_templates)
    )

    known_keys = set(st.session_state["rem_batch_known_fix_keys"])
    current_keys = {item.key for item in candidate_plan.items}
    selected_keys = set(st.session_state["rem_batch_selected_fix_keys"])
    new_fix_keys = {
        item.key for item in candidate_plan.items
        if item.key not in known_keys and (available_rows or item.step.needs_template)
    }
    if new_fix_keys:
        selected_keys.update(new_fix_keys)
        st.session_state["rem_batch_editor_revision"] += 1
        st.session_state["rem_batch_authorise"] = False
    st.session_state["rem_batch_known_fix_keys"] = list(known_keys | current_keys)
    selected_keys.intersection_update(current_keys)
    st.session_state["rem_batch_selected_fix_keys"] = list(selected_keys)

    scope_labels = {
        "TERMINAL_IDENTIFIER": "Terminal",
        "INSTANCE_IDENTIFIER": "Instance",
        "LOCATION_NO": "Location",
    }

    def joined_coverage_values(item: batch.BatchItem, column: str) -> str:
        values = sorted(
            {
                str(row.get(column)).strip()
                for row in item.covered_rows
                if row.get(column) is not None and str(row.get(column)).strip()
            }
        )
        return ", ".join(values) if values else "—"

    selection_rows = []
    selection_identity_to_key = {}
    for item in candidate_plan.items:
        step = item.step
        scope = scope_labels.get(step.target_kind, step.target_kind)
        identity = (
            step.definition.bit,
            step.flag.flag_name,
            scope,
            step.target_identifier,
            step.display_value,
        )
        selection_identity_to_key[identity] = item.key
        selection_rows.append(
            {
                "Include": item.key in selected_keys,
                "Bit": step.definition.bit,
                "Check": step.flag.flag_name,
                "Target scope": scope,
                "Target": step.target_identifier,
                "Value": step.display_value or "—",
                "Terminals covered": len(item.covered_rows),
                "Country": joined_coverage_values(item, "COUNTRY_NAME"),
                "Region": joined_coverage_values(item, "REGION"),
                "Terminal brand": joined_coverage_values(item, "TERMINAL_BRAND_NAME"),
                "Acquirer": joined_coverage_values(item, "ACQUIRER_NAME"),
            }
        )
    selection_table = pd.DataFrame(
        selection_rows,
        columns=[
            "Include", "Bit", "Check", "Target scope", "Target", "Value",
            "Terminals covered", "Country", "Region", "Terminal brand", "Acquirer",
        ],
    )
    with st.form("rem_batch_selection_form"):
        st.caption(
            "Each checkbox selects one procedure call at its actual scope. Instance and "
            "Location rows include every currently broken terminal that the shared target "
            "call can affect."
        )
        edited = st.data_editor(
            selection_table,
            column_config={"Include": st.column_config.CheckboxColumn("Include")},
            disabled=[column for column in selection_table.columns if column != "Include"],
            hide_index=True,
            use_container_width=True,
            key=f"rem_batch_selection_{st.session_state['rem_batch_editor_revision']}",
        )
        controls = st.columns(2)
        apply_selection = controls[0].form_submit_button(
            "Apply selection",
            help=(
                "Save the checked target-scope fix groups. Does not load targets or execute "
                "fixes."
            ),
        )
        clear_table = controls[1].form_submit_button(
            "Empty table",
            help=(
                "Clear loaded terminals, fix groups and live authorisation. "
                "Filters stay unchanged."
            ),
        )
    if clear_table:
        for item in receipt_items:
            terminal_id = str(item.covered_rows[0]["TERMINAL_IDENTIFIER"])
            for key in (
                f"rem_tpl_{terminal_id}",
                f"rem_tpl_source_{terminal_id}",
                f"rem_tpl_custom_{terminal_id}",
                f"rem_tpl_selection_{terminal_id}",
            ):
                st.session_state.pop(key, None)
            st.session_state.pop(
                f"rem_batch_template_choice_{item.step.target_identifier}", None
            )
            for key in list(st.session_state):
                if key.startswith(
                    (
                        f"rem_tpl_ok_{terminal_id}_",
                        f"rem_tpl_rule_ok_{terminal_id}_",
                        f"rem_tpl_confirm_{terminal_id}_",
                    )
                ):
                    del st.session_state[key]
        st.session_state.pop("rem_batch_rehearsal", None)
        st.session_state["rem_batch_loaded_rows"] = []
        st.session_state["rem_batch_loaded_ids"] = []
        st.session_state["rem_batch_selected_fix_keys"] = []
        st.session_state["rem_batch_known_fix_keys"] = []
        st.session_state["rem_batch_scope_rows"] = {}
        st.session_state["rem_batch_editor_revision"] += 1
        st.session_state["rem_batch_authorise"] = False
        st.rerun()
    if apply_selection:
        checked_keys = []
        for _, row in edited.iterrows():
            identity = (
                int(row["Bit"]),
                str(row["Check"]),
                str(row["Target scope"]),
                str(row["Target"]),
                "" if str(row["Value"]) == "—" else str(row["Value"]),
            )
            key = selection_identity_to_key.get(identity)
            if bool(row["Include"]) and key is not None:
                checked_keys.append(key)
        st.session_state["rem_batch_selected_fix_keys"] = checked_keys
        st.session_state["rem_batch_editor_revision"] += 1
        st.session_state["rem_batch_authorise"] = False
        st.rerun()

    selected_items = tuple(item for item in candidate_plan.items if item.key in selected_keys)
    affected_rows_by_id = {}
    fixes_per_terminal = {}
    for item in selected_items:
        for row in item.covered_rows:
            terminal_id = str(row.get("TERMINAL_IDENTIFIER"))
            affected_rows_by_id[terminal_id] = row
            fixes_per_terminal[terminal_id] = fixes_per_terminal.get(terminal_id, 0) + 1
    plan = replace(
        candidate_plan,
        items=selected_items,
        selected_terminals=tuple(sorted(affected_rows_by_id)),
        fixes_per_terminal=fixes_per_terminal,
    )
    if plan.items:
        with st.expander("Terminals covered by the selected fix groups"):
            coverage_table = [
                {
                    "Bit": item.step.definition.bit,
                    "Check": item.step.flag.flag_name,
                    "Target scope": scope_labels.get(item.step.target_kind, item.step.target_kind),
                    "Target": item.step.target_identifier,
                    "Terminal": str(row.get("TERMINAL_IDENTIFIER")),
                    "Location": row.get("LOCATION_NO"),
                    "Instance": row.get("INSTANCE_IDENTIFIER"),
                }
                for item in plan.items
                for row in item.covered_rows
            ]
            st.dataframe(pd.DataFrame(coverage_table), use_container_width=True, hide_index=True)
    _render_plan(plan)
    if plan.selected_terminals:
        with st.expander("Fix history for terminals covered by this batch"):
            history_sql, history_params = fixlog.build_batch_history_query(
                objs, plan.selected_terminals
            )
            history, history_error = _query(conn, history_sql, history_params)
            if history_error:
                st.caption(f"History unavailable: {history_error}")
            elif history is None or history.empty:
                st.caption("No attempts logged yet for the covered terminals.")
            else:
                st.caption("All audit rows for covered terminals, including previous runs.")
                st.dataframe(history, use_container_width=True, hide_index=True)
    agent_panel.render_account_agent(conn, context_key="batch")

    if coverage_error:
        st.error(coverage_error)
        return

    if not plan.has_work:
        st.info(
            "Check at least one automatic fix group to plan a Batch. "
            "See manual/template items below."
        )
        return

    over_cap = (
        len(plan.selected_terminals) > terminal_limit or len(plan.items) > MAX_BATCH_ITEMS
    )
    if over_cap:
        st.error(
            f"This batch is too large ({len(plan.selected_terminals)} terminals, "
            f"{len(plan.items)} fixes). The cap is {terminal_limit} terminals / "
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

    rehearsal_signature = (
        id(sql_conn), environment, sql_conn.server, sql_conn.database,
        tuple((item.key, item.terminals) for item in plan.items),
    )
    rehearsal = st.session_state.get("rem_batch_rehearsal")
    rehearsal_ready = bool(
        rehearsal
        and rehearsal[0] == rehearsal_signature
        and time.time() - rehearsal[1] <= fixer.FRESHNESS_S
        and rehearsal[2]
    )
    change_ref = st.text_input(
        "Change / CAB reference",
        key="rem_batch_change_ref",
        max_chars=120,
        placeholder="e.g. CHG0043215 or batch reference",
        help=(
            "Recorded in CHANGE_REF on every APP_FIX_LOG row for this Batch, including "
            "pre-check outcomes, dry runs and live attempts, and on eligible registry entries. "
            "Required for Apply live on PROD; optional for other runs."
        ),
    ).strip()
    authorised = False
    if apply_live:
        where = f"{environment} · {sql_conn.server}/{sql_conn.database}"
        (st.warning if is_prod else st.info)(
            f"**{where}{' — PRODUCTION' if is_prod else ''}.** Apply live runs the gated "
            "pre-check → dry run → apply → verify cycle on every fix below."
        )
        with st.expander(f"Conditional live statements for selected fixes ({len(plan.items)})"):
            st.caption(
                "Every target gets a fresh pre-check. Already OK and NOT_FOUND outcomes "
                "are logged and skipped without calling the procedure. Targets needing a "
                "fix get another dry run before the live gates permit Apply."
            )
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
        if not rehearsal_ready:
            blockers.append("run a successful rehearsal of this exact selection within 15 minutes")
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
            rehearsal_ready
            and
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
        if not apply_live:
            successful = (
                any(result.outcome == "REHEARSED" for result in results)
                and all(result.outcome in (
                    "REHEARSED", "SKIPPED_ALREADY_OK", "NOT_FOUND"
                ) for result in results)
            )
            st.session_state["rem_batch_rehearsal"] = (
                rehearsal_signature, time.time(), successful,
            )
        else:
            st.session_state.pop("rem_batch_rehearsal", None)
        st.rerun()

    _render_results(st.session_state.get(_RESULTS_KEY) or [])
