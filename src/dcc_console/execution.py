"""Build and run one configuration test against the stored procedure."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .catalog import TestDefinition
from .config import BIT8_FALSE_FLAGS, PROCEDURE, RETURN_CODE_COLUMN
from .database import DatabaseConnection
from .journal import get_journal
from .rollback import (
    METHOD_PROCEDURE_JSON,
    RollbackOutcome,
    capture_rollback_plan,
    flag_states_match,
    read_sp_flag_states,
    read_state,
    restore_via_payload,
)
from .trace import TraceSignature, trace_from_messages

PERMISSION_DENIED_MARKER = "EXECUTE permission was denied"

ORIGIN_TEST = "test"
ORIGIN_REMEDIATION = "remediation"


@dataclass
class TestResult:
    """Everything observed for a single execution."""

    id: str
    test_key: str
    bit: int
    environment: str
    login: str
    target_type: str
    target: str
    value: str
    mode: str
    status: str
    error: str | None
    state_before: object
    state_after: object
    restore_point: object
    change_persisted: bool
    transaction: str
    sql: str
    messages: list[str]
    grids: list[dict]
    grid_frames: list[pd.DataFrame]
    duration_s: float
    timestamp: str
    rollback_log: list[dict] = field(default_factory=list)
    campaign_id: str | None = None
    # --- CAB evidence fields (defaulted so existing constructors keep working) ---
    input_json: str = ""
    params: list = field(default_factory=list)
    proc_args: tuple[str, ...] = ()
    proposed_change_observed: bool = False
    is_negative: bool = False
    journal_id: int | None = None
    trace_rows: list[dict] = field(default_factory=list)
    trace_status: str = "NOT_CAPTURED"
    trace_reason: str | None = None
    return_code: int | None = None
    # Legacy SP output is retained as evidence only, never executed.
    sp_rollback_scripts: list[str] = field(default_factory=list)
    # True when this test's bit is procedure-managed (see TestDefinition.sp_managed).
    sp_managed: bool = False
    # Handler-level flag verification (sp_managed bits: Bit 8's handler flags, Bit
    # 16's receipt template): the named flag's value (or the whole document, for
    # Bit 16) on every affected handler, read via the procedure's own
    # instance_id/handler_type join, before the call, after the call, and after
    # rollback. Additive CAB evidence.
    sp_prior_states: list[dict] = field(default_factory=list)
    sp_after_states: list[dict] = field(default_factory=list)
    sp_restored_states: list[dict] = field(default_factory=list)
    # True/False once a rollback has been verified against prior; None = not yet
    # attempted or the verification read was unavailable.
    sp_flag_verified: bool | None = None
    # Which part of the console produced the run (ORIGIN_* below). Remediation fixes
    # are kept on purpose and roll back only through the DCC Remediation tab.
    origin: str = "test"
    rollback_plan: dict = field(default_factory=dict)
    rollback_capture_error: str | None = None
    server: str = ""
    database_name: str = ""

    @property
    def trace_text(self) -> str:
        """Flatten trace messages into one searchable blob."""
        if not self.trace_rows:
            return ""
        parts: list[str] = []
        for row in self.trace_rows:
            if "message" in row:
                if row["message"] is not None:
                    parts.append(str(row["message"]))
            else:
                parts.extend(str(v) for v in row.values() if v is not None)
        return " ".join(parts)

    @property
    def is_live(self) -> bool:
        return self.mode == "LIVE"

    @property
    def restore_point_known(self) -> bool:
        return bool(self.rollback_plan)

    @property
    def can_rollback(self) -> bool:
        """A live change with structured original values that has not been restored."""
        return self.rollback_available

    @property
    def has_sp_rollback(self) -> bool:
        """A live run with a structured SP JSON restore point."""
        return self.is_live and bool(self.rollback_plan)

    @property
    def sp_rolled_back(self) -> bool:
        """The SP has restored and verified this result's original values."""
        return any(
            entry.get("ok") and entry.get("method") == METHOD_PROCEDURE_JSON
            for entry in self.rollback_log
        )

    @property
    def rollback_available(self) -> bool:
        """Never offer legacy SQL or replay a verified rollback."""
        return (
            self.has_sp_rollback and not self.sp_rolled_back
            and (self.effective_change_persisted or bool(self.error))
        )

    @property
    def sp_flag_rows(self) -> list[dict]:
        """Per-handler prior/after/restored flag values for the CAB verification table."""
        by_handler: dict[str, dict] = {}
        for label, states in (
            ("prior", self.sp_prior_states),
            ("after", self.sp_after_states),
            ("restored", self.sp_restored_states),
        ):
            for state in states:
                name = state.get("handler_name")
                handler_id = state.get("handler_id")
                identity = str(handler_id) if handler_id is not None else name
                row = by_handler.setdefault(identity, {"handler_name": name})
                if handler_id is not None:
                    row["handler_id"] = str(handler_id)
                row[label] = state.get("flag_value")
        return list(by_handler.values())

    def _sp_join(self, field_key: str) -> object:
        """Join one field of ``sp_flag_rows`` across handlers for single-value display.

        A single affected handler (the common case) yields its raw value; multiple
        handlers are joined as ``name: value`` pairs so nothing is silently dropped.
        """
        rows = self.sp_flag_rows
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0].get(field_key)
        return "; ".join(
            f"{r.get('handler_name')}"
            + (f" (handler_id={r['handler_id']})" if "handler_id" in r else "")
            + f": {r.get(field_key)}"
            for r in rows
        )

    @property
    def sp_display_before(self) -> object:
        """The real pre-call value on the related table (sp_managed bits only)."""
        return self._sp_join("prior")

    @property
    def sp_display_after(self) -> object:
        """The real post-call value on the related table (sp_managed bits only)."""
        return self._sp_join("after")

    @property
    def sp_change_detected(self) -> bool | None:
        """True/False once prior/after are both known; None if not read (cannot confirm)."""
        rows = self.sp_flag_rows
        if not rows:
            return None
        return any(r.get("prior") != r.get("after") for r in rows)

    @property
    def effective_change_persisted(self) -> bool:
        """Change-persisted for display: the real related-table comparison for sp_managed

        bits (where the generic verify column never moves), else the verify column's own
        before/after comparison.
        """
        if self.sp_rolled_back:
            return False
        if self.sp_managed:
            if self.sp_change_detected is not None:
                return self.sp_change_detected
            return self.has_sp_rollback and not self.sp_rolled_back and self.status == "PASS"
        return self.change_persisted

    @property
    def verdict_code(self) -> str:
        """Precise, CAB-facing outcome that never implies a config was changed.

        Distinguishes *the procedure executed safely under simulation* from *the
        configuration was verified as changed*, which is the core of CAB issue #5.
        """
        if self.is_negative:
            if self.status == "BLOCKED":
                return "BLOCKED"
            return "REJECTED-AS-EXPECTED" if self.status == "PASS" else "NOT-REJECTED"
        if self.status == "BLOCKED":
            return "BLOCKED"
        if self.status == "FAIL":
            return "FAIL"
        if self.status == "REVIEW":
            return "REVIEW"
        # status == PASS
        if self.mode == "SIMULATION":
            return "SIMULATED-OK"
        return "APPLIED"

    @property
    def verdict_detail(self) -> str:
        """One-sentence justification for :pyattr:`verdict_code`."""
        code = self.verdict_code
        proposed = (
            "the procedure returned a simulation/preview result set"
            if self.proposed_change_observed
            else "the procedure returned no preview result set"
        )
        details = {
            "SIMULATED-OK": (
                f"Procedure executed under @is_simulation=1, {proposed}, and the enclosing "
                "transaction was rolled back — the verified column was unchanged afterwards, "
                "so nothing was persisted. This proves the call ran, not that a config changed."
            ),
            "APPLIED": (
                "Live call committed and a fresh handler read confirms the value "
                "changed; rollback restores the original values through the SP JSON interface."
                if self.sp_managed
                else "Live call committed and the verified column changed from its pre-test "
                "value, confirming the configuration was actually applied."
            ),
            "REVIEW": (
                "Live call completed without error but the verified column did not change — "
                "the procedure accepted the call without acting; inspect the target and value."
            ),
            "BLOCKED": (
                "The connected login lacked EXECUTE, so the procedure body never ran. This is "
                "an environment/permission gap, not a procedure defect."
            ),
            "FAIL": f"The call raised a SQL error: {self.error or 'see error field'}.",
            "REJECTED-AS-EXPECTED": (
                "Deliberately invalid input was rejected by the procedure (SQL exception or "
                "ERROR trace), proving the validation path works."
            ),
            "NOT-REJECTED": (
                "Deliberately invalid input was NOT rejected — the procedure accepted or "
                "silently ignored it. This must be reviewed with the procedure owner."
            ),
        }
        return details.get(code, "")

    def export(self) -> dict:
        payload = {
            key: value for key, value in self.__dict__.items() if key != "grid_frames"
        }
        payload["can_rollback"] = self.can_rollback
        payload["verdict_code"] = self.verdict_code
        payload["verdict_detail"] = self.verdict_detail
        payload["effective_change_persisted"] = self.effective_change_persisted
        return payload


