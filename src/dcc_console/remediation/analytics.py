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


def build_fix_activity_query(objs: RemediationObjects, days: int = 14) -> str:
    """Daily applied fixes, already-OK checks, and simulations by environment.

    Reads the shared ``APP_FIX_LOG`` so the Overview page can show recent remediation
    throughput, not just the standing backlog. Collapses per-terminal log rows into
    procedure-call events by correlation ID, timestamp, mode, environment, and outcome.
    ``days`` is clamped and inlined as an integer literal (never user text); the fix-log
    name is a validated identifier.
    """
    window = max(1, min(int(days), 180))
    return f"""WITH events AS (
    SELECT DISTINCT CAST(APPLIED_AT AS DATE) AS FIX_DATE,
           CORRELATION_ID, APPLIED_AT, MODE, ENVIRONMENT, OUTCOME
    FROM {objs.fix_log_table}
    WHERE APPLIED_AT >= DATEADD('day', -{window}, CURRENT_TIMESTAMP())
)
SELECT FIX_DATE,
    COUNT_IF(ENVIRONMENT = 'PROD' AND MODE = 'LIVE' AND OUTCOME = 'APPLIED') AS LIVE_APPLIED_PROD,
    COUNT_IF(ENVIRONMENT = 'PROD' AND MODE = 'LIVE'
        AND OUTCOME = 'SKIPPED_ALREADY_OK') AS ALREADY_OK_PROD,
    COUNT_IF(ENVIRONMENT = 'PROD' AND MODE = 'SIMULATION') AS DRY_RUN_PROD,
    COUNT_IF(ENVIRONMENT = 'UAT' AND MODE = 'SIMULATION') AS DRY_RUN_UAT,
    COUNT_IF(ENVIRONMENT = 'DEV' AND MODE = 'SIMULATION') AS DRY_RUN_DEV,
    COUNT_IF(ENVIRONMENT IN ('UAT', 'DEV') AND MODE = 'LIVE'
        AND OUTCOME = 'APPLIED') AS LIVE_APPLIED_UAT_DEV
FROM events
GROUP BY FIX_DATE
ORDER BY FIX_DATE"""


def build_fix_coverage_query(objs: RemediationObjects) -> str:
    """Unique registered PROD targets grouped by target type and registry status."""
    return f"""SELECT
    CASE TARGET_ID_KIND
        WHEN 'TERMINAL_IDENTIFIER' THEN 'Terminal'
        WHEN 'INSTANCE_IDENTIFIER' THEN 'Instance'
        WHEN 'LOCATION_NO' THEN 'Location'
        ELSE TARGET_ID_KIND
    END AS TARGET_TYPE,
    COUNT(DISTINCT TARGET_IDENTIFIER) AS REGISTERED_TARGETS,
    COUNT(DISTINCT IFF(STATUS = 'CONFIRMED', TARGET_IDENTIFIER, NULL)) AS CONFIRMED_TARGETS,
    COUNT(DISTINCT TERMINAL_IDENTIFIER) AS TERMINALS_COVERED
FROM {objs.registry_table}
WHERE ENVIRONMENT = 'PROD' AND STATUS IN ('FIXED', 'CONFIRMED')
GROUP BY TARGET_ID_KIND
ORDER BY TARGET_TYPE"""


def build_bit_fix_coverage_query(objs: RemediationObjects) -> str:
    """PROD call and registry counts by supported remediation bit."""
    return f"""WITH bits AS (
    SELECT column1 AS FIX_BIT FROM VALUES (1), (2), (8), (16)
), log_events AS (
    SELECT DISTINCT FIX_BIT, CORRELATION_ID, APPLIED_AT, MODE, OUTCOME
    FROM {objs.fix_log_table}
    WHERE ENVIRONMENT = 'PROD' AND FIX_BIT IN (1, 2, 8, 16)
), log_counts AS (
    SELECT FIX_BIT,
        COUNT_IF(MODE = 'LIVE' AND OUTCOME = 'APPLIED') AS LIVE_APPLIED_CALLS,
        COUNT_IF(MODE = 'LIVE' AND OUTCOME = 'SKIPPED_ALREADY_OK') AS ALREADY_OK_CALLS,
        COUNT_IF(MODE = 'SIMULATION') AS DRY_RUN_ATTEMPTS
    FROM log_events
    GROUP BY FIX_BIT
), registry_counts AS (
    SELECT FIX_BIT,
        COUNT(*) AS REGISTERED_CHECKS,
        COUNT_IF(STATUS = 'FIXED') AS PENDING_CONFIRMATION_CHECKS,
        COUNT_IF(STATUS = 'CONFIRMED') AS CONFIRMED_CHECKS,
        COUNT(DISTINCT TERMINAL_IDENTIFIER) AS TERMINALS_COVERED
    FROM {objs.registry_table}
    WHERE ENVIRONMENT = 'PROD' AND STATUS IN ('FIXED', 'CONFIRMED')
      AND FIX_BIT IN (1, 2, 8, 16)
    GROUP BY FIX_BIT
)
SELECT bits.FIX_BIT,
    COALESCE(log_counts.LIVE_APPLIED_CALLS, 0) AS LIVE_APPLIED_CALLS,
    COALESCE(log_counts.ALREADY_OK_CALLS, 0) AS ALREADY_OK_CALLS,
    COALESCE(log_counts.DRY_RUN_ATTEMPTS, 0) AS DRY_RUN_ATTEMPTS,
    COALESCE(registry_counts.REGISTERED_CHECKS, 0) AS REGISTERED_CHECKS,
    COALESCE(registry_counts.PENDING_CONFIRMATION_CHECKS, 0) AS PENDING_CONFIRMATION_CHECKS,
    COALESCE(registry_counts.CONFIRMED_CHECKS, 0) AS CONFIRMED_CHECKS,
    COALESCE(registry_counts.TERMINALS_COVERED, 0) AS TERMINALS_COVERED
FROM bits
LEFT JOIN log_counts USING (FIX_BIT)
LEFT JOIN registry_counts USING (FIX_BIT)
ORDER BY bits.FIX_BIT"""
