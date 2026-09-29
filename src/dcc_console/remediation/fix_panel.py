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
from ..journal import get_journal
from ..state import add_result
from . import RemediationObjects, fixer, fixlog, suggest
from . import worklist as wl
from .sf_connection import SnowflakeConnection

SELECTED_KEY = "rem_selected_terminal"
_WORK_KEY = "rem_fix_work"
_PENDING_KEY = "rem_pending_logs"
_OPERATOR_KEY = "rem_operator"
_TEMPLATES_KEY = "rem_template_cache"
_NOTICE_KEY = "rem_fix_notice"

_KIND_LABELS = {
    "INSTANCE_IDENTIFIER": "instance",
    "LOCATION_NO": "location",
    "TERMINAL_IDENTIFIER": "terminal",
}
# A suggestion is "strong" when most peers agree and there are enough of them.
_STRONG_SHARE = 0.9
_STRONG_PEERS = 20


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _state(key: str, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def _notice(level: str, message: str) -> None:
    """Queue a message that survives ``st.rerun()``."""
    _state(_NOTICE_KEY, []).append((level, message))


def _show_notices() -> None:
    for level, message in st.session_state.pop(_NOTICE_KEY, []):
        getattr(st, level)(message)


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
    """``(user, is_operator, error)``, cached per Snowflake session and shared schema."""
    cache_key = (id(conn), objs.shared_fqn)
    cached = st.session_state.get(_OPERATOR_KEY)
    if cached and cached[0] == cache_key:
        return cached[1], cached[2], cached[3]
    frame, error = _query(conn, fixlog.build_operator_check_query(objs))
    if error or frame is None or frame.empty:
        status = (conn.settings.user, False, error or "The operator check returned nothing.")
    else:
        row = frame.iloc[0]
        status = (str(row["USER_NAME"]), int(row["IS_OPERATOR"] or 0) > 0, None)
    st.session_state[_OPERATOR_KEY] = (cache_key, *status)
    return status


def _write_log(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    records: list[dict],
    *,
    summary: str,
    keep: TestResult | None = None,
) -> bool:
    """Write fix-log rows; on failure queue them for retry (never lose an audit row).

    ``keep`` is a verified live fix: once its rows are written it leaves crash recovery.
    """
    if not records:
        return True
    try:
        sql, params = fixlog.build_insert_many(records, objs)
    except ValueError as exc:  # a malformed record is a bug — surface it, don't queue it
        _notice("error", f"Fix-log rows for {summary} were rejected: {exc}")
        return False
    try:
        conn.execute(sql, params)
    except Exception as exc:
        _state(_PENDING_KEY, []).append(
            {
                "sql": sql,
                "params": params,
                "summary": summary,
                "journal_id": keep.journal_id if keep is not None else None,
                "error": str(exc),
            }
        )
        _notice(
            "error",
            f"Could not write the fix log for {summary}: {exc}. The rows are queued — use "
            "**Retry fix-log writes** at the top of the tab.",
        )
        return False
    if keep is not None:
        fixer.keep_fix(keep)
    return True


def render_pending_logs(conn: SnowflakeConnection) -> None:
    """Banner + retry for fix-log rows that could not be written."""
    pending = st.session_state.get(_PENDING_KEY) or []
    if not pending:
        return
    st.error(
        f"**{len(pending)} fix-log write(s) are queued.** Until they are written, those "
        "attempts are missing from the shared audit trail, and a live fix among them stays "
        "listed in crash recovery."
    )
    if st.button("Retry fix-log writes", key="rem_retry_logs", type="primary"):
        remaining = []
        for item in pending:
            try:
                conn.execute(item["sql"], item["params"])
            except Exception as exc:
                remaining.append({**item, "error": str(exc)})
                continue
            if item.get("journal_id") is not None:
                get_journal().mark_kept(item["journal_id"])
        st.session_state[_PENDING_KEY] = remaining
        written = len(pending) - len(remaining)
        _notice(
            "success" if not remaining else "warning",
            f"Wrote {written} queued fix-log write(s); {len(remaining)} still queued.",
        )
        st.rerun()
    with st.expander("Queued fix-log writes"):
        st.table([{"what": item["summary"], "last error": item["error"]} for item in pending])


# --------------------------------------------------------------------------- #
# per-step inputs: Bit 16 template, UAT/DEV substitute target, coverage
# --------------------------------------------------------------------------- #


def _load_templates(conn: SnowflakeConnection, row: dict):
    label, ranked, error = "", [], None
    for query in suggest.build_template_suggestion_queries(row):
        frame, error = _query(conn, query.sql, query.params)
        ranked = suggest.summarise(frame)
        if ranked:
            label = query.label
            break
    frame, known_error = _query(conn, suggest.build_known_templates_query())
    known = [s.template for s in suggest.summarise(frame)]
    return label, ranked, known, error or known_error


def _choose_template(conn: SnowflakeConnection, row: dict, step: fixer.FixStep) -> fixer.FixStep:
    tid = str(row["TERMINAL_IDENTIFIER"])
    cache = _state(_TEMPLATES_KEY, {})
    if tid not in cache:
        with st.spinner("Looking up templates used by healthy peers…"):
            cache[tid] = _load_templates(conn, row)
    label, ranked, known, error = cache[tid]
    if error:
        st.warning(f"Template lookup was incomplete: {error}")
    suggested = [s.template for s in ranked]
    options = suggested + [t for t in known if t not in suggested]
    if not options:
        st.error("No templates are in use on healthy terminals — prepare this fix manually.")
        return step
    if ranked:
        top = ranked[0]
        message = (
            f"Suggested template **{top.template}** — used by {top.terminals:,} {label} "
            f"({top.share:.0%})."
        )
        if top.share >= _STRONG_SHARE and top.terminals >= _STRONG_PEERS:
            st.success(message)
        else:
            st.warning(f"{message} Low confidence — check it before using it.")
    else:
        st.warning(
            "No healthy terminal shares this brand/model, so there is no suggestion. "
            "Pick the template yourself."
        )
    choice = st.selectbox(
        "DCC receipt template",
        options,
        index=0 if ranked else None,
        placeholder="Choose a template",
        key=f"rem_tpl_{tid}",
    )
    if not choice:
        return step
    confirmed = st.checkbox(
        f"I confirm **{choice}** is the correct receipt template for instance "
        f"{step.target_identifier}",
        key=f"rem_tpl_ok_{tid}_{choice}",
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
    value = step.config_value or "template to choose"
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
            st.success(f"**Live:** applied and verified by a fresh read ({live.transaction}).")
        else:
            detail = post.detail if post is not None else "no verification read"
            st.warning(
                f"**Live:** applied, but verification did not confirm the fix — {detail}. "
                "Investigate, and roll back if needed."
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
    _show_notices()

    sql_conn = st.session_state.get("connection")
    environment = str(st.session_state.get("connected_env") or "").upper()
    login = str(st.session_state.get("connected_login") or "")
    if sql_conn is None or not environment:
        st.info("Connect to SQL Server in the sidebar to pre-check and fix this terminal.")
        return

    user, is_operator, operator_error = operator_status(conn, objs)
    where = f"{environment} · {sql_conn.server}/{sql_conn.database}"
    if environment in fixer.LIVE_ENVIRONMENTS:
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
    if operator_error:
        who += f" Operator check failed: {operator_error}"
    st.caption(who)

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
            _write_log(
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
            _notice("error", f"The dry run did not start: {exc}")
        else:
            add_result(result)
            work["dry"] = fixer.DryRun(step.signature(environment), result.status, time.time())
            recorded = fixer.dry_run_outcome(result)
            _write_log(
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
                )
                post = fixer.precheck(sql_conn, step, environment)
        except Exception as exc:
            _notice("error", f"The live call did not run: {exc}")
        else:
            add_result(result)
            work.update(live=result, post=post)
            work.pop("dry", None)  # any further live call needs a fresh dry run
            recorded = fixer.live_outcome(result, post)
            _write_log(
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
        recorded = fixer.rollback_outcome(outcome.ok, outcome.error)
        _write_log(
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
        _notice(
            "success" if outcome.ok else "error",
            "Rolled back." if outcome.ok else f"Rollback failed: {outcome.error}",
        )
        st.rerun()

    _render_progress(work, step.definition.bit)
    _render_history(conn, objs, tid)
    _render_next(next_terminal)


def _render_next(next_terminal: str | None) -> None:
    if next_terminal and st.button(f"Next terminal → {next_terminal}", key="rem_next_terminal"):
        st.session_state[SELECTED_KEY] = next_terminal
        st.rerun()