def build_call(
    definition: TestDefinition,
    target_identifier: str,
    config_value: str,
    simulation: bool,
) -> tuple[str, tuple, str]:
    """Return ``(sql, params, rendered_sql)`` for one catalogue entry."""
    bit = definition.bit
    sim = 1 if simulation else 0
    payload = json.dumps([{definition.json_field: target_identifier}])

    if bit == 1:
        sql = (
            f"EXEC {PROCEDURE} @display_config = ?, @location_json = ?, "
            "@extra_function_name = ?, @add = ?, @is_simulation = ?"
        )
        params = (
            bit,
            payload,
            definition.function_name or "DCCXpressCO",
            1 if config_value == "Add" else 0,
            sim,
        )
    elif bit == 2:
        sql = (
            f"EXEC {PROCEDURE} @display_config = ?, @terminal_json = ?, "
            "@ConfigDownloadVersionDesc = ?, @is_simulation = ?"
        )
        params = (bit, payload, config_value, sim)
    elif bit == 4:
        sql = (
            f"EXEC {PROCEDURE} @display_config = ?, @terminal_json = ?, "
            "@FirmwarePackageName = ?, @is_simulation = ?"
        )
        params = (bit, payload, config_value, sim)
    elif bit == 8:
        sql = (
            f"EXEC {PROCEDURE} @display_config = ?, @instance_json = ?, "
            "@Extra_Config_Name = ?, @Config_value = ?, @is_simulation = ?"
        )
        expected = definition.bit8_config_value
        if expected is None:
            expected = config_value not in BIT8_FALSE_FLAGS
        params = (bit, payload, config_value, int(expected), sim)
    elif bit == 16:
        sql = (
            f"EXEC {PROCEDURE} @display_config = ?, @instance_json = ?, "
            "@printout_type_Template_DCC = ?, @add_bit16 = ?, @is_simulation = ?"
        )
        if config_value == "Remove":
            template_name, add_flag = None, 0
        else:
            _, _, template_name = config_value.partition(":")
            add_flag = 1
        params = (bit, payload, template_name, add_flag, sim)
    else:
        raise ValueError(f"Unsupported display_config bit: {bit}")

    sql += ", @is_rollback = 0"
    sql = _wrap_with_return_code(sql)
    rendered = sql
    for value in params:
        rendered = rendered.replace("?", repr(value), 1)
    return sql, params, rendered


