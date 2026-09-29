"""Builders for the authoritative fix log (``APP_FIX_LOG``), its guards and the allowlist.

Every value is bound as a parameter. Column names are taken only from the
``_INSERTABLE`` allowlist below (which mirrors the table), never from caller keys
that are not recognised — an unknown key raises rather than reaching the SQL text.

The live fixer writes one row per terminal a procedure call covers, for every
attempt (pre-check, dry run, live apply, rollback), all threaded by
``CORRELATION_ID``. The fix log and ``FIX_OPERATORS`` live in the shared schema.
"""

from __future__ import annotations

from . import RemediationObjects

# Writable columns in canonical order. FIX_ID (autoincrement), APPLIED_AT and
# CAPTURED_AT (defaults) are intentionally omitted.
_INSERTABLE: tuple[str, ...] = (
    "CORRELATION_ID",
    "APPLIED_BY",
    "SQL_LOGIN",
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
    "NOTES",
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
    "APPLIED",  # live change committed (VERIFIED says whether it was confirmed)
    "SKIPPED_ALREADY_OK",  # live pre-check found the target already correct
    "NOT_FOUND",  # live pre-check could not find the target in this environment
    "SIMULATED",  # dry run completed without error
    "FAILED",  # the call (or a live verify) failed
    "ROLLED_BACK",  # a committed fix was undone
)


def build_insert(record: dict, objs: RemediationObjects) -> tuple[str, list]:
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
    sql = f"INSERT INTO {objs.fix_log_table} ({col_sql}) VALUES ({placeholders})"  # noqa: S608
    return sql, params


def build_insert_many(records: list[dict], objs: RemediationObjects) -> tuple[str, list]:
    """One multi-row ``INSERT`` for several fix-log records (one round trip).

    Every record is validated exactly as :func:`build_insert` does, and all must
    carry the same columns (they do when built by ``fixer.log_records``).
    """
    if not records:
        raise ValueError("No fix-log records to insert.")
    first_sql, first_params = build_insert(records[0], objs)
    columns = [c for c in _INSERTABLE if c in records[0]]
    params = list(first_params)
    for record in records[1:]:
        if [c for c in _INSERTABLE if c in record] != columns:
            raise ValueError("Fix-log records in one insert must share the same columns.")
        _, row_params = build_insert(record, objs)
        params.extend(row_params)
    row = "(" + ", ".join(["%s"] * len(columns)) + ")"
    values = ", ".join([row] * len(records))
    return first_sql.split(" VALUES ", 1)[0] + f" VALUES {values}", params


def build_last_live_outcome_query(objs: RemediationObjects) -> str:
    """The latest live APPLIED / ROLLED_BACK row for one terminal + check + environment.

    Caller binds ``[terminal_identifier, check_column, environment]``. The live
    're-fix' guard, independent of the daily snapshot: a verified fix newer than the
    snapshot's source load blocks a repeat, while a later rollback re-opens it.
    """
    return (
        f"SELECT OUTCOME, VERIFIED, APPLIED_AT\n"  # noqa: S608
        f"FROM {objs.fix_log_table}\n"
        f"WHERE TERMINAL_IDENTIFIER = %s AND CHECK_COLUMN = %s AND ENVIRONMENT = %s\n"
        f"  AND MODE = 'LIVE' AND OUTCOME IN ('APPLIED', 'ROLLED_BACK')\n"
        f"ORDER BY APPLIED_AT DESC, FIX_ID DESC\nLIMIT 1"
    )


_HISTORY_COLUMNS = (
    "APPLIED_AT",
    "APPLIED_BY",
    "SQL_LOGIN",
    "ENVIRONMENT",
    "MODE",
    "CHECK_COLUMN",
    "FIX_BIT",
    "TARGET_ID_KIND",
    "TARGET_IDENTIFIER",
    "VALUE_SENT",
    "OUTCOME",
    "VERIFIED",
    "ERROR",
    "NOTES",
    "CORRELATION_ID",
)


def build_terminal_history_query(objs: RemediationObjects, limit: int = 50) -> str:
    """Recent fix-log rows for one terminal; the caller binds ``[terminal_identifier]``."""
    capped = max(1, min(int(limit), 500))
    return (
        f"SELECT {', '.join(_HISTORY_COLUMNS)}\n"  # noqa: S608
        f"FROM {objs.fix_log_table}\n"
        f"WHERE TERMINAL_IDENTIFIER = %s\n"
        f"ORDER BY APPLIED_AT DESC\nLIMIT {capped}"
    )


def build_operator_check_query(objs: RemediationObjects) -> str:
    """The signed-in Snowflake user and whether they are an active live-fix operator.

    Identity is the SSO session's ``CURRENT_USER()`` — nothing the client supplies.
    Returns one row: ``USER_NAME``, ``IS_OPERATOR`` (0/1).
    """
    return (
        "SELECT CURRENT_USER() AS USER_NAME,\n"  # noqa: S608
        f"    (SELECT COUNT(*) FROM {objs.operators_table}\n"
        "     WHERE UPPER(USER_NAME) = UPPER(CURRENT_USER()) AND ACTIVE) AS IS_OPERATOR"
    )
