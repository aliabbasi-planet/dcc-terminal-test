"""Builders for the durable fix registry (``DCC_FIX_REGISTRY``).

The registry is the authoritative record of *currently-fixed* production checks. It
is what makes a verified live fix survive the daily snapshot refresh: the worklist
view (:mod:`.ddl`) reads its PROD rows, so a terminal we fixed is not re-flagged the
next morning even if ``PROD_PRESENTATION`` has not caught up yet. One row per
``(ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN)``:

* **inserted / upserted** when a live fix is verified by a fresh SQL-Server read (or a
  live pre-check proves the check already satisfied) — see :func:`build_upsert`;
* **deleted** when a rollback is confirmed reverted by a fresh read — see
  :func:`build_delete_many`; the audit trail stays in the immutable ``APP_FIX_LOG``.

``FIXED_AT`` / ``FIXED_AT_UTC`` are set server-side (never from the client clock), so
the freshness guard in the view compares like-for-like with ``SOURCE_LAST_ALTERED``.
Every value is bound as a parameter; column names come only from the ``_INSERTABLE``
allowlist below, never from caller keys.
"""

from __future__ import annotations

from . import RemediationObjects

# Every statement here is built from validated identifiers / declared constants and binds
# all values as parameters (see the module docstring), so the S608 "SQL injection" lint —
# which fires on the interpolated table names in the MERGE/backfill — is a false positive.
# ruff: noqa: S608

# Columns the client supplies, in canonical order. FIXED_AT / FIXED_AT_UTC / STATUS are
# set by the SQL itself (server clock), not bound here.
_INSERTABLE: tuple[str, ...] = (
    "ENVIRONMENT",
    "TERMINAL_IDENTIFIER",
    "CHECK_COLUMN",
    "INSTANCE_IDENTIFIER",
    "LOCATION_NO",
    "FIX_BIT",
    "FLAG_NAME",
    "VALUE_SENT",
    "TARGET_ID_KIND",
    "TARGET_IDENTIFIER",
    "CORRELATION_ID",
    "CHANGE_REF",
    "RESOLUTION",
    "SP_ROLLBACK_SCRIPT",
    "FIXED_BY",
)
_REQUIRED: tuple[str, ...] = (
    "ENVIRONMENT",
    "TERMINAL_IDENTIFIER",
    "CHECK_COLUMN",
    "RESOLUTION",
)
# Cast each column in the USING clause so an all-NULL column (e.g. CHANGE_REF) never
# leaves Snowflake unable to infer a type for the MERGE source.
_CAST: dict[str, str] = {"FIX_BIT": "::NUMBER"}
_KEY: tuple[str, ...] = ("ENVIRONMENT", "TERMINAL_IDENTIFIER", "CHECK_COLUMN")

# A resolution is either a committed live fix (can be rolled back) or a live pre-check
# that proved the check already satisfied in production (nothing to undo).
VALID_RESOLUTIONS: tuple[str, ...] = ("APPLIED", "ALREADY_OK")

# A registered fix is FIXED until the Cortex source confirms it (then CONFIRMED, kept for
# history — see :mod:`.reconcile`). A rollback deletes the row rather than flipping status,
# so the worklist re-opens the check immediately.
STATUS_FIXED = "FIXED"
STATUS_CONFIRMED = "CONFIRMED"
_STATUS = STATUS_FIXED
_FIXED_AT = "CURRENT_TIMESTAMP()"
_FIXED_AT_UTC = "CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ"


