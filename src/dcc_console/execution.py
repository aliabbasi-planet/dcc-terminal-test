"""Build and run one configuration test against the stored procedure."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .catalog import TestDefinition
from .config import PROCEDURE, RETURN_CODE_COLUMN
from .database import DatabaseConnection
from .journal import get_journal
from .rollback import (
    RollbackOutcome,
    flag_states_match,
    read_sp_flag_states,
    read_state,
    restore_state,
    restore_statement,
    restore_via_scripts,
)
from .trace import TraceSignature, trace_from_messages

PERMISSION_DENIED_MARKER = "EXECUTE permission was denied"


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
    # Compensating UPDATE(s) the procedure returned in its `rollback_script` result
    # set. For sp_managed bits (e.g. Bit 8) these are the authoritative rollback,
    # because the change is on a related table the generic verify column never sees.
    sp_rollback_scripts: list[str] = field(default_factory=list)
    # True when this test's bit is procedure-managed (see TestDefinition.sp_managed).
    sp_managed: bool = False
    # Handler-level flag verification (Bit 8): the named flag's value on every
    # affected handler, read via the procedure's own instance_id/handler_type join,
    # before the call, after the call, and after rollback. Additive CAB evidence.
    sp_prior_states: list[dict] = field(default_factory=list)
    sp_after_states: list[dict] = field(default_factory=list)
    sp_restored_states: list[dict] = field(default_factory=list)
    # True/False once a rollback has been verified against prior; None = not yet
    # attempted or the verification read was unavailable.
    sp_flag_verified: bool | None = None

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
        return self.restore_point is not None

    @property
    def can_rollback(self) -> bool:
        """Live run, a captured restore point, and a value that still differs."""
        return self.is_live and self.restore_point_known and self.change_persisted

    @property
    def has_sp_rollback(self) -> bool:
        """Live run for which the procedure returned its own compensating script.

        Used to offer rollback for procedure-managed bits (e.g. Bit 8) where the
        generic verify column never moves, so ``can_rollback`` would be False even
        though a change was committed and can be undone.
        """
        return self.is_live and bool(self.sp_rollback_scripts)

    @property
    def rollback_available(self) -> bool:
        """Either the generic column-restore or the procedure's own script applies."""
        return self.can_rollback or self.has_sp_rollback

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
                row = by_handler.setdefault(name, {"handler_name": name})
                row[label] = state.get("flag_value")
        return list(by_handler.values())

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
                "Live call committed and the procedure's own output confirms the handler "
                "flag was changed; roll back with the procedure's returned script."
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
        params = (bit, payload, config_value, 1, sim)
    elif bit == 16:
        sql = (
            f"EXEC {PROCEDURE} @display_config = ?, @instance_json = ?, "
            "@printout_type_Template_DCC = ?, @is_simulation = ?"
        )
        params = (bit, payload, config_value, sim)
    else:
        raise ValueError(f"Unsupported display_config bit: {bit}")

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
    """Collect the procedure's own compensating UPDATE(s) from its result sets.

    The procedure returns a ``rollback_script`` column (one row per affected
    handler). These are authored by the procedure and target the correct table
    and prior value, so they are the authoritative rollback for sp_managed bits.
    """
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

    # Handler-level flag snapshot BEFORE the call (Bit 8 only). Read via the
    # procedure's own join; additive evidence, never blocks the test.
    sp_prior_states: list[dict] = []
    if definition.sp_managed:
        sp_prior_states = read_sp_flag_states(connection, target_identifier, config_value)

    error: str | None = None
    grids: list[pd.DataFrame] = []
    messages: list[str] = []
    return_code: int | None = None
    rollback_log: list[dict] = []
    journal_id: int | None = None

    # Write restore point to journal BEFORE live execution on UAT/PROD.
    journal = get_journal()
    if not simulation and journal.is_journaled(environment):
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
            restore_sql=restore_statement(definition),
            campaign_id=campaign_id,
        )

    try:
        output = connection.call_procedure(sql, params, rollback=simulation)
        grids, messages = output.grids, output.messages
        return_code = output.return_code
    except Exception as exc:
        error = str(exc)
        connection.safe_rollback()

    # The procedure surfaces its trace via PRINT db.fnDisplayTrace(...); those
    # lines arrive on the message stream, which we already captured above.
    # Validation failures may surface here instead of as a raised SQL exception.
    trace = trace_from_messages(messages)

    after = read_state(connection, definition, target_identifier)
    persisted = before != after

    # Handler-level flag snapshot AFTER the call (Bit 8 only) — shows the flag
    # actually moved on the correct table, independent of the instance column.
    sp_after_states: list[dict] = []
    if definition.sp_managed:
        sp_after_states = read_sp_flag_states(connection, target_identifier, config_value)

    # The procedure's own simulation/preview result set, a server message, or its
    # internal trace is the evidence that the intended change was computed.
    proposed_change_observed = bool(grids) or bool(messages) or bool(trace.rows)

    # Capture the procedure's own compensating UPDATE(s). For sp_managed bits the
    # change is on a related table (e.g. handler.extra_config) that the generic
    # verify column never sees, so these scripts are the authoritative rollback.
    sp_rollback_scripts = _extract_rollback_scripts(grids)

    # For sp_managed bits the generic before/after read is not the column the
    # procedure edits, so trust the procedure's own signal: a returned rollback
    # script on a committed live call means it changed something.
    sp_applied = bool(
        definition.sp_managed and not simulation and sp_rollback_scripts and not error
    )

    # A failed live call must never leave a half-applied change behind.
    if not simulation and error and persisted and before is not None:
        auto: RollbackOutcome = restore_state(
            connection, definition, target_identifier, before, trigger="automatic"
        )
        rollback_log.append(auto.as_dict())
        after = read_state(connection, definition, target_identifier)
        persisted = before != after

    # `persisted` stays truthful about the verify column; classification also
    # accepts the procedure-managed signal so the verdict is not a false REVIEW.
    status = classify(simulation, error, persisted or sp_applied)

    if simulation:
        transaction = "ROLLED BACK (simulation)"
    elif any(entry["trigger"] == "automatic" and entry["ok"] for entry in rollback_log):
        transaction = "COMMITTED then AUTO-ROLLED BACK"
    else:
        transaction = "COMMITTED"

    # For sp_managed live changes, replace the journal's generic (wrong) restore
    # SQL with the procedure's own script so crash recovery restores the right row.
    if journal_id is not None and sp_applied and sp_rollback_scripts:
        journal.update_restore_sql(journal_id, "\n".join(sp_rollback_scripts))

    # Update journal: mark resolved if auto-rolled back or genuinely no change.
    if journal_id is not None:
        if not (persisted or sp_applied):
            journal.mark_not_needed(journal_id)
        elif any(e.get("trigger") == "automatic" and e.get("ok") for e in rollback_log):
            journal.mark_resolved(journal_id)

    return TestResult(
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
    )


