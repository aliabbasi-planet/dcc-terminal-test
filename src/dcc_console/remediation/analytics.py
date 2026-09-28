"""Builders for the remediation analytics panel.

All queries read the Snowflake views/tables in :mod:`dcc_console.remediation`.
Group-by / measure columns are validated against :mod:`.mapping` allowlists
before being placed in SQL text; there are no bound *values* here because these
are fixed rollups, not user-filtered reads.
"""

from __future__ import annotations

from . import V_CURRENT_BROKEN, V_REMEDIATION_KPIS
from .mapping import DIMENSION_COLUMNS, TRACKED_CHECK_COLUMNS

_MAX_TOP_N = 200


def build_kpis_query() -> str:
    """Headline KPIs (actionable / awaiting-refresh / fixes / latest snapshot)."""
    return f"SELECT * FROM {V_REMEDIATION_KPIS}"


def build_state_totals_query() -> str:
    """Row count per remediation state."""
    return (
        f"SELECT REMEDIATION_STATE, COUNT(*) AS TERMINALS\n"
        f"FROM {V_CURRENT_BROKEN}\nGROUP BY REMEDIATION_STATE\nORDER BY TERMINALS DESC"
    )


def build_breakdown_query(dimension: str, top_n: int = 20) -> str:
    """Top-N actionable broken-terminal counts grouped by one dimension."""
    if dimension not in DIMENSION_COLUMNS:
        raise ValueError(f"Unknown dimension column: {dimension!r}")
    capped = max(1, min(int(top_n), _MAX_TOP_N))
    return (
        f"SELECT {dimension} AS CATEGORY, COUNT(*) AS TERMINALS\n"  # noqa: S608
        f"FROM {V_CURRENT_BROKEN}\n"
        f"WHERE REMEDIATION_STATE = 'ACTIONABLE' AND {dimension} IS NOT NULL\n"
        f"GROUP BY {dimension}\nORDER BY TERMINALS DESC\nLIMIT {capped}"
    )


def build_flag_totals_query() -> str:
    """One row: count of actionable terminals broken on each tracked check."""
    sums = ",\n    ".join(f"SUM({c}) AS {c}" for c in TRACKED_CHECK_COLUMNS)  # noqa: S608
    return (
        f"SELECT\n    {sums}\n"
        f"FROM {V_CURRENT_BROKEN}\nWHERE REMEDIATION_STATE = 'ACTIONABLE'"
    )