def create_table(objs: RemediationObjects) -> str:
    """Idempotent ``CREATE TABLE`` for the registry (shared schema)."""
    return f"""CREATE TABLE IF NOT EXISTS {objs.registry_table} (
    ENVIRONMENT          VARCHAR(20)  NOT NULL,
    TERMINAL_IDENTIFIER  VARCHAR(100) NOT NULL,
    CHECK_COLUMN         VARCHAR(80)  NOT NULL,
    INSTANCE_IDENTIFIER  VARCHAR(100),
    LOCATION_NO          VARCHAR(50),
    FIX_BIT              NUMBER(4,0),
    FLAG_NAME            VARCHAR(120),
    VALUE_SENT           VARCHAR(120),
    TARGET_ID_KIND       VARCHAR(30),
    TARGET_IDENTIFIER    VARCHAR(100),
    CORRELATION_ID       VARCHAR(64),
    CHANGE_REF           VARCHAR(120),
    RESOLUTION           VARCHAR(20)  NOT NULL,
    STATUS               VARCHAR(20)  NOT NULL DEFAULT '{_STATUS}',
    SP_ROLLBACK_SCRIPT   VARCHAR,
    FIXED_BY             VARCHAR(150),
    FIXED_AT             TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    FIXED_AT_UTC         TIMESTAMP_NTZ NOT NULL,
    CONFIRMED_AT_UTC     TIMESTAMP_NTZ,
    PRIMARY KEY (ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN)
)"""


def _validate(record: dict) -> None:
    unknown = [k for k in record if k not in _INSERTABLE]
    if unknown:
        raise ValueError(f"Unknown registry column(s): {unknown}")
    missing = [k for k in _REQUIRED if record.get(k) in (None, "")]
    if missing:
        raise ValueError(f"Missing required registry column(s): {missing}")
    if record["RESOLUTION"] not in VALID_RESOLUTIONS:
        raise ValueError(f"Invalid RESOLUTION: {record['RESOLUTION']!r}")


def build_upsert(records: list[dict], objs: RemediationObjects) -> tuple[str, list]:
    """One ``MERGE`` upserting the registry from ``records`` (one per covered terminal).

    Matched rows are refreshed (a re-apply updates the details and the fix time);
    unmatched rows are inserted. ``STATUS`` and the fix timestamps are server-set.
    """
    if not records:
        raise ValueError("No registry records to upsert.")
    for record in records:
        _validate(record)

    def cell(column: str) -> str:
        return f"%s{_CAST.get(column, '')}"

    row_sql = "SELECT " + ", ".join(f"{cell(c)} AS {c}" for c in _INSERTABLE)
    using = "\n    UNION ALL ".join(row_sql for _ in records)
    params: list = []
    for record in records:
        params.extend(record.get(c) for c in _INSERTABLE)

    on = " AND ".join(f"tgt.{k} = src.{k}" for k in _KEY)
    update_cols = [c for c in _INSERTABLE if c not in _KEY]
    update_sql = ",\n    ".join(f"{c} = src.{c}" for c in update_cols)
    insert_cols = list(_INSERTABLE) + ["STATUS", "FIXED_AT", "FIXED_AT_UTC"]
    insert_vals = [f"src.{c}" for c in _INSERTABLE] + [f"'{_STATUS}'", _FIXED_AT, _FIXED_AT_UTC]
    sql = f"""MERGE INTO {objs.registry_table} tgt
USING (
    {using}
) src
ON {on}
WHEN MATCHED THEN UPDATE SET
    {update_sql},
    STATUS = '{_STATUS}',
    FIXED_AT = {_FIXED_AT},
    FIXED_AT_UTC = {_FIXED_AT_UTC}
WHEN NOT MATCHED THEN INSERT ({", ".join(insert_cols)})
VALUES ({", ".join(insert_vals)})"""
    return sql, params


def build_delete_many(
    environment: str, terminals: list[str], check_column: str, objs: RemediationObjects
) -> tuple[str, list]:
    """Delete the registry rows for one check on several terminals (a confirmed rollback).

    The audit trail is untouched — ``APP_FIX_LOG`` keeps the ROLLED_BACK row; removing
    the registry row is what re-opens the check on the worklist.
    """
    unique = [t for t in dict.fromkeys(terminals) if t]
    if not unique:
        raise ValueError("No terminals to delete from the registry.")
    placeholders = ", ".join(["%s"] * len(unique))
    sql = (
        f"DELETE FROM {objs.registry_table}\n"  # noqa: S608
        f"WHERE ENVIRONMENT = %s AND CHECK_COLUMN = %s "
        f"AND TERMINAL_IDENTIFIER IN ({placeholders})"
    )
    return sql, [environment, check_column, *unique]


