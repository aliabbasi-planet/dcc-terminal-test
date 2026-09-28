"""Builders for the authoritative fix log (``APP_FIX_LOG``) and the re-fix guard.

Every value is bound as a parameter. Column names are taken only from the
``_INSERTABLE`` allowlist below (which mirrors the table), never from caller keys
that are not recognised — an unknown key raises rather than reaching the SQL text.

The live fixer (later phase) writes one row per attempt: pre-check → apply →
verify → log, all threaded by ``CORRELATION_ID``.
"""

from __future__ import annotations

from . import FIX_LOG_TABLE

# Writable columns in canonical order. FIX_ID (autoincrement), APPLIED_AT and
# CAPTURED_AT (defaults) are intentionally omitted.
_INSERTABLE: tuple[str, ...] = (
    "CORRELATION_ID",
    "APPLIED_BY",
    "MODE",
    "ENVIRONMENT",
    "SERVER",
    "DATABASE_NAME",
    "FIX_BIT",
    "CHECK_COLUMN",
    "FLAG_NAME",
    "TARGET_ID_KIND",
    "TARGET_IDENTIFIER",
    "INSTANCE_IDENTIFIER",
    "TERMINAL_IDENTIFIER",
    "LOCATION_NO",
    "VALUE_SENT",
    "STATE_BEFORE",
    "STATE_AFTER",
    "VERIFIED",
    "ROLLBACK_SCRIPT",
    "OUTCOME",
    "ERROR",
    "BANK_MERCHANT_ID",
    "MERCHANT_NAME",
    "CUSTOMER_NAME",
    "COUNTRY_NAME",
    "REGION",
    "INDUSTRY_NAME",
    "ACQUIRER_NAME",
    "TERMINAL_BRAND_NAME",
    "TERMINAL_MODEL_NAME",
)

_REQUIRED: tuple[str, ...] = (
    "CORRELATION_ID",
    "APPLIED_BY",
    "MODE",
    "ENVIRONMENT",
    "SERVER",
    "DATABASE_NAME",
    "FIX_BIT",
    "TARGET_ID_KIND",
    "TARGET_IDENTIFIER",
    "OUTCOME",
)

VALID_MODES: tuple[str, ...] = ("LIVE", "SIMULATION")
VALID_OUTCOMES: tuple[str, ...] = (
    "APPLIED",
    "SKIPPED_ALREADY_OK",
    "FAILED",
    "ROLLED_BACK",
)


def build_insert(record: dict) -> tuple[str, list]:
    """Return ``(sql, params)`` inserting one fix-log row from ``record``.

    Raises ``ValueError`` on unknown keys, missing required columns, or an
    invalid ``MODE`` / ``OUTCOME`` (fail closed rather than write a bad audit row).
    """
    unknown = [k for k in record if k not in _INSERTABLE]
    if unknown:
        raise ValueError(f"Unknown fix-log column(s): {unknown}")
    missing = [k for k in _REQUIRED if record.get(k) in (None, "")]
    if missing:
        raise ValueError(f"Missing required fix-log column(s): {missing}")
    if record["MODE"] not in VALID_MODES:
        raise ValueError(f"Invalid MODE: {record['MODE']!r}")
    if record["OUTCOME"] not in VALID_OUTCOMES:
        raise ValueError(f"Invalid OUTCOME: {record['OUTCOME']!r}")

    columns = [c for c in _INSERTABLE if c in record]
    placeholders = ", ".join(["%s"] * len(columns))
    col_sql = ", ".join(columns)
    params = [record[c] for c in columns]
    sql = f"INSERT INTO {FIX_LOG_TABLE} ({col_sql}) VALUES ({placeholders})"  # noqa: S608
    return sql, params


def build_recent_fix_query() -> tuple[str, list]:
    """Return ``(sql, params)`` for the last verified LIVE fix of a terminal+check.

    Caller binds ``params=[terminal_identifier, check_column]``. Used as the live
    'have we already fixed this?' guard, independent of the daily snapshot.
    """
    sql = (
        f"SELECT MAX(APPLIED_AT) AS LAST_FIX_AT\n"  # noqa: S608
        f"FROM {FIX_LOG_TABLE}\n"
        f"WHERE TERMINAL_IDENTIFIER = %s AND CHECK_COLUMN = %s\n"
        f"  AND OUTCOME = 'APPLIED' AND VERIFIED = TRUE AND MODE = 'LIVE'"
    )
    return sql, []
