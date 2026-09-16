"""Negative validation battery for the DCC enablement procedure.

CAB issue #4 asks for proof that the procedure's *validation* logic works, not
only its happy path. Each :class:`NegativeCase` sends deliberately invalid input
and the expected outcome is a **rejection** — a raised SQL exception or an ERROR
trace. A rejection is the pass condition; silent acceptance of bad input is a
finding.

Every negative run is forced to ``@is_simulation = 1`` and its transaction is
rolled back, so a bad input can never be persisted even if the procedure fails
to reject it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import pandas as pd

from .catalog import TEST_CATALOG, TestDefinition
from .database import DatabaseConnection
from .execution import PERMISSION_DENIED_MARKER, TestResult, build_call
from .rollback import read_state

# Substrings that, seen in a server message or a returned grid, indicate the
# procedure actively rejected the input rather than silently accepting it.
REJECTION_MARKERS: tuple[str, ...] = (
    "error",
    "not found",
    "does not exist",
    "doesn't exist",
    "invalid",
    "cannot",
    "missing",
    "rejected",
    "not a valid",
    "no matching",
    "unknown",
)


@dataclass(frozen=True)
class NegativeCase:
    """One invalid-input scenario expected to be rejected by the procedure."""

    name: str
    base_key: str
    target: str
    value: str
    invalid_reason: str
    expected: str
    empty_payload: bool = False
    function_override: str | None = None

    @property
    def definition(self) -> TestDefinition:
        base = TEST_CATALOG[self.base_key]
        if self.function_override is not None:
            return replace(base, function_name=self.function_override)
        return base


def default_negative_cases(
    valid_instance: str = "I000000001",
    valid_location: str = "0000000",
    valid_terminal: str | None = None,
) -> list[NegativeCase]:
    """Return the standard battery.

    Value-based negatives target a *valid* row so the rejection can only be
    attributed to the bad value; target-based negatives use non-existent ids.
    """
    terminal_for_version = valid_terminal or "99999999"
    version_reason = (
        "invalid ConfigDownloadVersionDesc on a valid terminal"
        if valid_terminal
        else "invalid ConfigDownloadVersionDesc (and a non-existent terminal)"
    )
    return [
        NegativeCase(
            name="Invalid instance identifier",
            base_key="Bit 8 — DCC Handler Flags (instance)",
            target="I000099999",
            value="dccEnable",
            invalid_reason="instance_identifier that does not exist",
            expected="Procedure rejects the call (no such instance).",
        ),
        NegativeCase(
            name="Invalid terminal identifier",
            base_key="Bit 2 — Config Download Version (terminal)",
            target="99999999",
            value="Standard",
            invalid_reason="terminal_identifier that does not exist",
            expected="Procedure rejects the call (no such terminal).",
        ),
        NegativeCase(
            name="Invalid location number",
            base_key="Bit 1 — DCC Xpress CO (location extra_function)",
            target="9999999",
            value="Add",
            invalid_reason="location_no that does not exist",
            expected="Procedure rejects the call (no such location).",
        ),
        NegativeCase(
            name="Invalid handler flag name",
            base_key="Bit 8 — DCC Handler Flags (instance)",
            target=valid_instance,
            value="dccNotARealFlag",
            invalid_reason="Extra_Config_Name that is not a known handler flag",
            expected="Procedure rejects the unknown configuration name.",
        ),
        NegativeCase(
            name="Invalid location function name",
            base_key="Bit 1 — DCC Xpress CO (location extra_function)",
            target=valid_location,
            value="Add",
            invalid_reason="extra_function_name that is not a known DCC function",
            expected="Procedure rejects the unknown extra_function name.",
            function_override="DCCNotARealFunction",
        ),
        NegativeCase(
            name="Invalid config download version",
            base_key="Bit 2 — Config Download Version (terminal)",
            target=terminal_for_version,
            value="NotAVersion",
            invalid_reason=version_reason,
            expected="Procedure rejects the unknown version description.",
        ),
        NegativeCase(
            name="Missing target JSON (empty array)",
            base_key="Bit 8 — DCC Handler Flags (instance)",
            target=valid_instance,
            value="dccEnable",
            invalid_reason="empty JSON target array — no target supplied",
            expected="Procedure rejects or no-ops the empty target set.",
            empty_payload=True,
        ),
    ]


def _render(sql: str, params: tuple) -> str:
    rendered = sql
    for value in params:
        rendered = rendered.replace("?", repr(value), 1)
    return rendered


def build_negative_call(case: NegativeCase) -> tuple[str, tuple, str]:
    """Build the EXEC for a negative case, always in simulation mode."""
    sql, params, rendered = build_call(case.definition, case.target, case.value, simulation=True)
    if case.empty_payload:
        mutated = list(params)
        mutated[1] = "[]"
        params = tuple(mutated)
        rendered = _render(sql, params)
    return sql, params, rendered


def _looks_rejected(messages: list[str], grids: list[pd.DataFrame]) -> bool:
    haystacks: list[str] = list(messages)
    for frame in grids:
        try:
            haystacks.append(frame.to_csv(index=False))
        except Exception:
            haystacks.append(str(frame))
    blob = " ".join(haystacks).lower()
    return any(marker in blob for marker in REJECTION_MARKERS)


def run_negative(
    connection: DatabaseConnection,
    case: NegativeCase,
    environment: str,
    login: str,
    campaign_id: str | None = None,
) -> TestResult:
    """Execute one negative case; a rejection is the pass condition."""
    definition = case.definition
    sql, params, rendered = build_negative_call(case)

    before = read_state(connection, definition, case.target)
    started = datetime.now()
    error: str | None = None
    grids: list[pd.DataFrame] = []
    messages: list[str] = []

    try:
        output = connection.call_procedure(sql, params, rollback=True)
        grids, messages = output.grids, output.messages
    except Exception as exc:
        error = str(exc)
        connection.safe_rollback()

    after = read_state(connection, definition, case.target)
    persisted = before != after

    if error and PERMISSION_DENIED_MARKER in error:
        status = "BLOCKED"
    elif error:
        # A raised exception is the clearest possible rejection.
        status = "PASS"
    elif _looks_rejected(messages, grids):
        # The procedure reported the problem through its trace/result set.
        status = "PASS"
    elif persisted:
        # Invalid input was actually applied — the worst outcome for a negative.
        status = "FAIL"
    else:
        # No error, no rejection marker, nothing persisted: bad input was accepted
        # without complaint. Needs the procedure owner to confirm intended behaviour.
        status = "REVIEW"

    return TestResult(
        id=f"neg-{definition.bit}-{started.strftime('%H%M%S%f')}",
        test_key=f"Negative — {case.name}",
        bit=definition.bit,
        environment=environment,
        login=login,
        target_type=definition.target,
        target=case.target if not case.empty_payload else "(empty JSON)",
        value=case.value,
        mode="SIMULATION",
        status=status,
        error=error,
        state_before=before,
        state_after=after,
        restore_point=before,
        change_persisted=persisted,
        transaction="ROLLED BACK (simulation)",
        sql=rendered,
        messages=messages,
        grids=[frame.to_dict("records") for frame in grids],
        grid_frames=grids,
        duration_s=round((datetime.now() - started).total_seconds(), 3),
        timestamp=started.isoformat(timespec="seconds"),
        campaign_id=campaign_id,
        input_json=params[1] if len(params) > 1 else "",
        params=list(params),
        proc_args=definition.proc_args,
        proposed_change_observed=bool(grids) or bool(messages),
        is_negative=True,
    )


def run_negative_battery(
    connection: DatabaseConnection,
    environment: str,
    login: str,
    cases: list[NegativeCase] | None = None,
    campaign_id: str | None = None,
) -> list[TestResult]:
    battery = cases if cases is not None else default_negative_cases()
    return [
        run_negative(connection, case, environment, login, campaign_id=campaign_id)
        for case in battery
    ]


__all__ = [
    "NegativeCase",
    "default_negative_cases",
    "build_negative_call",
    "run_negative",
    "run_negative_battery",
    "REJECTION_MARKERS",
]
