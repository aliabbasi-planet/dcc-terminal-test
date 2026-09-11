"""Build and run one configuration test against the stored procedure."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .catalog import TestDefinition
from .config import PROCEDURE
from .database import DatabaseConnection
from .rollback import RollbackOutcome, read_state, restore_state

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

    def export(self) -> dict:
        payload = {
            key: value for key, value in self.__dict__.items() if key != "grid_frames"
        }
        payload["can_rollback"] = self.can_rollback
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
        params = (bit, payload, "DCCXpressCO", 1 if config_value == "Add" else 0, sim)
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

    try:
        output = connection.call_procedure(sql, params, rollback=simulation)
        grids, messages = output.grids, output.messages
    except Exception as exc:
        error = str(exc)
        connection.safe_rollback()

    after = read_state(connection, definition, target_identifier)
    persisted = before != after

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
    return outcome
