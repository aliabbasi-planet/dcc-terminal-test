"""Restore the pre-test value of a verified column.

A rollback is only ever an `UPDATE` of the one column declared in
:mod:`dcc_console.catalog`, using the value captured before the test ran.
Table, column, key and cast come from the catalogue; the value and the row key
are bound as parameters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalog import TestDefinition
from .database import DatabaseConnection

logger = logging.getLogger(__name__)


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


def read_state(connection: DatabaseConnection, definition: TestDefinition, identifier: str):
    """Read the verified column for one row, or ``None`` if it cannot be read."""
    verify = definition.verify
    # Identifiers are catalogue constants; the row key is a bound parameter.
    sql = (
        f"SELECT TOP (1) CONVERT(nvarchar(max), {verify.column}) "  # noqa: S608
        f"FROM {verify.table} "
        f"WHERE CONVERT(nvarchar(50), {verify.key}) = ?"
    )
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
