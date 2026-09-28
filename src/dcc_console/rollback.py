"""Restore the pre-test value of a verified column.

A rollback is only ever an `UPDATE` of the one column declared in
:mod:`dcc_console.catalog`, using the value captured before the test ran.
Table, column, key and cast come from the catalogue; the value and the row key
are bound as parameters.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalog import TestDefinition
from .database import DatabaseConnection

logger = logging.getLogger(__name__)

# The handler types a Bit 8 call touches, taken verbatim from the procedure's own
# selection query. Used only to READ the same rows back for verification evidence.
HANDLER_TYPE_FILTER = (
    "handlerTerminalRequester",
    "handlerTerminalRequesterCale",
    "handlerTerminalRequesterNexo",
    "handlerTerminalRequesterPayAtTable",
    "handlerTerminalRequesterServiceApi",
    "handlerTerminalStandalone",
)

_CONFIG_VALUE_RE = re.compile(r'config_value="([^"]*)"')


@dataclass
class RollbackOutcome:
    ok: bool
    rows: int
    sql: str
    trigger: str
    error: str | None = None
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "rows": self.rows,
            "sql": self.sql,
            "trigger": self.trigger,
            "error": self.error,
            "at": self.at,
        }


def restore_statement(definition: TestDefinition) -> str:
    verify = definition.verify
    # Identifiers come from the frozen catalogue, never from user input; the value
    # and the row key are bound parameters.
    return (
        f"UPDATE {verify.table} SET {verify.column} = CONVERT({verify.cast}, ?) "  # noqa: S608
        f"WHERE CONVERT(nvarchar(50), {verify.key}) = ?"
    )


def read_statement(definition: TestDefinition) -> str:
    """The exact SELECT used to read the verified column before/after a call."""
    verify = definition.verify
    # Identifiers are catalogue constants; the row key is a bound parameter.
    return (
        f"SELECT TOP (1) CONVERT(nvarchar(max), {verify.column}) "  # noqa: S608
        f"FROM {verify.table} "
        f"WHERE CONVERT(nvarchar(50), {verify.key}) = ?"
    )


def read_state(connection: DatabaseConnection, definition: TestDefinition, identifier: str):
    """Read the verified column for one row, or ``None`` if it cannot be read."""
    sql = read_statement(definition)
    try:
        return connection.scalar(sql, (identifier,))
    except Exception as exc:
        logger.warning("State read failed for %s: %s", identifier, exc)
        return None


def restore_state(
    connection: DatabaseConnection,
    definition: TestDefinition,
    identifier: str,
    original_value,
    trigger: str,
) -> RollbackOutcome:
    """Write ``original_value`` back and verify it landed."""
    sql = restore_statement(definition)
    try:
        affected = connection.execute_write(sql, (original_value, identifier))
    except Exception as exc:
        logger.error("Restore failed for %s: %s", identifier, exc)
        return RollbackOutcome(ok=False, rows=0, sql=sql, trigger=trigger, error=str(exc))

    verified = read_state(connection, definition, identifier) == original_value
    return RollbackOutcome(
        ok=verified,
        rows=affected,
        sql=sql,
        trigger=trigger,
        error=None if verified else "Update committed but the stored value still differs.",
    )


def restore_via_scripts(
    connection: DatabaseConnection,
    scripts: list[str],
    trigger: str,
) -> RollbackOutcome:
    """Roll back using the procedure's own returned compensating script(s).

    For sp_managed bits (e.g. Bit 8) the change is on a related table the generic
    verify column never sees, so the procedure emits its own `rollback_script`.
    We run those verbatim — they are authored by the trusted procedure, target the
    correct row(s) and prior value, and require no schema assumptions here.

    Value-level re-verification of the flag is not attempted, because the affected
    table/key is procedure-owned and varies; success is reported from rows affected.
    """
    joined = "\n".join(s for s in scripts if str(s).strip())
    if not joined:
        return RollbackOutcome(
            ok=False, rows=0, sql="", trigger=trigger,
            error="No procedure rollback script was captured for this result.",
        )
    try:
        affected = connection.execute_batch(list(scripts))
    except Exception as exc:
        logger.error("Procedure rollback script failed: %s", exc)
        return RollbackOutcome(ok=False, rows=0, sql=joined, trigger=trigger, error=str(exc))

    return RollbackOutcome(
        ok=affected > 0,
        rows=affected,
        sql=joined,
        trigger=trigger,
        error=None if affected > 0 else "Rollback script committed but affected 0 rows.",
    )


def extract_flag_value(extra_config: str, flag_name: str) -> str | None:
    """Return the ``config_value`` for a named flag inside an ``extra_config`` XML string.

    Parses defensively — no XML library, no attribute-order assumption: find the
    element that declares ``config_name="<flag>"`` and read its ``config_value``.
    Returns None if the flag is absent or the value can't be read. Value-agnostic,
    so it works for boolean flags and any other stored value alike.
    """
    if not extra_config or not flag_name:
        return None
    needle = f'config_name="{flag_name}"'
    for element in str(extra_config).split("<"):
        if needle in element:
            match = _CONFIG_VALUE_RE.search(element)
            if match:
                return match.group(1)
    return None


def read_sp_flag_states(
    connection: DatabaseConnection,
    instance_identifier: str,
    flag_name: str | None,
    column: str = "extra_config",
) -> list[dict]:
    """Read the current handler-level value on every handler an sp_managed call targets.

    Uses the procedure's own handler<->instance join (``instance_id``) and the same
    ``handler_type`` filter, so we inspect exactly the rows the procedure changes.
    ``column`` is always one of the catalogue's own fixed constants (``extra_config``
    for Bit 8, ``receipt_config`` for Bit 16) — never built from user input.

    When ``flag_name`` is given, the named ``config_name``/``config_value`` pair is
    extracted from the document (Bit 8's boolean handler flags). When it is
    ``None``, the whole document is returned as-is (Bit 16's receipt template,
    which is not a named flag lookup).

    This is additive evidence only: any failure returns ``[]`` so a rollback is
    never affected by a verification-read problem.
    """
    if not instance_identifier:
        return []
    placeholders = ", ".join(["?"] * len(HANDLER_TYPE_FILTER))
    # Safe: `column` is a catalogue constant (never user input); only `?`
    # placeholders are interpolated (fixed count) with bound values.
    sql = (
        f"SELECT h.handler_name, CONVERT(nvarchar(max), h.{column}) AS {column} "  # noqa: S608
        "FROM [cccintegrang].[handler] h WITH (NOLOCK) "
        "INNER JOIN [cccintegrang].[instance] i WITH (NOLOCK) ON i.instance_id = h.instance_id "
        "INNER JOIN [cccintegrang].[handler_type] ht WITH (NOLOCK) "
        "ON ht.handler_type_id = h.handler_type_id "
        "WHERE CONVERT(nvarchar(50), i.instance_identifier) = ? "
        f"AND ht.handler_type_identifier IN ({placeholders})"
    )
    try:
        frame = connection.query(sql, (instance_identifier, *HANDLER_TYPE_FILTER))
    except Exception as exc:
        logger.warning("Handler-level verification read failed: %s", exc)
        return []
    states: list[dict] = []
    for _, row in frame.iterrows():
        raw = row.get(column)
        value = extract_flag_value(raw, flag_name) if flag_name else raw
        states.append(
            {
                "handler_name": row.get("handler_name"),
                "flag_value": value,
            }
        )
    return states


def flag_states_match(prior: list[dict], restored: list[dict]) -> bool:
    """True if every handler's flag value in ``restored`` equals its value in ``prior``.

    Requires the same set of handlers on both sides; empty input is treated as
    "cannot confirm" (False) so we never claim verification we didn't perform.
    """
    if not prior or not restored:
        return False
    prior_by_handler = {s["handler_name"]: s["flag_value"] for s in prior}
    restored_by_handler = {s["handler_name"]: s["flag_value"] for s in restored}
    if set(prior_by_handler) != set(restored_by_handler):
        return False
    return all(prior_by_handler[h] == restored_by_handler[h] for h in prior_by_handler)
