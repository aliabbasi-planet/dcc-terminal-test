"""Builders for the remediation analytics panel.

All queries read the Snowflake views/tables in :mod:`dcc_console.remediation`.
Group-by / measure columns are validated against :mod:`.mapping` allowlists
before being placed in SQL text; there are no bound *values* here because these
are fixed rollups, not user-filtered reads.
"""

from __future__ import annotations

from . import RemediationObjects
from .mapping import DIMENSION_COLUMNS, TRACKED_CHECK_COLUMNS

_MAX_TOP_N = 200


def build_kpis_query(objs: RemediationObjects) -> str:
    """Headline KPIs (actionable / awaiting-refresh / fixes / latest snapshot)."""
    return f"SELECT * FROM {objs.v_remediation_kpis}"


def build_state_totals_query(objs: RemediationObjects) -> str:
    """Row count per remediation state."""
    return (
        f"SELECT REMEDIATION_STATE, COUNT(*) AS TERMINALS\n"
        f"FROM {objs.v_current_broken}\nGROUP BY REMEDIATION_STATE\nORDER BY TERMINALS DESC"
    )


def build_breakdown_query(dimension: str, objs: RemediationObjects, top_n: int = 20) -> str:
    """Top-N actionable broken-terminal counts grouped by one dimension."""
    if dimension not in DIMENSION_COLUMNS:
        raise ValueError(f"Unknown dimension column: {dimension!r}")
    capped = max(1, min(int(top_n), _MAX_TOP_N))
    return (
        f"SELECT {dimension} AS CATEGORY, COUNT(*) AS TERMINALS\n"  # noqa: S608
        f"FROM {objs.v_current_broken}\n"
        f"WHERE REMEDIATION_STATE = 'ACTIONABLE' AND {dimension} IS NOT NULL\n"
        f"GROUP BY {dimension}\nORDER BY TERMINALS DESC\nLIMIT {capped}"
    )


def build_flag_totals_query(objs: RemediationObjects) -> str:
    """One row: count of actionable terminals broken on each tracked check."""
    sums = ",\n    ".join(f"SUM({c}) AS {c}" for c in TRACKED_CHECK_COLUMNS)  # noqa: S608
    return (
        f"SELECT\n    {sums}\n"
        f"FROM {objs.v_current_broken}\nWHERE REMEDIATION_STATE = 'ACTIONABLE'"
    )
