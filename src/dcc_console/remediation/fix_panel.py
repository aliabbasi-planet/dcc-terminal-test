"""Streamlit panel: fix one selected worklist terminal, one check at a time.

Per check: **Pre-check** (live read) → **Dry run** (simulation, rolled back) →
**Apply live** (DEV/UAT only this phase, operators only, console Mode armed, exact
EXEC reviewed) → automatic **verify** (a fresh live read) → logged to the shared
``APP_FIX_LOG`` → **Roll back** if needed. Every rule lives in :mod:`.fixer`; this
module renders the state and records each outcome. Nothing here can issue a live
call that :func:`fixer.live_gate` refuses, and :func:`fixer.run_step` refuses PROD
writes on its own as well.
"""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from ..config import configdownload_version_text
from ..execution import TestResult, apply_rollback, build_call
from ..state import add_result
from . import RemediationObjects, agent_panel, fixer, fixlog, suggest, writes
from . import worklist as wl
from .sf_connection import SnowflakeConnection

SELECTED_KEY = "rem_selected_terminal"
_WORK_KEY = "rem_fix_work"
_OPERATOR_KEY = "rem_operator"

_KIND_LABELS = {
    "INSTANCE_IDENTIFIER": "instance",
    "LOCATION_NO": "location",
    "TERMINAL_IDENTIFIER": "terminal",
}
# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _state(key: str, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def _query(conn: SnowflakeConnection, sql: str, params: list | None = None):
    try:
        return conn.query(sql, params), None
    except Exception as exc:  # surface Snowflake errors in the panel, never crash it
        return None, str(exc)


def _kind(step: fixer.FixStep) -> str:
    return _KIND_LABELS.get(step.target_kind, step.target_kind)


# --------------------------------------------------------------------------- #
# operator identity + fix-log writes
# --------------------------------------------------------------------------- #


def operator_status(conn: SnowflakeConnection, objs: RemediationObjects):
    """``(user, is_operator, can_prod, error)``, cached per Snowflake session + schema."""
    cache_key = (id(conn), objs.shared_fqn)
    cached = st.session_state.get(_OPERATOR_KEY)
    if cached and cached[0] == cache_key:
        return cached[1], cached[2], cached[3], cached[4]
    frame, error = _query(conn, fixlog.build_operator_check_query(objs))
    if error or frame is None or frame.empty:
        status = (conn.settings.user, False, False, error or "The operator check returned nothing.")
    else:
        row = frame.iloc[0]
        status = (
            str(row["USER_NAME"]),
            int(row["IS_OPERATOR"] or 0) > 0,
            int(row.get("CAN_PROD") or 0) > 0,
            None,
        )
    st.session_state[_OPERATOR_KEY] = (cache_key, *status)
    return status


# --------------------------------------------------------------------------- #
# per-step inputs: Bit 16 template, UAT/DEV substitute target, coverage
# --------------------------------------------------------------------------- #


def _load_nfc_providers(conn: SnowflakeConnection, rows: list[dict]) -> list[dict]:
    affected = [
        row for row in rows if any(str(row.get(check)) == "1" for check in fixer.NFC_CHECKS)
    ]
    if not affected:
        return rows
    sql, params = suggest.build_nfc_provider_lookup_query(affected)
    frame, error = _query(conn, sql, params) if params else (None, None)
    providers = {}
    if error:
        st.warning(f"Could not load NFC provider context; automatic NFC fixes are blocked: {error}")
    if frame is not None and not frame.empty:
        providers = {
            str(record["INSTANCE_IDENTIFIER"]): record["NFC_PROVIDER_CODE"]
            if int(record["NFC_PROVIDER_COUNT"] or 0) == 1 else None
            for record in frame.to_dict("records")
        }
    return [
        {**row, "DCCPROVIDER_CODE": providers.get(str(row.get("INSTANCE_IDENTIFIER")))}
        if any(str(row.get(check)) == "1" for check in fixer.NFC_CHECKS) else row
        for row in rows
    ]