def _wrap_with_return_code(exec_sql: str) -> str:
    """Wrap an ``EXEC proc ...`` so the procedure's integer return code is captured.

    Turns ``EXEC <proc> ...`` into::

        DECLARE @dcc_rc INT;
        EXEC @dcc_rc = <proc> ...;
        SELECT @dcc_rc AS dcc_return_code;

    No parameter placeholders are added, so binding is unchanged. The trailing
    SELECT is side-effect free and is stripped from the preview grids by the
    database layer (recognised by the ``dcc_return_code`` sentinel column).
    """
    marker = f"EXEC {PROCEDURE}"
    if marker not in exec_sql:
        return exec_sql
    captured = exec_sql.replace(marker, f"EXEC @dcc_rc = {PROCEDURE}", 1)
    return (
        f"DECLARE @dcc_rc INT;\n{captured};\n"
        f"SELECT @dcc_rc AS {RETURN_CODE_COLUMN};"
    )


ROLLBACK_SCRIPT_COLUMN = "rollback_script"


def _extract_rollback_scripts(grids: list[pd.DataFrame]) -> list[str]:
    """Retain returned SQL as evidence only; rollback never executes it."""
    scripts: list[str] = []
    for frame in grids:
        if ROLLBACK_SCRIPT_COLUMN in getattr(frame, "columns", []):
            for value in frame[ROLLBACK_SCRIPT_COLUMN].tolist():
                if value is not None and str(value).strip():
                    scripts.append(str(value))
    return scripts


