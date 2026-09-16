"""Page sections for the single-page console."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import pandas as pd
import streamlit as st

from ..campaign import campaign_markdown, campaign_results, campaign_summary, campaign_tests
from ..catalog import TEST_CATALOG, TEST_KEYS
from ..config import ENVIRONMENTS
from ..coverage import compute_coverage, coverage_totals
from ..execution import TestResult, apply_rollback, build_call, run_test
from ..negatives import NegativeCase, default_negative_cases, run_negative, run_negative_battery
from ..readiness import all_passed, capture_procedure_version, grant_script, run_readiness
from ..reference import load_instances, load_locations, load_terminals
from ..report import cab_report
from ..state import add_result, connection, results

STATUS_BADGES = {"PASS": "🟢", "FAIL": "🔴", "REVIEW": "🟡", "BLOCKED": "⛔"}


def _badge(status: str) -> str:
    return STATUS_BADGES.get(status, "⚪")


def _clip(value: object, limit: int = 1500) -> str:
    text = "(null)" if value is None else str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text) - limit} more characters]"


def _result_ledger(result: TestResult) -> str:
    """Return the factual record included in the downloadable session report."""
    return "\n".join(
        [
            f"ENVIRONMENT        : {result.environment} (login {result.login})",
            f"TEST NAME          : {result.test_key}",
            f"DISPLAY BIT        : {result.bit}",
            f"TARGET             : {result.target_type} = {result.target}",
            f"VALUE SENT         : {result.value}",
            f"EXECUTION MODE     : {result.mode}",
            f"TRANSACTION        : {result.transaction}",
            f"SQL EXECUTED       : {result.sql}",
            f"DURATION           : {result.duration_s} s",
            f"STATE BEFORE       : {_clip(result.state_before)}",
            f"STATE AFTER        : {_clip(result.state_after)}",
            f"CHANGE PERSISTED   : {result.change_persisted}",
            f"HARNESS STATUS     : {result.status}",
            f"SQL ERROR          : {result.error or 'none'}",
            f"SERVER MESSAGES    : {result.messages or 'none'}",
            f"PROC RESULT SETS   : {_clip(json.dumps(result.grids, default=str), 1800)}",
            f"RESTORE POINT KNOWN: {result.restore_point_known}",
            f"ROLLBACK LOG       : {result.rollback_log or 'no rollback performed'}",
            f"ROLLBACK AVAILABLE : {result.can_rollback}",
        ]
    )


def _session_history_markdown() -> str:
    """Return archived run records without mixing them into current results."""
    lines = ["# DCC Previous Session Log", ""]
    for session in st.session_state.session_history:
        lines.extend(
            [
                f"## {session['environment']} · {session['login']} · {session['archived_at']}",
                "",
                *(_result_ledger(result) for result in session["results"]),
                "",
            ]
        )
    return "\n".join(lines)


def _cab_meta(all_results: list[TestResult]) -> dict:
    """Assemble the header metadata block for the CAB report."""
    conn = st.session_state.connection
    mode = "LIVE" if any(result.is_live for result in all_results) else "SIMULATION"
    return {
        "server": getattr(conn, "server", "?"),
        "database": getattr(conn, "database", "?"),
        "environment": st.session_state.connected_env or "?",
        "login": st.session_state.connected_login or "?",
        "mode": mode,
        "procedure_version": st.session_state.get("procedure_version"),
    }


# ---------------------------------------------------------------------------
# 1 · readiness
# ---------------------------------------------------------------------------


def render_readiness() -> bool:
    st.subheader("1 · Pre-flight readiness")
    st.caption(
        "Confirms the procedure and its trace dependency exist and that this login may "
        "execute both. Tests stay locked until every check passes."
    )

    if st.session_state.readiness is None:
        with st.spinner("Running pre-flight readiness checks…"):
            st.session_state.readiness = run_readiness(
                connection(), st.session_state.connected_login
            )
            st.session_state.procedure_version = capture_procedure_version(connection())

    outcomes = st.session_state.readiness
    controls = st.columns([1, 3])

    if controls[0].button("🔄 Re-run readiness checks", use_container_width=True):
        with st.spinner("Probing the database…"):
            st.session_state.readiness = run_readiness(
                connection(), st.session_state.connected_login
            )
            st.session_state.procedure_version = capture_procedure_version(connection())
        st.rerun()

    passed = sum(1 for outcome in outcomes if outcome.passed)
    total = len(outcomes)
    if passed == total:
        controls[1].success(f"✅ {passed}/{total} checks passed — ready to run tests")
    else:
        controls[1].error(f"⛔ {total - passed} of {total} check(s) failed — tests are blocked")

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Check": outcome.name,
                    "Status": "✅ PASS" if outcome.passed else "❌ FAIL",
                    "Fix": "" if outcome.passed else outcome.remedy,
                }
                for outcome in outcomes
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    if all_passed(outcomes):
        return True

    st.warning("Ask a DBA to run the script below, then re-run the readiness checks.")
    st.code(
        grant_script(
            outcomes,
            connection().database,
            st.session_state.connected_env or "",
        ),
        language="sql",
    )

    override = st.checkbox(
        "Override — run tests anyway (results will land as BLOCKED)",
        key="readiness_override",
    )
    if override:
        st.info("Override active. Expect permission errors until the grants are in place.")
    return override


# ---------------------------------------------------------------------------
# 2 · execution mode
# ---------------------------------------------------------------------------


def render_mode() -> tuple[bool, bool]:
    env_name = st.session_state.connected_env or "DEV"
    st.subheader(f"2 · Execution mode — {env_name}")

    left, right = st.columns([1, 2])
    with left:
        mode = st.radio(
            "How should the procedure be executed?",
            ["🛡️ Simulation — roll back everything", "⚡ Live — commit changes"],
            index=0,
            key="exec_mode",
        )
    simulation = mode.startswith("🛡️")

    with right:
        if simulation:
            st.success(
                "**SIMULATION** · `@is_simulation = 1` and the transaction is rolled back "
                "after every call. Nothing is written to the database."
            )
            return True, True

        st.error(
            f"**LIVE on {env_name}** · `@is_simulation = 0` and the transaction is committed. "
            "The harness captures the pre-test value first, auto-restores on failure, and "
            "offers a manual rollback on every result."
        )
        acknowledged = st.checkbox(f"I understand this writes to {env_name}", key="live_ack")
        typed = st.text_input(f"Type `{env_name}` to arm live execution", key="live_confirm")
        confirmed = acknowledged and typed.strip().upper() == env_name.upper()
        if acknowledged and not confirmed:
            st.caption(f"Waiting for you to type `{env_name}` exactly.")
    return False, confirmed


# ---------------------------------------------------------------------------
# 3 · terminals
# ---------------------------------------------------------------------------


def render_terminals() -> None:
    st.subheader("3 · Active terminals")

    controls = st.columns([1, 1, 1, 2])
    only_online = controls[0].checkbox("Online only", value=True, key="flt_online")
    only_unlocked = controls[1].checkbox("Unlocked only", value=True, key="flt_unlocked")
    limit = controls[2].number_input("Max rows", 10, 2000, 200, step=10, key="flt_limit")

    if controls[3].button("🔍 Load active terminals", use_container_width=True, type="primary"):
        with st.spinner("Querying cccintegrang.emv_terminal…"):
            try:
                st.session_state.terminals = load_terminals(
                    connection(), only_online, only_unlocked, limit
                )
            except Exception as exc:
                st.error(f"Terminal query failed: {exc}")

    terminals = st.session_state.terminals
    if terminals is None:
        st.info("Click **Load active terminals** to list the terminals available for testing.")
        return
    if terminals.empty:
        st.warning("No terminals matched the current filters.")
        return

    search = st.text_input("Filter by terminal id, model or location", "", key="terminal_search")
    view = terminals
    if search:
        needle = search.lower()
        view = view[
            view.apply(
                lambda row: needle in " ".join(str(cell).lower() for cell in row.values),
                axis=1,
            )
        ]

    metrics = st.columns(4)
    metrics[0].metric("Loaded", len(terminals))
    metrics[1].metric("Matching filter", len(view))
    metrics[2].metric("Online", int(terminals["is_online"].fillna(0).astype(int).sum()))
    metrics[3].metric("Locked", int(terminals["is_locked"].fillna(0).astype(int).sum()))

    st.dataframe(view, use_container_width=True, hide_index=True, height=280)

    if view.empty:
        return

    picker = st.columns([3, 1])
    choice = picker[0].selectbox(
        "Select a terminal to use in the test below",
        view["terminal_identifier"].tolist(),
        key="terminal_picker",
    )
    if picker[1].button("📌 Use this terminal", use_container_width=True):
        st.session_state.selected_terminal = choice
        st.success(f"Terminal `{choice}` selected for testing.")


# ---------------------------------------------------------------------------
# 4 · configuration under test
# ---------------------------------------------------------------------------


def _target_picker(definition) -> str:
    if definition.target == "terminal":
        return st.text_input(
            "Terminal identifier",
            value=st.session_state.selected_terminal,
            placeholder="pick one above or type it here",
            key="target_terminal",
        ).strip()

    if definition.target == "instance":
        if st.session_state.instances is None:
            st.session_state.instances = load_instances(connection())
        options = st.session_state.instances["instance_identifier"].tolist()
        return st.selectbox("Instance identifier", options, key="target_instance")

    if st.session_state.locations is None:
        st.session_state.locations = load_locations(connection())
    options = st.session_state.locations["location_no"].tolist()
    return st.selectbox("Location number", options, key="target_location")


def render_test(simulation: bool, armed: bool) -> None:
    st.subheader("4 · Configuration under test")

    test_key = st.selectbox("Configuration", TEST_KEYS, key="config_name")
    definition = TEST_CATALOG[test_key]
    st.caption(definition.summary)

    with st.expander("What does this setting actually do?"):
        st.markdown(f"**Business meaning** · {definition.business_meaning}")
        st.markdown(f"**Mechanism** · {definition.mechanism}")
        st.markdown(
            f"**Verified column** · `{definition.verify.qualified}` "
            f"keyed by `{definition.verify.key}`"
        )

    left, right = st.columns(2)
    with left:
        st.markdown(f"**Target · {definition.target}**")
        target = _target_picker(definition)
    with right:
        st.markdown(f"**{definition.value_label}**")
        if definition.value_options:
            value = st.selectbox(
                definition.value_label, definition.value_options, key="config_value"
            )
        else:
            value = st.text_input(
                definition.value_label,
                placeholder="exact value expected by the procedure",
                key="config_value_text",
            ).strip()

    if target and value:
        _, _, preview = build_call(definition, target, value, simulation)
        st.code(preview, language="sql")

    negative_mode = st.checkbox(
        "🔀 Negative test — use an invalid target to prove rejection",
        help=(
            "Overrides the target with a deliberately non-existent identifier. "
            "The procedure SHOULD reject it; silent acceptance is a finding."
        ),
        key="negative_single_mode",
    )
    if negative_mode:
        invalid_defaults = {
            "instance": "I000099999",
            "terminal": "99999999",
            "location": "9999999",
        }
        target = st.text_input(
            "Invalid target identifier (type any non-existent value)",
            value=invalid_defaults.get(definition.target, "INVALID"),
            key="negative_single_target",
        ).strip()

    blocked = not target or not value or not armed
    if st.button("▶️ Run this test", type="primary", use_container_width=True, disabled=blocked):
        if negative_mode:
            case = NegativeCase(
                name=f"Manual: {test_key}",
                base_key=test_key,
                target=target,
                value=value,
                invalid_reason="User-specified invalid target for negative validation",
                expected="Procedure should reject or error on this input.",
            )
            with st.spinner(f"Negative test: {test_key} → {target}…"):
                result = run_negative(
                    connection(),
                    case,
                    st.session_state.connected_env,
                    st.session_state.connected_login,
                )
        else:
            with st.spinner(f"Executing {test_key}…"):
                result = run_test(
                    connection(),
                    definition,
                    target,
                    value,
                    simulation,
                    st.session_state.connected_env,
                    st.session_state.connected_login,
                )
        add_result(result)
        st.rerun()

    if blocked:
        if not target:
            st.warning("Choose a target before running the test.")
        elif not value:
            st.warning(f"Provide a {definition.value_label.lower()} before running the test.")
        else:
            st.warning(
                "Blocked · pass the pre-flight readiness checks and, in live mode, confirm "
                "the environment before running the test."
            )


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def _load_campaign_terminals() -> None:
    """Reuse the section 3 filters so the campaign tab can load targets on its own."""
    st.session_state.terminals = load_terminals(
        connection(),
        bool(st.session_state.get("flt_online", True)),
        bool(st.session_state.get("flt_unlocked", True)),
        int(st.session_state.get("flt_limit", 200)),
    )


def render_campaign(simulation: bool, armed: bool) -> None:
    st.subheader("Configuration test campaign")
    st.caption(
        "Run compatible location, terminal, and instance configuration tests in sequence. "
        "Each target type is selected explicitly; live changes retain rollback controls."
    )

    env_name = st.session_state.connected_env or ""
    if simulation:
        st.success(
            f"**SIMULATION on {env_name}** · every campaign call runs with `@is_simulation = 1` "
            "inside a transaction that is rolled back. Nothing is written to the database."
        )
    elif armed:
        st.error(
            f"**LIVE on {env_name}** · campaign calls are committed. Each test captures its "
            "restore point first and stays rollback-capable in the campaign report below."
        )
    else:
        st.warning(
            f"**LIVE on {env_name}** is selected but not armed. Pass readiness and confirm the "
            "environment in section 2 before a campaign can run."
        )

    campaign_name = st.text_input(
        "Campaign name", value="Full DCC CAB validation", key="campaign_name"
    )
    definitions = campaign_tests()
    selected_keys = st.multiselect(
        "Configuration tests",
        [definition.key for definition in definitions],
        default=[definition.key for definition in definitions],
    )
    selected_definitions = [TEST_CATALOG[key] for key in selected_keys]

    include_negatives = st.checkbox(
        "Include negative validation battery (wrong inputs, imposed failures — Section C)",
        value=True,
        key="campaign-include-negatives",
        help=(
            "Appends 7 deliberately-invalid cases to the campaign: non-existent instance, "
            "terminal, and location; unknown flag name; unknown function; unknown version; "
            "empty JSON. Each must be rejected by the procedure for the campaign to be CAB-grade."
        ),
    )

    targets: dict[str, list[str]] = {}
    if any(definition.target == "terminal" for definition in selected_definitions):
        terminals = st.session_state.terminals
        if terminals is None or terminals.empty:
            loader = st.columns([3, 1])
            loader[0].info("No active terminals are loaded yet.")
            if loader[1].button(
                "🔍 Load terminals",
                use_container_width=True,
                key="campaign-load-terminals",
            ):
                with st.spinner("Querying cccintegrang.emv_terminal…"):
                    _load_campaign_terminals()
                st.rerun()
        else:
            terminal_ids = terminals["terminal_identifier"].dropna().astype(str).tolist()
            targets["terminal"] = st.multiselect(
                "Active terminals",
                terminal_ids,
                default=[st.session_state.selected_terminal]
                if st.session_state.selected_terminal in terminal_ids
                else [],
                key="campaign-target-terminal",
            )

    if any(definition.target == "instance" for definition in selected_definitions):
        if st.session_state.instances is None:
            st.session_state.instances = load_instances(connection())
        instance_ids = (
            st.session_state.instances["instance_identifier"].dropna().astype(str).tolist()
        )
        targets["instance"] = st.multiselect(
            "Instances",
            instance_ids,
            key="campaign-target-instance",
        )

    if any(definition.target == "location" for definition in selected_definitions):
        if st.session_state.locations is None:
            st.session_state.locations = load_locations(connection())
        location_ids = st.session_state.locations["location_no"].dropna().astype(str).tolist()
        targets["location"] = st.multiselect(
            "Locations",
            location_ids,
            key="campaign-target-location",
        )

    values: dict[str, list[str]] = {}
    for definition in definitions:
        if definition.key not in selected_keys:
            continue
        short_name = (
            definition.function_name
            or definition.key.split(" — ", 1)[-1].split(" (")[0]
        )
        if definition.value_options:
            values[definition.key] = st.multiselect(
                f"{definition.value_label} for {short_name} — one run per selected value",
                definition.value_options,
                default=[definition.value_options[0]],
                key=f"campaign-value-{definition.key}",
            )
        else:
            entered = st.text_input(
                f"{definition.value_label} for {short_name}",
                key=f"campaign-value-{definition.key}",
                placeholder="exact value expected by the procedure",
            ).strip()
            values[definition.key] = [entered] if entered else []

    missing_values = [key for key in selected_keys if not values.get(key)]
    missing_targets = [
        definition.target
        for definition in selected_definitions
        if not targets.get(definition.target)
    ]
    blocked = (
        not campaign_name.strip()
        or not selected_keys
        or bool(missing_values)
        or bool(missing_targets)
        or not armed
    )
    planned_runs = sum(
        len(targets.get(definition.target, [])) * len(values.get(definition.key, []))
        for definition in selected_definitions
    )
    neg_count = len(default_negative_cases()) if include_negatives else 0
    total_planned = planned_runs + neg_count
    mode_label = "SIMULATION" if simulation else "LIVE"
    neg_label = f" + {neg_count} negative cases" if neg_count else ""
    st.caption(
        f"Planned executions: {planned_runs} positive{neg_label} = "
        f"**{total_planned} total** run(s) in {mode_label} mode."
    )

    if st.button(
        "▶️ Run campaign",
        type="primary",
        use_container_width=True,
        disabled=bool(blocked),
    ):
        campaign_id = f"campaign-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
        progress = st.progress(0, text=f"Preparing {total_planned} campaign test(s)...")
        completed = 0
        for definition in selected_definitions:
            for target in targets[definition.target]:
                for value in values[definition.key]:
                    progress.progress(
                        completed / max(total_planned, 1),
                        text=f"Testing {target}: {definition.key} = {value}",
                    )
                    result = run_test(
                        connection(),
                        definition,
                        target,
                        value,
                        simulation,
                        st.session_state.connected_env,
                        st.session_state.connected_login,
                        campaign_id=campaign_id,
                    )
                    add_result(result)
                    completed += 1
        if include_negatives:
            progress.progress(
                completed / max(total_planned, 1),
                text="Running negative validation battery (wrong inputs)…",
            )
            neg_results = run_negative_battery(
                connection(),
                st.session_state.connected_env,
                st.session_state.connected_login,
                campaign_id=campaign_id,
            )
            for r in neg_results:
                add_result(r)
            completed += len(neg_results)
        st.session_state.campaigns.insert(
            0,
            {
                "id": campaign_id,
                "name": campaign_name.strip(),
                "created_at": datetime.now().isoformat(),
            },
        )
        progress.progress(1.0, text=f"Completed {completed} campaign test(s).")
        st.rerun()

    if blocked:
        if not selected_keys:
            st.warning("Select at least one configuration test.")
        elif missing_values:
            st.warning("Provide a value for every selected test.")
        elif missing_targets:
            target_types = ", ".join(sorted(set(missing_targets)))
            st.warning(f"Select at least one target for: {target_types}.")
        elif not armed:
            st.warning("Pass readiness checks and confirm the environment before running.")

    st.divider()
    st.markdown("#### Campaign reports and rollback")
    if not st.session_state.campaigns:
        st.info("Run a campaign to generate its report and rollback controls.")
        return

    for campaign in st.session_state.campaigns:
        _render_campaign_report(campaign)


def _render_campaign_report(campaign: dict) -> None:
    campaign_id = campaign["id"]
    entries = campaign_results(results(), campaign_id)
    summary = campaign_summary(entries)

    st.markdown(f"**{campaign['name']}** · started {campaign['created_at']}")
    metrics = st.columns(6)
    metrics[0].metric("Tests", summary["total"])
    metrics[1].metric("Passed", summary["passed"])
    metrics[2].metric("Failed", summary["failed"])
    metrics[3].metric("Review", summary["review"])
    metrics[4].metric("Blocked", summary["blocked"])
    metrics[5].metric("Awaiting rollback", summary["awaiting_rollback"])

    if not entries:
        st.info("No results are attached to this campaign.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Target type": result.target_type,
                    "Target": result.target,
                    "Test": result.test_key,
                    "Value": result.value,
                    "Mode": result.mode,
                    "Status": f"{_badge(result.status)} {result.status}",
                    "Persisted": result.change_persisted,
                    "Rollback available": result.can_rollback,
                }
                for result in entries
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    actions = st.columns(3)
    actions[0].download_button(
        "📋 CAB report (Markdown)",
        data=cab_report(entries, _cab_meta(entries)),
        file_name=f"dcc_cab_campaign_{stamp}.md",
        mime="text/markdown",
        use_container_width=True,
        type="primary",
        key=f"campaign-cab-{campaign_id}",
    )
    actions[1].download_button(
        "📄 Campaign report (Markdown)",
        data=campaign_markdown(campaign["name"], entries),
        file_name=f"dcc_campaign_{stamp}.md",
        mime="text/markdown",
        use_container_width=True,
        key=f"campaign-md-{campaign_id}",
    )
    actions[2].download_button(
        "📥 Campaign results (JSON)",
        data=json.dumps([result.export() for result in entries], indent=2, default=str),
        file_name=f"dcc_campaign_{stamp}.json",
        mime="application/json",
        use_container_width=True,
        key=f"campaign-json-{campaign_id}",
    )

    st.markdown("**↩️ Campaign rollback**")
    outstanding = [result for result in entries if result.can_rollback]
    if not any(result.is_live for result in entries):
        st.info(
            "Simulation campaign — every transaction was already rolled back, so there is "
            "nothing to restore."
        )
    elif outstanding:
        st.warning(f"{len(outstanding)} live change(s) from this campaign are still applied.")
        if st.button(
            "↩️ Roll back this campaign's live changes",
            key=f"campaign-rollback-{campaign_id}",
            type="primary",
            use_container_width=True,
        ):
            for result in outstanding:
                with st.spinner(f"Restoring {result.target}…"):
                    apply_rollback(
                        connection(),
                        TEST_CATALOG[result.test_key],
                        result,
                        trigger="campaign",
                    )
            st.rerun()
    else:
        st.success("Every live change in this campaign matches its pre-test value.")

    for result in entries:
        _render_result(result, expanded=False)


# ---------------------------------------------------------------------------
# 5 · results and rollback
# ---------------------------------------------------------------------------


def _render_rollback_panel(result: TestResult) -> None:
    """Always shown for live runs so the manual control is never hidden."""
    if not result.is_live:
        st.info(
            "Simulation run — the harness already rolled the transaction back, so there is "
            "nothing to restore."
        )
        return

    definition = TEST_CATALOG[result.test_key]
    st.caption(
        f"Restores the value captured before the test into "
        f"`{definition.verify.qualified}` for {result.target_type} `{result.target}`."
    )

    for entry in result.rollback_log:
        label = entry["trigger"].capitalize()
        if entry["ok"]:
            st.success(f"{label} rollback succeeded at {entry['at']} ({entry['rows']} row(s)).")
        else:
            st.error(f"{label} rollback failed at {entry['at']}: {entry['error']}")

    if not result.restore_point_known:
        st.warning(
            "No restore point was captured for this row, so an automatic restore is not "
            "possible. Recover from a backup if the value must be reverted."
        )
        return

    with st.popover("Show the restore point"):
        st.code(_clip(result.restore_point, 2000))

    if result.can_rollback:
        st.warning("This live change is still applied to the database.")
        if st.button("↩️ Roll back this change", key=f"rb-{result.id}", type="primary"):
            with st.spinner("Restoring the pre-test value…"):
                apply_rollback(connection(), definition, result)
            st.rerun()
        return

    st.success("The verified column currently matches its pre-test value.")
    if st.button(
        "↩️ Force restore anyway",
        key=f"rb-force-{result.id}",
        help="Re-writes the captured pre-test value even though no difference was detected.",
    ):
        with st.spinner("Re-writing the pre-test value…"):
            apply_rollback(connection(), definition, result, trigger="forced")
        st.rerun()


def _render_result(result: TestResult, expanded: bool) -> None:
    header = (
        f"{_badge(result.status)} {result.status} · {result.test_key} · "
        f"{result.environment} · {result.target} · {result.mode} · {result.timestamp}"
    )
    with st.container():
        st.markdown(f"**{header}**")
        facts = st.columns(5)
        facts[0].metric("Bit", result.bit)
        facts[1].metric("Environment", result.environment or "?")
        facts[2].metric("Transaction", result.transaction)
        facts[3].metric("Persisted", "yes" if result.change_persisted else "no")
        facts[4].metric("Duration", f"{result.duration_s}s")

        if result.error:
            st.error(result.error)

        st.code(result.sql, language="sql")

        state = st.columns(2)
        state[0].markdown("**State before**")
        state[0].code(_clip(result.state_before, 600))
        state[1].markdown("**State after**")
        state[1].code(_clip(result.state_after, 600))

        if result.messages:
            st.markdown("**Server messages**")
            st.code("\n".join(result.messages))

        for index, frame in enumerate(result.grid_frames, start=1):
            st.markdown(f"**Procedure result set {index}**")
            st.dataframe(frame, use_container_width=True, hide_index=True)

        st.markdown("**↩️ Rollback**")
        _render_rollback_panel(result)


def _render_negative_battery() -> None:
    """Run the deliberately-invalid input battery — Section C evidence."""
    with st.expander("🧪 Negative validation battery (Section C evidence)"):
        st.caption(
            "Sends deliberately invalid input (bad instance, terminal, location, flag name, "
            "location function, version, and an empty target JSON). Every case runs with "
            "`@is_simulation = 1` and is rolled back. A **rejection** is the pass condition; "
            "silent acceptance is a finding."
        )
        cases = default_negative_cases(
            valid_terminal=st.session_state.selected_terminal or None
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Case": case.name,
                        "Sends": f"{case.base_key.split(' — ')[0]} · {case.value}",
                        "Why invalid": case.invalid_reason,
                        "Expected": case.expected,
                    }
                    for case in cases
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        if st.button("▶️ Run negative validation battery", use_container_width=True):
            progress = st.progress(0.0, text="Running negative cases…")
            outcomes = run_negative_battery(
                connection(),
                st.session_state.connected_env,
                st.session_state.connected_login,
                cases=cases,
            )
            for result in outcomes:
                add_result(result)
            progress.progress(1.0, text=f"Ran {len(outcomes)} negative case(s).")
            st.rerun()


def _render_cab_report_download(all_results: list[TestResult], stamp: str) -> None:
    """Full-width CAB report download plus a live coverage preview."""
    rows = compute_coverage([r for r in all_results if not r.is_negative])
    totals = coverage_totals(rows)
    negatives = [r for r in all_results if r.is_negative]
    rejected = sum(r.verdict_code == "REJECTED-AS-EXPECTED" for r in negatives)

    st.markdown("#### 📋 CAB validation report")
    cols = st.columns(3)
    cols[0].metric("Areas covered", f"{totals['covered']}/{totals['total']}")
    cols[1].metric("Areas blocked", totals["blocked"])
    cols[2].metric("Negatives rejected", f"{rejected}/{len(negatives)}")
    if totals["not_tested"]:
        st.caption(
            f"{totals['not_tested']} area(s) not yet exercised — they will appear as "
            "NOT TESTED in the coverage matrix until you run them."
        )

    st.download_button(
        "📄 Download CAB validation report (Markdown)",
        data=cab_report(all_results, _cab_meta(all_results)),
        file_name=f"dcc_cab_report_{stamp}.md",
        mime="text/markdown",
        use_container_width=True,
        type="primary",
    )


def render_results() -> None:
    st.subheader("5 · Results and rollback")

    if st.session_state.session_history:
        st.download_button(
            "Download previous session log",
            data=_session_history_markdown(),
            file_name="dcc_previous_sessions.md",
            mime="text/markdown",
        )

    _render_negative_battery()

    all_results = results()
    if not all_results:
        st.info(
            "No tests executed yet. Run a configuration test, a campaign, or the negative "
            "battery above."
        )
        return

    outstanding = [result for result in all_results if result.can_rollback]
    negatives = [result for result in all_results if result.is_negative]

    summary = st.columns(6)
    summary[0].metric("Total runs", len(all_results))
    summary[1].metric("Executed", sum(r.status in {"PASS", "REVIEW"} for r in all_results))
    summary[2].metric("Failed", sum(r.status == "FAIL" for r in all_results))
    summary[3].metric("Blocked", sum(r.status == "BLOCKED" for r in all_results))
    summary[4].metric("Negatives", len(negatives))
    summary[5].metric("Awaiting rollback", len(outstanding))

    if outstanding:
        st.warning(
            f"{len(outstanding)} live change(s) are still applied to "
            f"{st.session_state.connected_env}."
        )
        if st.button("↩️ Roll back every outstanding live change", use_container_width=True):
            for result in outstanding:
                with st.spinner(f"Restoring {result.target}…"):
                    apply_rollback(connection(), TEST_CATALOG[result.test_key], result)
            st.rerun()

    for result in all_results:
        _render_result(result, expanded=result is all_results[0])

    st.divider()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    _render_cab_report_download(all_results, stamp)
    st.divider()

    report_parts = ["# DCC Enablement Configuration Test Session", "## Per-test ledger"]
    for index, result in enumerate(all_results, start=1):
        report_parts.append(f"## {index}. {result.test_key} — {result.verdict_code}")
        report_parts.append("```")
        report_parts.append(_result_ledger(result))
        report_parts.append("```")

    markdown_report = "\n\n".join(report_parts)

    actions = st.columns(3)
    actions[0].download_button(
        "📥 Results (JSON)",
        data=json.dumps([result.export() for result in all_results], indent=2, default=str),
        file_name=f"dcc_results_{stamp}.json",
        mime="application/json",
        use_container_width=True,
    )
    actions[1].download_button(
        "📄 Per-test ledger (Markdown)",
        data=markdown_report,
        file_name=f"dcc_ledger_{stamp}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    if actions[2].button("🗑️ Clear results", use_container_width=True):
        st.session_state.results = []
        st.rerun()


__all__ = [
    "render_readiness",
    "render_mode",
    "render_terminals",
    "render_campaign",
    "render_test",
    "render_results",
    "ENVIRONMENTS",
]
