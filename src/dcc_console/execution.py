"""Build and run one configuration test against the stored procedure."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .catalog import TestDefinition
from .config import PROCEDURE
from .database import DatabaseConnection
from .journal import get_journal
from .rollback import RollbackOutcome, read_state, restore_state, restore_statement

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
                "Live call committed and the verified column changed from its pre-test value, "
                "confirming the configuration was actually applied."
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

    rendered = sql
    for value in params:
        rendered = rendered.replace("?", repr(value), 1)
    return sql, params, rendered


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
) -> TestResult:
    sql, params, rendered = build_call(definition, target_identifier, config_value, simulation)

    before = read_state(connection, definition, target_identifier)
    started = datetime.now()
    error: str | None = None
    grids: list[pd.DataFrame] = []
    messages: list[str] = []
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
    except Exception as exc:
        error = str(exc)
        connection.safe_rollback()

    after = read_state(connection, definition, target_identifier)
    persisted = before != after

    # The procedure's own simulation/preview result set (or a server message) is the
    # evidence that the intended change was computed — CAB issue #1/#6.
    proposed_change_observed = bool(grids) or bool(messages)

    # A failed live call must never leave a half-applied change behind.
    if not simulation and error and persisted and before is not None:
        auto: RollbackOutcome = restore_state(
            connection, definition, target_identifier, before, trigger="automatic"
        )
        rollback_log.append(auto.as_dict())
        after = read_state(connection, definition, target_identifier)
        persisted = before != after

    status = classify(simulation, error, persisted)

    if simulation:
        transaction = "ROLLED BACK (simulation)"
    elif any(entry["trigger"] == "automatic" and entry["ok"] for entry in rollback_log):
        transaction = "COMMITTED then AUTO-ROLLED BACK"
    else:
        transaction = "COMMITTED"

    # Update journal: mark resolved if auto-rolled back or no change.
    if journal_id is not None:
        if not persisted:
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
    )


def apply_rollback(
    connection: DatabaseConnection,
    definition: TestDefinition,
    result: TestResult,
    trigger: str = "manual",
) -> RollbackOutcome:
    """Restore the captured pre-test value and refresh the result in place."""
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