def _choose_template(
    conn: SnowflakeConnection,
    row: dict,
    step: fixer.FixStep,
    *,
    candidate_rows: list[dict] | None = None,
    batch_selection: bool = False,
) -> fixer.FixStep:
    tid = str(row["TERMINAL_IDENTIFIER"])
    source_rows = candidate_rows or [row]
    provider_sql, provider_params = suggest.build_provider_lookup_query(source_rows)
    provider_df, provider_error = _query(conn, provider_sql, provider_params)
    if provider_error:
        st.warning(f"Could not load DCC providers from the maintenance source: {provider_error}")
        provider_df = None
    providers = {}
    if provider_df is not None and not provider_df.empty:
        providers = {
            str(provider_row["TERMINAL_IDENTIFIER"]): (
                str(provider_row["DCC_PROVIDER_CODE"])
                if int(provider_row["DCC_PROVIDER_COUNT"] or 0) == 1
                else None
            )
            for _, provider_row in provider_df.iterrows()
        }
    rule_rows = [
        {**candidate, "DCCPROVIDER_CODE": providers.get(str(candidate["TERMINAL_IDENTIFIER"]))}
        for candidate in source_rows
    ]
    provider_values = {
        str(rule_row.get("DCCPROVIDER_CODE") or "") for rule_row in rule_rows
    }
    if len(provider_values) > 1:
        st.warning(
            "This instance covers different DCC providers. Only templates allowed for every "
            "provider and terminal brand can be selected."
        )
    if any(not rule_row.get("DCCPROVIDER_CODE") for rule_row in rule_rows):
        st.warning(
            "A single DCC provider could not be determined for every covered terminal. "
            "Workbook templates will not be offered for ambiguous or missing providers."
        )
    rules = suggest.common_allowed_templates(rule_rows)
    provider = str(rule_rows[0].get("DCCPROVIDER_CODE") or "unknown DCC provider")
    brand = str(row.get("TERMINAL_BRAND_NAME") or "unknown brand")
    if not rules:
        st.warning(
            f"No workbook rule is defined for DCC provider {provider} / brand {brand} "
            "across this instance. "
            "Enter a verified custom exception to continue."
        )

    if rules:
        st.caption(f"Workbook rule for DCC provider **{provider}** and brand **{brand}**.")
        if len(rules) > 1:
            st.info(
                "The workbook allows multiple templates. The first is prefilled; "
                "review or change it."
            )
        source = st.radio(
            "Template source",
            ("Workbook list", "Custom exception"),
            horizontal=True,
            key=f"rem_tpl_source_{tid}",
        )
    else:
        source = "Custom exception"

    if source == "Workbook list":
        options = list(rules)
        choice_key = f"rem_tpl_{tid}"
        existing_choice = st.session_state.get(choice_key)
        choice_index = options.index(existing_choice) if existing_choice in options else 0
        choice = st.selectbox(
            "DCC receipt template",
            options,
            index=choice_index,
            key=choice_key,
        )
    else:
        st.warning(
            "Custom templates are outside the workbook allowlist. Verify the name before use."
        )
        choice = st.text_input(
            "Custom DCC receipt template",
            key=f"rem_tpl_custom_{tid}",
            placeholder="Enter the exact template name",
        ).strip()

    confirmation_key = f"rem_tpl_confirm_{tid}_{source}_{choice}"
    selection_key = f"rem_tpl_selection_{tid}"
    selection = (source, choice)
    previous_selection = st.session_state.get(selection_key)
    if previous_selection is not None and previous_selection != selection:
        previous_source, previous_choice = previous_selection
        st.session_state.pop(
            f"rem_tpl_confirm_{tid}_{previous_source}_{previous_choice}", None
        )
        st.session_state.pop(confirmation_key, None)
    elif batch_selection and source == "Workbook list" and choice:
        st.session_state.setdefault(confirmation_key, True)
    st.session_state[selection_key] = selection

    if batch_selection:
        previous_key = f"rem_batch_template_choice_{step.target_identifier}"
        if previous_key in st.session_state and st.session_state[previous_key] != selection:
            st.session_state.pop("rem_batch_rehearsal", None)
            st.session_state["rem_batch_authorise"] = False
        st.session_state[previous_key] = selection
    if not choice:
        return step
    if source == "Custom exception":
        confirmation_label = (
            f"I confirm custom template **{choice}** is a valid exception for instance "
            f"{step.target_identifier}"
        )
    else:
        confirmation_label = (
            f"Use workbook-approved template **{choice}** for instance "
            f"{step.target_identifier}"
        )
    confirmed = st.checkbox(
        confirmation_label,
        key=confirmation_key,
    )
    return step.with_template(choice) if confirmed else step


