"""Builders for the remediation worklist (the filtered list of broken terminals).

Reads ``DEV_CORE_AAB.DCC_REMEDIATION.V_CURRENT_BROKEN`` — the latest snapshot per
terminal with the stale-data guard already applied (``REMEDIATION_STATE`` is
``ACTIONABLE`` or ``AWAITING_REFRESH``).

Filter *values* are always bound as parameters. Filter *columns* are validated
against the allowlists in :mod:`.mapping` before they are placed in the SQL text,
so a caller can never inject a column or table name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import RemediationObjects
from .mapping import (
    DIMENSION_COLUMNS,
    FIXABLE_CHECK_COLUMNS,
    IDENTIFIER_COLUMNS,
    TRACKED_CHECK_COLUMNS,
)

VALID_STATES: tuple[str, ...] = ("ACTIONABLE", "AWAITING_REFRESH")
_MAX_LIMIT = 5000


@dataclass(frozen=True)
class WorklistFilters:
    """Selections coming from the UI. Empty tuple / None means 'no filter'."""

    remediation_state: str | None = "ACTIONABLE"
    dimensions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    identifiers: dict[str, str] = field(default_factory=dict)
    check_columns: tuple[str, ...] = ()   # keep terminals broken on ANY of these
    fixable_only: bool = False
    limit: int = 500


def _validate_check_columns(columns: tuple[str, ...], fixable_only: bool) -> list[str]:
    allowed = set(FIXABLE_CHECK_COLUMNS if fixable_only else TRACKED_CHECK_COLUMNS)
    invalid = [c for c in columns if c not in allowed]
    if invalid:
        raise ValueError(f"Unknown or non-fixable check column(s): {invalid}")
    # Preserve canonical order for a stable, testable query string.
    source = FIXABLE_CHECK_COLUMNS if fixable_only else TRACKED_CHECK_COLUMNS
    chosen = set(columns)
    return [c for c in source if c in chosen]


def _validate_dimension(column: str) -> str:
    if column not in DIMENSION_COLUMNS:
        raise ValueError(f"Unknown dimension column: {column!r}")
    return column


def build_worklist_query(filters: WorklistFilters, objs: RemediationObjects) -> tuple[str, list]:
    """Return ``(sql, params)`` selecting the filtered broken-terminal rows."""
    clauses: list[str] = []
    params: list = []

    state = filters.remediation_state
    if state is not None:
        if state not in VALID_STATES:
            raise ValueError(f"Invalid remediation_state: {state!r}")
        clauses.append("REMEDIATION_STATE = %s")
        params.append(state)

    if filters.fixable_only:
        # At least one fixable check must be broken (=1) on the row.
        fixable_or = " OR ".join(f"{c} = 1" for c in FIXABLE_CHECK_COLUMNS)  # noqa: S608
        clauses.append(f"({fixable_or})")

    for column, values in sorted(filters.dimensions.items()):
        clean = _validate_dimension(column)
        selected = tuple(v for v in values if v is not None)
        if not selected:
            continue
        placeholders = ", ".join(["%s"] * len(selected))
        clauses.append(f"{clean} IN ({placeholders})")
        params.extend(selected)

    for column, value in sorted(filters.identifiers.items()):
        if column not in IDENTIFIER_COLUMNS:
            raise ValueError(f"Unknown identifier column: {column!r}")
        clean = value.strip()
        if clean:
            clauses.append(f"{column} = %s")
            params.append(clean)

    chosen_checks = _validate_check_columns(filters.check_columns, filters.fixable_only)
    if chosen_checks:
        broken_or = " OR ".join(f"{c} = 1" for c in chosen_checks)  # noqa: S608
        clauses.append(f"({broken_or})")

    where = ("\nWHERE " + "\n  AND ".join(clauses)) if clauses else ""
    limit = max(1, min(int(filters.limit), _MAX_LIMIT))
    sql = (
        f"SELECT * FROM {objs.v_current_broken}{where}\n"
        f"ORDER BY TERMINAL_IDENTIFIER\n"
        f"LIMIT {limit}"
    )
    return sql, params


def build_distinct_values_query(
    column: str, objs: RemediationObjects, limit: int = 1000
) -> str:
    """Return SQL for the distinct non-null values of a dimension (for dropdowns)."""
    clean = _validate_dimension(column)
    capped = max(1, min(int(limit), _MAX_LIMIT))
    return (
        f"SELECT DISTINCT {clean} AS VALUE FROM {objs.v_current_broken}\n"  # noqa: S608
        f"WHERE {clean} IS NOT NULL\nORDER BY VALUE\nLIMIT {capped}"
    )


def build_terminal_detail_query(objs: RemediationObjects) -> tuple[str, list]:
    """Return ``(sql, params)`` for a single terminal's current broken row.

    The parameter is bound by the caller: ``params=[terminal_identifier]``.
    """
    sql = f"SELECT * FROM {objs.v_current_broken} WHERE TERMINAL_IDENTIFIER = %s"  # noqa: S608
    return sql, []


def build_shared_target_query(
    target_column: str, check_column: str, objs: RemediationObjects, limit: int = 500
) -> str:
    """Listed terminals one fix covers: same instance/location/terminal, broken on the check.

    A Bit 8/16 call changes a whole instance and a Bit 1 call a whole location, so one
    call can repair several listed terminals. The caller binds ``[target_identifier]``;
    both column names are validated against the mapping allowlists first.
    """
    if target_column not in IDENTIFIER_COLUMNS:
        raise ValueError(f"Unknown identifier column: {target_column!r}")
    if check_column not in TRACKED_CHECK_COLUMNS:
        raise ValueError(f"Unknown check column: {check_column!r}")
    capped = max(1, min(int(limit), _MAX_LIMIT))
    return (
        f"SELECT * FROM {objs.v_current_broken}\n"  # noqa: S608
        f"WHERE {target_column} = %s AND {check_column} = 1\n"
        f"ORDER BY TERMINAL_IDENTIFIER\nLIMIT {capped}"
    )