def build_backfill_from_log(objs: RemediationObjects) -> str:
    """Seed the registry from existing ``APP_FIX_LOG`` history (idempotent, no clobber).

    Reconstructs the latest verified PROD resolution per (terminal, check) — exactly the
    set the worklist used to derive from the log — so upgrading a schema that already has
    fix history keeps those terminals suppressed. Only inserts rows that are missing.
    """
    return f"""MERGE INTO {objs.registry_table} tgt
USING (
    WITH latest AS (
        SELECT TERMINAL_IDENTIFIER, CHECK_COLUMN, INSTANCE_IDENTIFIER, LOCATION_NO,
               FIX_BIT, FLAG_NAME, VALUE_SENT, TARGET_ID_KIND, TARGET_IDENTIFIER,
               CORRELATION_ID, APPLIED_BY, ROLLBACK_SCRIPT, OUTCOME, VERIFIED, APPLIED_AT
        FROM {objs.fix_log_table}
        WHERE ENVIRONMENT = 'PROD' AND MODE = 'LIVE' AND CHECK_COLUMN IS NOT NULL
          AND OUTCOME IN ('APPLIED', 'SKIPPED_ALREADY_OK', 'ROLLED_BACK')
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY TERMINAL_IDENTIFIER, CHECK_COLUMN ORDER BY APPLIED_AT DESC, FIX_ID DESC
        ) = 1
    )
    SELECT 'PROD' AS ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN, INSTANCE_IDENTIFIER,
           LOCATION_NO, FIX_BIT, FLAG_NAME, VALUE_SENT, TARGET_ID_KIND, TARGET_IDENTIFIER,
           CORRELATION_ID, NULL AS CHANGE_REF,
           IFF(OUTCOME = 'APPLIED', 'APPLIED', 'ALREADY_OK') AS RESOLUTION,
           ROLLBACK_SCRIPT AS SP_ROLLBACK_SCRIPT, APPLIED_BY AS FIXED_BY,
           CONVERT_TIMEZONE('UTC', APPLIED_AT)::TIMESTAMP_NTZ AS FIXED_AT_UTC
    FROM latest
    WHERE OUTCOME IN ('APPLIED', 'SKIPPED_ALREADY_OK') AND VERIFIED = TRUE
) src
ON tgt.ENVIRONMENT = src.ENVIRONMENT
   AND tgt.TERMINAL_IDENTIFIER = src.TERMINAL_IDENTIFIER
   AND tgt.CHECK_COLUMN = src.CHECK_COLUMN
WHEN NOT MATCHED THEN INSERT (
    ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN, INSTANCE_IDENTIFIER, LOCATION_NO,
    FIX_BIT, FLAG_NAME, VALUE_SENT, TARGET_ID_KIND, TARGET_IDENTIFIER, CORRELATION_ID,
    CHANGE_REF, RESOLUTION, SP_ROLLBACK_SCRIPT, FIXED_BY, STATUS, FIXED_AT, FIXED_AT_UTC
) VALUES (
    src.ENVIRONMENT, src.TERMINAL_IDENTIFIER, src.CHECK_COLUMN, src.INSTANCE_IDENTIFIER,
    src.LOCATION_NO, src.FIX_BIT, src.FLAG_NAME, src.VALUE_SENT, src.TARGET_ID_KIND,
    src.TARGET_IDENTIFIER, src.CORRELATION_ID, src.CHANGE_REF, src.RESOLUTION,
    src.SP_ROLLBACK_SCRIPT, src.FIXED_BY, '{_STATUS}', {_FIXED_AT}, src.FIXED_AT_UTC
)"""


def build_registered_terminals_query(objs: RemediationObjects) -> str:
    """Rows currently registered as fixed in PROD (for a KPI / audit read)."""
    return (
        f"SELECT COUNT(*) AS REGISTERED_CHECKS,\n"  # noqa: S608
        f"       COUNT(DISTINCT TERMINAL_IDENTIFIER) AS REGISTERED_TERMINALS\n"
        f"FROM {objs.registry_table}\n"
        f"WHERE ENVIRONMENT = 'PROD' AND STATUS IN ('{STATUS_FIXED}', '{STATUS_CONFIRMED}')"
    )
