"""Builder for the daily broken-terminal snapshot refresh.

Generates the ``MERGE`` that reads ``PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE``
and upserts one broken-only row per terminal into
``DEV_CORE_AAB.DCC_REMEDIATION.HEALTH_DAILY_SNAPSHOT``.

The maintenance table is finer than terminal grain (~1.7 rows/terminal), so the
source is grouped to terminal and each per-check broken flag is the ``MAX`` over
the group: broken if *any* row for that terminal is broken. "Broken on a check"
means in-scope and failing-or-unknown: ``BASE = 1 AND COALESCE(CHECK, 1) = 1``.

All column and table names come from :mod:`.mapping` constants, never user input.
"""

from __future__ import annotations

from . import MAINTENANCE_SOURCE, SNAPSHOT_TABLE
from .mapping import FLAG_FIXES, TRACKED_CHECK_COLUMNS

# Non-check columns carried into the snapshot, in table order. Grouped to terminal
# grain with MAX (these are functionally determined per terminal in the source).
_CARRIED_COLUMNS: tuple[str, ...] = (
    "INSTANCE_IDENTIFIER",
    "LOCATION_NO",
    "COUNTRY_NAME",
    "REGION",
    "INDUSTRY_NAME",
    "BANK_MERCHANT_ID",
    "MERCHANT_NAME",
    "CUSTOMER_NAME",
    "ACQUIRER_NAME",
    "LOCATION_NAME",
    "TERMINAL_BRAND_NAME",
    "TERMINAL_MODEL_NAME",
    "FIRMWARE_VERSION",
)

_SOURCE_LAST_ALTERED = (
    "(SELECT LAST_ALTERED::TIMESTAMP_NTZ "
    "FROM PROD_PRESENTATION.INFORMATION_SCHEMA.TABLES "
    "WHERE TABLE_SCHEMA='CORTEX' AND TABLE_NAME='CORTEX_TERMINAL_MAINTENANCE')"
)


def _broken_expr(check_column: str, base_column: str) -> str:
    """SQL that is true when a terminal is broken on one check (in scope + failing)."""
    return f"{base_column} = 1 AND COALESCE({check_column}, 1) = 1"


def build_refresh_merge() -> str:
    """Return the idempotent upsert that refreshes today's snapshot.

    Safe to run repeatedly on the same day: matched terminals are updated, new
    ones inserted. ``SNAPSHOT_DATE`` is ``CURRENT_DATE()`` in the query.
    """
    max_carried = ",\n        ".join(f"MAX({c}) AS {c}" for c in _CARRIED_COLUMNS)
    max_checks = ",\n        ".join(
        f"MAX(IFF({_broken_expr(f.check_column, f.base_column)}, 1, 0)) AS {f.check_column}"
        for f in FLAG_FIXES
    )
    where_broken = "\n           OR ".join(
        f"({_broken_expr(f.check_column, f.base_column)})" for f in FLAG_FIXES
    )

    insert_cols = (
        ["SNAPSHOT_DATE", "TERMINAL_IDENTIFIER"]
        + list(_CARRIED_COLUMNS)
        + list(TRACKED_CHECK_COLUMNS)
        + ["IS_DCC_BROKEN", "SOURCE_LAST_ALTERED"]
    )
    insert_col_sql = ", ".join(insert_cols)
    insert_val_sql = ", ".join(
        "TRUE"
        if c == "IS_DCC_BROKEN"
        else "src.SOURCE_LAST_ALTERED"
        if c == "SOURCE_LAST_ALTERED"
        else f"src.{c}"
        for c in insert_cols
    )

    update_targets = list(_CARRIED_COLUMNS) + list(TRACKED_CHECK_COLUMNS)
    update_sql = ",\n    ".join(f"{c} = src.{c}" for c in update_targets)

    return f"""MERGE INTO {SNAPSHOT_TABLE} tgt
USING (
    SELECT
        CURRENT_DATE() AS SNAPSHOT_DATE,
        TERMINAL_IDENTIFIER,
        {max_carried},
        {max_checks},
        {_SOURCE_LAST_ALTERED} AS SOURCE_LAST_ALTERED
    FROM {MAINTENANCE_SOURCE}
    WHERE TERMINAL_IDENTIFIER IS NOT NULL
      AND (
           {where_broken}
      )
    GROUP BY TERMINAL_IDENTIFIER
) src
ON tgt.SNAPSHOT_DATE = src.SNAPSHOT_DATE AND tgt.TERMINAL_IDENTIFIER = src.TERMINAL_IDENTIFIER
WHEN MATCHED THEN UPDATE SET
    {update_sql},
    IS_DCC_BROKEN = TRUE,
    SOURCE_LAST_ALTERED = src.SOURCE_LAST_ALTERED,
    CAPTURED_AT = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT ({insert_col_sql})
VALUES ({insert_val_sql})"""