def classify(simulation: bool, error: str | None, persisted: bool) -> str:
    """Apply the harness pass/fail rules."""
    if error and PERMISSION_DENIED_MARKER in error:
        return "BLOCKED"
    if error:
        return "FAIL"
    if simulation:
        # A correct simulation rolls back, so nothing may remain changed.
        return "FAIL" if persisted else "PASS"
    return "PASS" if persisted else "REVIEW"


def run_test(
    connection: DatabaseConnection,
    definition: TestDefinition,
    target_identifier: str,
    config_value: str,
    simulation: bool,
    environment: str,
    login: str,
    campaign_id: str | None = None,
    trace_signature: TraceSignature | None = None,
) -> TestResult:
    sql, params, rendered = build_call(definition, target_identifier, config_value, simulation)

    before = read_state(connection, definition, target_identifier)
    started = datetime.now()

    # Display evidence; the strict snapshot below separately enforces live rollback capture.
    sp_prior_states: list[dict] = []
    if definition.sp_managed:
        sp_flag_name = config_value if definition.sp_value_is_flag_name else None
        sp_prior_states = read_sp_flag_states(
            connection, target_identifier, sp_flag_name, column=definition.sp_column
        )

    error: str | None = None
    grids: list[pd.DataFrame] = []
    messages: list[str] = []
    return_code: int | None = None
    rollback_log: list[dict] = []
    journal_id: int | None = None
    rollback_plan: dict = {}
    rollback_capture_error: str | None = None
    try:
        rollback_plan = capture_rollback_plan(
            connection, definition, target_identifier, config_value
        )
        if not definition.sp_managed:
            before = rollback_plan["originals"][0]["original"]
    except Exception as exc:
        rollback_capture_error = f"Could not capture an exact SP JSON restore point: {exc}"
        if not simulation:
            error = rollback_capture_error

    # Write restore point to journal BEFORE live execution on UAT/PROD.
    journal = get_journal()
    if not simulation and not error and journal.is_journaled(environment):
        journal_id = journal.record_restore_point(
            environment=environment,
            server=connection.server,
            database_name=connection.database,
            login=login,
            test_key=definition.key,
            bit=definition.bit,
            target_type=definition.target,
            target=target_identifier,
            config_value=config_value,
            original_value=before,
            restore_sql="",
            campaign_id=campaign_id,
            rollback_plan=rollback_plan,
        )

    if not error:
        try:
            output = connection.call_procedure(sql, params, rollback=simulation)
            grids, messages = output.grids, output.messages
            return_code = output.return_code
            if return_code not in (None, 0):
                error = f"Procedure returned failure code {return_code}."
        except Exception as exc:
            error = str(exc)
            connection.safe_rollback()

    # The procedure surfaces its trace via PRINT db.fnDisplayTrace(...); those
    # lines arrive on the message stream, which we already captured above.
    # Validation failures may surface here instead of as a raised SQL exception.
    trace = trace_from_messages(messages)

    after = read_state(connection, definition, target_identifier)
    persisted = before != after

    # Handler-level snapshot AFTER the call (sp_managed bits only) — shows the
    # value actually moved on the correct table, independent of the instance column.
    sp_after_states: list[dict] = []
    if definition.sp_managed:
        sp_flag_name = config_value if definition.sp_value_is_flag_name else None
        sp_after_states = read_sp_flag_states(
            connection, target_identifier, sp_flag_name, column=definition.sp_column
        )

    # The procedure's own simulation/preview result set, a server message, or its
    # internal trace is the evidence that the intended change was computed.
    proposed_change_observed = bool(grids) or bool(messages) or bool(trace.rows)

    sp_rollback_scripts = _extract_rollback_scripts(grids)

    if rollback_plan and not rollback_capture_error:
        try:
            current = capture_rollback_plan(connection, definition, target_identifier, config_value)
            if not definition.sp_managed:
                after = current["originals"][0]["original"]
                persisted = before != after
        except Exception as exc:
            error = error or f"Could not verify post-call original-value state: {exc}"
    sp_changed = bool(
        definition.sp_managed and sp_prior_states and sp_after_states
        and not flag_states_match(sp_prior_states, sp_after_states)
    )
    sp_applied = not simulation and sp_changed

    status = classify(simulation, error, persisted or sp_changed)

    if simulation:
        transaction = "ROLLED BACK (simulation)"
    elif any(entry["trigger"] == "automatic" and entry["ok"] for entry in rollback_log):
        transaction = "COMMITTED then AUTO-ROLLED BACK"
    elif rollback_capture_error:
        transaction = "NOT EXECUTED (no exact restore point)"
    else:
        transaction = "COMMITTED"

    # Update journal: mark resolved if auto-rolled back or genuinely no change.
    if journal_id is not None:
        if not error and not (persisted or sp_applied) and (
            not definition.sp_managed or (sp_prior_states and sp_after_states)
        ):
            journal.mark_not_needed(journal_id)
        elif any(e.get("trigger") == "automatic" and e.get("ok") for e in rollback_log):
            journal.mark_resolved(journal_id)

    result = TestResult(
        id=f"{definition.bit}-{started.strftime('%H%M%S%f')}",
        test_key=definition.key,
        bit=definition.bit,
        environment=environment,
        login=login,
        target_type=definition.target,
        target=target_identifier,
        value=config_value,
        mode="SIMULATION" if simulation else "LIVE",
        status=status,
        error=error,
        state_before=before,
        state_after=after,
        restore_point=before,
        change_persisted=persisted,
        transaction=transaction,
        sql=rendered,
        messages=messages,
        grids=[frame.to_dict("records") for frame in grids],
        grid_frames=grids,
        duration_s=round((datetime.now() - started).total_seconds(), 3),
        timestamp=started.isoformat(timespec="seconds"),
        rollback_log=rollback_log,
        campaign_id=campaign_id,
        input_json=params[1] if len(params) > 1 else "",
        params=list(params),
        proc_args=definition.proc_args,
        proposed_change_observed=proposed_change_observed,
        journal_id=journal_id,
        trace_rows=trace.rows,
        trace_status=trace.status,
        trace_reason=trace.reason,
        return_code=return_code,
        sp_rollback_scripts=sp_rollback_scripts,
        sp_managed=definition.sp_managed,
        sp_prior_states=sp_prior_states,
        sp_after_states=sp_after_states,
        rollback_plan=rollback_plan,
        rollback_capture_error=rollback_capture_error,
        server=connection.server,
        database_name=connection.database,
    )
    if not simulation and error and rollback_plan and (persisted or sp_changed):
        apply_rollback(connection, definition, result, trigger="automatic")
    return result