def _choose_target(step: fixer.FixStep, environment: str, tid: str) -> fixer.FixStep:
    if not fixer.substitute_allowed(environment):
        return step
    use = st.checkbox(
        f"Rehearse on a different {_kind(step)} in {environment}",
        key=f"rem_sub_{tid}_{step.check_column}",
        help="The worklist is PROD data. Use this when its target does not exist here.",
    )
    if not use:
        return step
    value = st.text_input(
        f"{environment} {_kind(step)} identifier to rehearse on",
        key=f"rem_sub_id_{tid}_{step.check_column}",
    )
    if not value.strip():
        st.caption("Enter the identifier to rehearse on.")
        return step
    return step.with_target(value)


def _covered_rows(
    conn: SnowflakeConnection, objs: RemediationObjects, row: dict, step: fixer.FixStep
) -> list[dict]:
    """Worklist terminals one call covers (the selected row first)."""
    if step.is_substitute:
        return [row]
    sql = wl.build_shared_target_query(step.target_kind, step.check_column, objs)
    frame, error = _query(conn, sql, [step.worklist_target])
    if error or frame is None or frame.empty:
        return [row]
    tid = str(row["TERMINAL_IDENTIFIER"])
    others = [r for r in frame.to_dict("records") if str(r.get("TERMINAL_IDENTIFIER")) != tid]
    return [row, *others]