def apply_rollback(
    connection: DatabaseConnection,
    definition: TestDefinition,
    result: TestResult,
    trigger: str = "manual",
) -> RollbackOutcome:
    """Restore the pre-test value and refresh the result in place.

    For procedure-managed bits (Bit 8) the change is on a related table the generic
    verify column never sees, so we prefer the procedure's own returned rollback
    script; otherwise we fall back to the generic column-restore.
    """
    if result.sp_rollback_scripts:
        # The procedure also returns a rollback_script under @is_simulation=1 as a
        # PREVIEW. That simulated change was never committed, so running the script
        # would be an unrequested write. Refuse rather than touch the database.
        if not result.is_live:
            return RollbackOutcome(
                ok=False,
                rows=0,
                sql="",
                trigger=trigger,
                error=(
                    "Refusing to run the procedure's rollback script for a SIMULATION "
                    "result — the simulated change was never committed, so there is "
                    "nothing to restore."
                ),
            )
        outcome = restore_via_scripts(connection, result.sp_rollback_scripts, trigger=trigger)
        result.rollback_log.append(outcome.as_dict())
        if outcome.ok:
            result.transaction = (
                f"COMMITTED then {trigger.upper()} ROLLED BACK (procedure script)"
            )
            # Handler-level verification: read the flag back and compare to prior.
            # Additive — a read problem leaves sp_flag_verified=None, never failing
            # the rollback that already committed.
            result.sp_restored_states = read_sp_flag_states(
                connection, result.target, result.value
            )
            if result.sp_prior_states and result.sp_restored_states:
                result.sp_flag_verified = flag_states_match(
                    result.sp_prior_states, result.sp_restored_states
                )
            else:
                result.sp_flag_verified = None
            if result.journal_id is not None:
                get_journal().mark_resolved(result.journal_id)
        return outcome

    outcome = restore_state(
        connection, definition, result.target, result.restore_point, trigger=trigger
    )
    result.rollback_log.append(outcome.as_dict())

    if outcome.ok:
        result.state_after = read_state(connection, definition, result.target)
        result.change_persisted = result.state_after != result.restore_point
        result.transaction = f"COMMITTED then {trigger.upper()} ROLLED BACK"
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
        4. Immediately rollback (compensating UPDATE)
        5. Read state AFTER ROLLBACK
        6. Verify: before == after_rollback (full cycle proven)
    """
    # Step 1: capture before
    state_before = read_state(connection, definition, target_identifier)

    # Step 2: run live (commits)
    live_result = run_test(
        connection, definition, target_identifier, config_value,
        simulation=False, environment=environment, login=login,
    )
    state_after_apply = live_result.state_after
    change_detected = live_result.change_persisted

    # Step 3: rollback if change was applied
    rollback_outcome: RollbackOutcome | None = None
    error: str | None = live_result.error
    if change_detected and live_result.can_rollback:
        rollback_outcome = apply_rollback(connection, definition, live_result, trigger="evidence")
        state_after_rollback = read_state(connection, definition, target_identifier)
    elif not change_detected and not error:
        state_after_rollback = state_after_apply
        error = "Procedure did not change the verified column — rollback not needed."
    else:
        state_after_rollback = read_state(connection, definition, target_identifier)

    rollback_successful = (
        rollback_outcome is not None and rollback_outcome.ok
    ) if rollback_outcome else False
    full_cycle = (
        state_before is not None
        and state_after_apply is not None
        and state_after_rollback is not None
        and str(state_before) == str(state_after_rollback)
        and change_detected
    )

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