def apply_rollback(
    connection: DatabaseConnection,
    definition: TestDefinition,
    result: TestResult,
    trigger: str = "manual",
) -> RollbackOutcome:
    """Restore through the SP JSON interface shared by every console workflow."""
    reason = None
    if not result.is_live:
        reason = "Refusing rollback for a SIMULATION result; nothing was committed."
    elif result.sp_rolled_back:
        reason = "This result has already been rolled back; refusing to replay it."
    elif not result.rollback_plan:
        reason = (
            "No structured JSON restore point was captured. Legacy SQL rollback is disabled; "
            "do not execute the returned UPDATE scripts."
        )
    elif (
        (result.server and result.server.casefold() != connection.server.casefold())
        or (result.database_name
            and result.database_name.casefold() != connection.database.casefold())
    ):
        reason = "Connect to the original server/database before rolling back this result."
    elif (
        definition.key != result.test_key
        or result.rollback_plan.get("test_key") != result.test_key
        or result.rollback_plan.get("target") != result.target
    ):
        reason = "The rollback definition or target does not match this result."
    if reason:
        outcome = RollbackOutcome(
            False, 0, "", trigger, reason, method=METHOD_PROCEDURE_JSON
        )
    else:
        outcome = restore_via_payload(connection, result.rollback_plan, trigger)
    result.rollback_log.append(outcome.as_dict())
    if outcome.ok:
        result.state_after = (
            read_state(connection, definition, result.target)
            if definition.sp_managed else result.rollback_plan["originals"][0]["original"]
        )
        result.change_persisted = False
        result.transaction = f"COMMITTED then {trigger.upper()} ROLLED BACK (SP JSON)"
        if definition.sp_managed:
            flag = result.value if definition.sp_value_is_flag_name else None
            result.sp_restored_states = read_sp_flag_states(
                connection, result.target, flag, column=definition.sp_column
            )
            result.sp_after_states = result.sp_restored_states
            result.sp_flag_verified = True
        if result.journal_id is not None:
            get_journal().mark_resolved(result.journal_id)
    return outcome