def _render_coverage(step: fixer.FixStep, covered: list[dict]) -> None:
    if step.is_substitute:
        st.caption(
            f"Rehearsal on {_kind(step)} {step.target_identifier} (worklist target "
            f"{step.worklist_target}). Logged as a rehearsal; it never changes the PROD worklist."
        )
        return
    others = len(covered) - 1
    if others <= 0:
        return
    st.caption(
        f"This call changes the whole {_kind(step)} {step.target_identifier}, so it also "
        f"covers **{others}** other listed terminal(s) broken on this check. Each is logged."
    )
    models = sorted(
        {str(r["TERMINAL_MODEL_NAME"]) for r in covered if r.get("TERMINAL_MODEL_NAME")}
    )
    if step.needs_template and len(models) > 1:
        st.warning(
            f"This instance has {len(models)} terminal models ({', '.join(models)}); one "
            "receipt template will apply to all of them."
        )
    frame = pd.DataFrame(covered)
    columns = [
        c
        for c in (
            "TERMINAL_IDENTIFIER",
            "MERCHANT_NAME",
            "LOCATION_NAME",
            "TERMINAL_MODEL_NAME",
            "REMEDIATION_STATE",
        )
        if c in frame.columns
    ]
    with st.expander(f"Terminals covered by this call ({len(covered)})"):
        st.dataframe(frame[columns], hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------- #
# the panel
# --------------------------------------------------------------------------- #


def _step_label(step: fixer.FixStep) -> str:
    value = step.display_value or "template to choose"
    label = (
        f"{step.flag.flag_name} — Bit {step.definition.bit} on {_kind(step)} "
        f"{step.target_identifier} ({value})"
    )
    return f"{label} · fixed, awaiting refresh" if step.resolved else label


def _describe_state(value: object, bit: int) -> str:
    if bit == 2:
        return configdownload_version_text(value)
    return "(empty)" if value in (None, "") else str(value)


def _render_progress(work: dict, bit: int) -> None:
    pre: fixer.Precheck | None = work.get("pre")
    dry: fixer.DryRun | None = work.get("dry")
    live: TestResult | None = work.get("live")
    post: fixer.Precheck | None = work.get("post")
    if pre is not None:
        text = f"**Pre-check:** {pre.status} — {pre.detail}"
        if pre.current is not None:
            text += f"  \nCurrent value: `{_describe_state(pre.current, bit)}`"
        {
            fixer.NEEDS_FIX: st.info,
            fixer.ALREADY_OK: st.success,
        }.get(pre.status, st.warning)(text)
    if dry is not None:
        (st.success if dry.status == "PASS" else st.error)(
            f"**Dry run:** {dry.status} (rolled back; nothing changed)."
        )
    if live is not None:
        verified = post is not None and post.status == fixer.ALREADY_OK
        if live.error:
            st.error(f"**Live:** {live.status} — {live.error} ({live.transaction})")
        elif verified:
            st.success(
                f"**Live:** applied and **verified** by a fresh SQL-Server read "
                f"({live.transaction}). Registered in the durable fix registry — it survives "
                "the daily refresh until a rollback removes it."
            )
        else:
            detail = post.detail if post is not None else "no verification read"
            st.warning(
                f"**Live:** applied, but a fresh read did **not** confirm the fix — {detail}. "
                "It was **not** registered as fixed. Investigate, and roll back if needed."
            )
        for entry in live.rollback_log:
            state = "succeeded" if entry.get("ok") else f"failed: {entry.get('error')}"
            st.caption(f"{str(entry.get('trigger', '')).capitalize()} rollback {state}.")


def _render_history(conn: SnowflakeConnection, objs: RemediationObjects, tid: str) -> None:
    with st.expander("Fix history for this terminal"):
        frame, error = _query(conn, fixlog.build_terminal_history_query(objs), [tid])
        if error:
            st.caption(f"History unavailable: {error}")
        elif frame is None or frame.empty:
            st.caption("No attempts logged yet.")
        else:
            st.dataframe(frame, hide_index=True, use_container_width=True)


def render_fix_panel(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    row: dict,
    *,
    armed_live: bool,
    next_terminal: str | None,
) -> None:
    """Render the one-by-one fixer for a single worklist row."""
    tid = str(row["TERMINAL_IDENTIFIER"])
    st.subheader(f"Fix terminal {tid}")
    facts = [
        str(row[c])
        for c in (
            "MERCHANT_NAME",
            "LOCATION_NAME",
            "COUNTRY_NAME",
            "TERMINAL_BRAND_NAME",
            "TERMINAL_MODEL_NAME",
        )
        if row.get(c)
    ]
    st.caption(" · ".join([*facts, f"state {row.get('REMEDIATION_STATE')}"]))
    writes.show_notices()
    agent_panel.render_terminal_agent(conn, row)

    sql_conn = st.session_state.get("connection")
    environment = str(st.session_state.get("connected_env") or "").upper()
    login = str(st.session_state.get("connected_login") or "")
    if sql_conn is None or not environment:
        st.info("Connect to SQL Server in the sidebar to pre-check and fix this terminal.")
        return

    user, is_operator, can_prod, operator_error = operator_status(conn, objs)
    is_prod = environment == fixer.PROD_ENVIRONMENT
    where = f"{environment} · {sql_conn.server}/{sql_conn.database}"
    if is_prod:
        st.warning(
            f"**{where} — PRODUCTION.** Live changes affect real terminals. A PROD fix needs a "
            "PROD-approved operator (CAN_PROD), a change/CAB reference, the console Mode armed, "
            "and a fresh pre-check + dry run of the exact target."
        )
    elif environment in fixer.LIVE_ENVIRONMENTS:
        st.info(
            f"**{where}** — rehearsal environment: live fixes are allowed for operators. The "
            "worklist is PROD data; if a target does not exist here, rehearse on a substitute."
        )
    else:
        st.warning(
            f"**{where}** — pre-check and dry run only. Live fixes on {environment} are not "
            "enabled in this phase."
        )
    who = f"Snowflake user **{user}** — " + (
        "live-fix operator."
        if is_operator
        else "not on the operator list (FIX_OPERATORS): pre-check and dry run only."
    )
    if is_operator:
        who += " PROD-approved (CAN_PROD)." if can_prod else " Not PROD-approved (UAT/DEV only)."
    if operator_error:
        who += f" Operator check failed: {operator_error}"
    st.caption(who)

    row = _load_nfc_providers(conn, [row])[0]
    steps, blocked = fixer.plan_for_row(row)
    for item in blocked:
        st.caption(f"Manual follow-up — **{item.flag.flag_name}**: {item.reason}")
    if not steps:
        st.info("No broken check on this terminal can be fixed by the procedure.")
        _render_history(conn, objs, tid)
        _render_next(next_terminal)
        return

    index = st.radio(
        "Check to fix",
        range(len(steps)),
        format_func=lambda i: _step_label(steps[i]),
        key=f"rem_step_{tid}",
    )
    step = steps[index]
    if step.resolved:
        st.success(
            "This check is already fixed or confirmed on PROD for the current snapshot; it "
            "clears from the worklist on the next refresh."
        )
    if step.needs_template:
        step = _choose_template(conn, row, step)
    step = _choose_target(step, environment, tid)

    covered = _covered_rows(conn, objs, row, step)
    _render_coverage(step, covered)

    change_ref = ""
    if is_prod:
        change_ref = st.text_input(
            "Change / CAB reference (required for a PROD fix)",
            key=f"rem_change_ref_{tid}",
            placeholder="e.g. CHG0043215",
            help="Recorded on every PROD fix-log row and in the durable fix registry.",
        ).strip()

    work = _state(_WORK_KEY, {}).setdefault((environment, tid, step.check_column), {})
    correlation = work.setdefault("correlation", fixer.new_correlation_id())
    pre: fixer.Precheck | None = work.get("pre")
    dry: fixer.DryRun | None = work.get("dry")
    live: TestResult | None = work.get("live")

    last_frame, _ = _query(
        conn, fixlog.build_last_live_outcome_query(objs), [tid, step.check_column, environment]
    )
    last_live = None if last_frame is None or last_frame.empty else last_frame.iloc[0].to_dict()
    gate = fixer.live_gate(
        step=step,
        environment=environment,
        is_operator=is_operator,
        armed_live=armed_live,
        pre=pre,
        dry_run=dry,
        last_live=last_live,
        source_last_altered=row.get("SOURCE_LAST_ALTERED"),
        can_prod=can_prod,
        change_ref=change_ref,
    )

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
    )
    ready_value = step.config_value is not None
    actions = st.columns(4)

    # 1 · live pre-check -----------------------------------------------------------
    if actions[0].button(
        "1 · Pre-check", key=f"rem_pre_{tid}", use_container_width=True, disabled=not ready_value
    ):
        with st.spinner("Reading the live target…"):
            pre = fixer.precheck(sql_conn, step, environment)
        work["pre"] = pre
        recorded = fixer.precheck_outcome(pre)
        if recorded is not None:
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
                summary=f"pre-check of {tid}",
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
                    summary=f"pre-check of {tid}",
                )
        st.rerun()

    # 2 · dry run ----------------------------------------------------------------
    if actions[1].button(
        "2 · Dry run", key=f"rem_dry_{tid}", use_container_width=True, disabled=not ready_value
    ):
        try:
            with st.spinner("Dry run (the transaction is rolled back)…"):
                result = fixer.run_step(
                    sql_conn,
                    step,
                    simulation=True,
                    environment=environment,
                    login=login,
                    correlation_id=correlation,
                )
        except Exception as exc:
            writes.notice("error", f"The dry run did not start: {exc}")
        else:
            add_result(result)
            work["dry"] = fixer.DryRun(step.signature(environment), result.status, time.time())
            recorded = fixer.dry_run_outcome(result)
            writes.write_log(
                conn,
                objs,
                fixer.log_records(
                    **log_context,
                    outcome=recorded.outcome,
                    mode=recorded.mode,
                    verified=recorded.verified,
                    result=result,
                    pre=pre,
                    error=recorded.error,
                ),
                summary=f"dry run on {tid}",
            )
        st.rerun()

    # 3 · apply live ----------------------------------------------------------------
    reviewed = False
    if gate.allowed:
        _, _, rendered = build_call(
            step.definition, step.target_identifier, step.config_value, simulation=False
        )
        st.markdown("**Exact statement Apply live will run:**")
        st.code(rendered, language="sql")
        reviewed = st.checkbox(
            f"I have reviewed this exact EXEC and want to apply it live on {environment}",
            key=f"rem_reviewed_{tid}_{abs(hash(step.signature(environment)))}",
        )
    elif ready_value:
        with st.expander("Why **Apply live** is disabled"):
            for reason in gate.reasons:
                st.markdown(f"- {reason}")
    if actions[2].button(
        "3 · Apply live",
        key=f"rem_apply_{tid}",
        type="primary",
        use_container_width=True,
        disabled=not (gate.allowed and reviewed),
    ):
        try:
            with st.spinner(f"Applying live on {environment} and verifying…"):
                result = fixer.run_step(
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
            writes.notice("error", f"The live call did not run: {exc}")
        else:
            add_result(result)
            work.update(live=result, post=post)
            work.pop("dry", None)  # any further live call needs a fresh dry run
            recorded = fixer.live_outcome(result, post)
            writes.write_log(
                conn,
                objs,
                fixer.log_records(
                    **log_context,
                    outcome=recorded.outcome,
                    mode=recorded.mode,
                    verified=recorded.verified,
                    result=result,
                    pre=pre,
                    post=post,
                    error=recorded.error,
                ),
                summary=f"live fix on {tid}",
                keep=result if recorded.outcome == "APPLIED" and recorded.verified else None,
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
                        result=result,
                    ),
                    summary=f"live fix on {tid}",
                )
        st.rerun()

    # rollback -------------------------------------------------------------------------
    can_roll_back = (
        live is not None
        and live.is_live
        and live.rollback_available
        and live.environment.upper() == environment
    )
    if actions[3].button(
        "↩ Roll back",
        key=f"rem_rollback_{tid}",
        use_container_width=True,
        disabled=not can_roll_back,
    ):
        with st.spinner("Rolling back and re-reading the target…"):
            outcome = apply_rollback(sql_conn, step.definition, live, trigger="manual")
            post = fixer.precheck(sql_conn, step, environment)
        work["post"] = post
        work.pop("pre", None)
        reverted = fixer.rollback_confirmed(post)
        recorded = fixer.rollback_outcome(outcome.ok, outcome.error, reverted)
        writes.write_log(
            conn,
            objs,
            fixer.log_records(
                **log_context,
                outcome=recorded.outcome,
                mode=recorded.mode,
                verified=recorded.verified,
                result=live,
                post=post,
                error=recorded.error,
            ),
            summary=f"rollback on {tid}",
        )
        if outcome.ok and reverted:
            writes.deregister_fix(
                conn,
                objs,
                environment=environment,
                terminals=[str(r.get("TERMINAL_IDENTIFIER")) for r in covered],
                check_column=step.check_column,
                summary=f"terminal(s) on {tid}'s call",
            )
        if not outcome.ok:
            writes.notice("error", f"Rollback failed: {outcome.error}")
        elif reverted:
            writes.notice(
                "success",
                f"Rolled back and confirmed reverted on {environment} by a fresh read; the fix "
                "registry entry was removed, so the check re-opens on the worklist.",
            )
        else:
            writes.notice(
                "warning",
                "The rollback ran but a fresh read did not confirm the change reverted — "
                "investigate. The fix registry entry was kept so the worklist is not misled.",
            )
        st.rerun()

    _render_progress(work, step.definition.bit)
    _render_history(conn, objs, tid)
    _render_next(next_terminal)


def _render_next(next_terminal: str | None) -> None:
    if next_terminal and st.button(f"Next terminal → {next_terminal}", key="rem_next_terminal"):
        st.session_state[SELECTED_KEY] = next_terminal
        st.rerun()