@dataclass
class RollbackEvidenceResult:
    """Full before→change→restore cycle for CAB Issue #6."""

    test_key: str
    target: str
    environment: str
    state_before: object
    state_after_apply: object
    state_after_rollback: object
    change_detected: bool
    rollback_successful: bool
    full_cycle_proven: bool
    live_result: TestResult
    rollback_outcome: RollbackOutcome | None
    error: str | None = None

    def export(self) -> dict:
        return {
            "test_key": self.test_key,
            "target": self.target,
            "environment": self.environment,
            "state_before": str(self.state_before)[:500] if self.state_before else None,
            "state_after_apply": (
                str(self.state_after_apply)[:500] if self.state_after_apply else None
            ),
            "state_after_rollback": (
                str(self.state_after_rollback)[:500] if self.state_after_rollback else None
            ),
            "change_detected": self.change_detected,
            "rollback_successful": self.rollback_successful,
            "full_cycle_proven": self.full_cycle_proven,
            "error": self.error,
        }


def run_rollback_evidence_test(
    connection: DatabaseConnection,
    definition: TestDefinition,
    target_identifier: str,
    config_value: str,
    environment: str,
    login: str,
) -> RollbackEvidenceResult:
    """Run a live apply + manual rollback to prove the full cycle for CAB.

    Sequence:
        1. Read state BEFORE
        2. Execute procedure in LIVE mode (commits)
        3. Read state AFTER APPLY
        4. Immediately restore through the SP's JSON rollback operation
        5. Read state AFTER ROLLBACK
        6. Verify: before == after_rollback (full cycle proven)
    """
    # Step 2: run live (commits)
    live_result = run_test(
        connection, definition, target_identifier, config_value,
        simulation=False, environment=environment, login=login,
    )
    state_before = (
        live_result.sp_display_before if definition.sp_managed else live_result.state_before
    )
    state_after_apply = (
        live_result.sp_display_after if definition.sp_managed else live_result.state_after
    )
    change_detected = live_result.effective_change_persisted

    # Step 3: rollback if change was applied
    rollback_outcome: RollbackOutcome | None = None
    error: str | None = live_result.error
    if change_detected and live_result.can_rollback:
        rollback_outcome = apply_rollback(connection, definition, live_result, trigger="evidence")
        state_after_rollback = (
            live_result.sp_display_after if definition.sp_managed else live_result.state_after
        )
        if not rollback_outcome.ok:
            error = rollback_outcome.error
    elif not change_detected and not error:
        state_after_rollback = state_after_apply
        error = "Procedure did not change the verified column — rollback not needed."
    else:
        state_after_rollback = read_state(connection, definition, target_identifier)

    rollback_successful = (
        rollback_outcome is not None and rollback_outcome.ok
    ) if rollback_outcome else False
    full_cycle = rollback_successful and change_detected and not error

    return RollbackEvidenceResult(
        test_key=definition.key,
        target=target_identifier,
        environment=environment,
        state_before=state_before,
        state_after_apply=state_after_apply,
        state_after_rollback=state_after_rollback,
        change_detected=change_detected,
        rollback_successful=rollback_successful,
        full_cycle_proven=full_cycle,
        live_result=live_result,
        rollback_outcome=rollback_outcome,
        error=error,
    )
