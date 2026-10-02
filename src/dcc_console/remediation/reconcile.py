"""Builders for daily fix confirmation and the profit-tracking handoff.

The operational app records a verified live fix in ``DCC_FIX_REGISTRY`` (``STATUS='FIXED'``)
immediately, before the source has caught up. This module adds the *next* step: once the
Cortex maintenance table reflects the fix as healthy, the fix is **confirmed** and copied
into a durable handoff table (``DCC_CONFIRMED_FIXES``) that the separate financial /
profit-tracking app consumes. The registry row is kept but flipped to ``STATUS='CONFIRMED'``
(the immutable audit trail stays in ``APP_FIX_LOG``).

A registered PROD fix is *confirmed* when both hold, read straight from the shared source
(:data:`MAINTENANCE_SOURCE`), the same table the daily snapshot is built from:

* the source has reloaded since we fixed it — table ``LAST_ALTERED`` (UTC) is newer than the
  registry row's ``FIXED_AT_UTC`` (so an un-refreshed source never counts as confirmation);
* that specific ``CHECK_COLUMN`` is no longer broken for the terminal — the same
  ``BASE = 1 AND COALESCE(CHECK, 1) = 1`` rule the snapshot uses, aggregated to terminal grain.

Every column / table name comes from :mod:`.mapping` constants or validated identifiers; no
value is interpolated, so the generated SQL is injection-proof (the S608 lint fires only on
the interpolated *table names*, which are safe — see :mod:`.registry`).
"""

from __future__ import annotations

from . import MAINTENANCE_SOURCE, RemediationObjects, registry
from .mapping import FLAG_FIXES
from .snapshot import _SOURCE_LAST_ALTERED, _broken_expr

# ruff: noqa: S608  # table names are validated identifiers; no values are interpolated.

PROD = "PROD"

# Business dimensions copied from the source at confirmation time (terminal grain, MAX per
# terminal — functionally determined, mirrors the snapshot's carried columns). Instance /
# location come from the registry row, so they are not repeated here.
_DIMENSION_COLUMNS: tuple[str, ...] = (
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
# DDL for the dimension block, in the same order (mirrors the snapshot column types).
_DIMENSION_DDL: tuple[tuple[str, str], ...] = (
    ("COUNTRY_NAME", "VARCHAR(100)"),
    ("REGION", "VARCHAR(100)"),
    ("INDUSTRY_NAME", "VARCHAR(100)"),
    ("BANK_MERCHANT_ID", "VARCHAR(50)"),
    ("MERCHANT_NAME", "VARCHAR(250)"),
    ("CUSTOMER_NAME", "VARCHAR(250)"),
    ("ACQUIRER_NAME", "VARCHAR(250)"),
    ("LOCATION_NAME", "VARCHAR(500)"),
    ("TERMINAL_BRAND_NAME", "VARCHAR(100)"),
    ("TERMINAL_MODEL_NAME", "VARCHAR(200)"),
    ("FIRMWARE_VERSION", "VARCHAR(200)"),
)

# Fix metadata carried from the registry row (in handoff column order).
_FIX_COLUMNS: tuple[str, ...] = (
    "ENVIRONMENT",
    "TERMINAL_IDENTIFIER",
    "CHECK_COLUMN",
    "FLAG_NAME",
    "FIX_BIT",
    "VALUE_SENT",
    "TARGET_ID_KIND",
    "TARGET_IDENTIFIER",
    "INSTANCE_IDENTIFIER",
    "LOCATION_NO",
    "CORRELATION_ID",
    "CHANGE_REF",
    "RESOLUTION",
    "FIXED_BY",
    "FIXED_AT_UTC",
)
_CONFIRM_COLUMNS: tuple[str, ...] = ("SRC_LOADED_UTC", "CONFIRMED_AT_UTC")
# Full ordered column list of the handoff table.
HANDOFF_COLUMNS: tuple[str, ...] = _FIX_COLUMNS + _CONFIRM_COLUMNS + _DIMENSION_COLUMNS
# The handoff is keyed on the fix event, so re-running the reconcile never duplicates a row.
_KEY: tuple[str, ...] = ("ENVIRONMENT", "TERMINAL_IDENTIFIER", "CHECK_COLUMN", "FIXED_AT_UTC")

_CONFIRMED_AT_UTC = "CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ"


def create_confirmed_table(objs: RemediationObjects) -> str:
    """Idempotent ``CREATE TABLE`` for the profit-tracking handoff (shared schema)."""
    dims = ",\n    ".join(f"{name:<20} {ddl}" for name, ddl in _DIMENSION_DDL)
    return f"""CREATE TABLE IF NOT EXISTS {objs.confirmed_table} (
    ENVIRONMENT          VARCHAR(20)  NOT NULL,
    TERMINAL_IDENTIFIER  VARCHAR(100) NOT NULL,
    CHECK_COLUMN         VARCHAR(80)  NOT NULL,
    FLAG_NAME            VARCHAR(120),
    FIX_BIT              NUMBER(4,0),
    VALUE_SENT           VARCHAR(120),
    TARGET_ID_KIND       VARCHAR(30),
    TARGET_IDENTIFIER    VARCHAR(100),
    INSTANCE_IDENTIFIER  VARCHAR(100),
    LOCATION_NO          VARCHAR(50),
    CORRELATION_ID       VARCHAR(64),
    CHANGE_REF           VARCHAR(120),
    RESOLUTION           VARCHAR(20),
    FIXED_BY             VARCHAR(150),
    FIXED_AT_UTC         TIMESTAMP_NTZ NOT NULL,
    SRC_LOADED_UTC       TIMESTAMP_NTZ,
    CONFIRMED_AT_UTC     TIMESTAMP_NTZ NOT NULL DEFAULT {_CONFIRMED_AT_UTC},
    {dims},
    PRIMARY KEY (ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN, FIXED_AT_UTC)
)"""


def _cortex_scan(objs: RemediationObjects) -> str:
    """CTE: current per-terminal Cortex health for every registry (PROD/FIXED) terminal."""
    dims = ",\n           ".join(f"MAX({c}) AS {c}" for c in _DIMENSION_COLUMNS)
    checks = ",\n           ".join(
        f"MAX(IFF({_broken_expr(f.check_column, f.base_column)}, 1, 0)) AS {f.check_column}"
        for f in FLAG_FIXES
    )
    return f"""cortex AS (
        SELECT TERMINAL_IDENTIFIER,
           {dims},
           {checks},
           {_SOURCE_LAST_ALTERED} AS SRC_LOADED_UTC
        FROM {MAINTENANCE_SOURCE}
        WHERE TERMINAL_IDENTIFIER IN (
            SELECT TERMINAL_IDENTIFIER FROM {objs.registry_table}
            WHERE ENVIRONMENT = '{PROD}' AND STATUS = '{registry.STATUS_FIXED}'
        )
        GROUP BY TERMINAL_IDENTIFIER
    )"""


def _still_broken_case() -> str:
    """CASE that resolves a registry row's CHECK_COLUMN to its current Cortex broken flag."""
    whens = "\n                ".join(
        f"WHEN '{f.check_column}' THEN c.{f.check_column}" for f in FLAG_FIXES
    )
    # ELSE 1: an unknown / not-yet-mapped check is treated as still broken (never confirmed).
    return f"CASE r.CHECK_COLUMN\n                {whens}\n                ELSE 1\n            END"


def build_confirmed_merge(objs: RemediationObjects) -> str:
    """``MERGE`` that inserts newly-confirmed fixes into the handoff (idempotent).

    A registry PROD/FIXED row is confirmed when the source reloaded after the fix
    (``SRC_LOADED_UTC > FIXED_AT_UTC``) and its check is no longer broken. Keyed on the fix
    event, so a terminal that is re-fixed later (new ``FIXED_AT_UTC``) is a new handoff row.
    """
    select_terms = (
        [f"r.{c}" for c in _FIX_COLUMNS]
        + ["c.SRC_LOADED_UTC", f"{_CONFIRMED_AT_UTC} AS CONFIRMED_AT_UTC"]
        + [f"c.{c}" for c in _DIMENSION_COLUMNS]
    )
    select_sql = ",\n               ".join(select_terms)
    on = "\n   AND ".join(f"tgt.{k} = src.{k}" for k in _KEY)
    insert_cols = ", ".join(HANDOFF_COLUMNS)
    insert_vals = ", ".join(f"src.{c}" for c in HANDOFF_COLUMNS)
    return f"""MERGE INTO {objs.confirmed_table} tgt
USING (
    WITH {_cortex_scan(objs)}
    SELECT {select_sql}
    FROM {objs.registry_table} r
    JOIN cortex c ON c.TERMINAL_IDENTIFIER = r.TERMINAL_IDENTIFIER
    WHERE r.ENVIRONMENT = '{PROD}' AND r.STATUS = '{registry.STATUS_FIXED}'
      AND c.SRC_LOADED_UTC > r.FIXED_AT_UTC
      AND ({_still_broken_case()}) = 0
) src
ON {on}
WHEN NOT MATCHED THEN INSERT ({insert_cols})
VALUES ({insert_vals})"""


def build_mark_registry_confirmed(objs: RemediationObjects) -> str:
    """Flip registry rows that reached the handoff to ``STATUS='CONFIRMED'`` (kept, not deleted)."""
    match = (
        "h.ENVIRONMENT = r.ENVIRONMENT\n"
        "          AND h.TERMINAL_IDENTIFIER = r.TERMINAL_IDENTIFIER\n"
        "          AND h.CHECK_COLUMN = r.CHECK_COLUMN\n"
        "          AND h.FIXED_AT_UTC = r.FIXED_AT_UTC"
    )
    return f"""UPDATE {objs.registry_table} r
SET STATUS = '{registry.STATUS_CONFIRMED}',
    CONFIRMED_AT_UTC = (
        SELECT MAX(h.CONFIRMED_AT_UTC) FROM {objs.confirmed_table} h
        WHERE {match}
    )
WHERE r.ENVIRONMENT = '{PROD}' AND r.STATUS = '{registry.STATUS_FIXED}'
  AND EXISTS (
        SELECT 1 FROM {objs.confirmed_table} h
        WHERE {match}
  )"""


def _reconcile_body(objs: RemediationObjects) -> str:
    """The Scripting block body: hand off confirmed fixes, then mark the registry rows."""
    return (
        "BEGIN\n"
        "    BEGIN TRANSACTION;\n"
        f"    {build_confirmed_merge(objs)};\n"
        f"    {build_mark_registry_confirmed(objs)};\n"
        "    COMMIT;\n"
        "    RETURN 'reconciled';\n"
        "END"
    )


def build_reconcile_block(objs: RemediationObjects) -> str:
    """One executable statement running the full reconcile atomically (used by the app button)."""
    return f"EXECUTE IMMEDIATE $$\n{_reconcile_body(objs)};\n$$"


def build_reconcile_task(
    objs: RemediationObjects, warehouse: str, schedule: str = "USING CRON 30 6 * * * UTC"
) -> str:
    """``CREATE TASK`` running the reconcile daily after the Cortex load (owner-run, needs grants).

    Not part of the default Initialise: the task owner needs ``EXECUTE TASK`` on the account
    and ``SELECT`` on the Cortex source. Review ``sql/remediation/004_reconcile.sql`` and the
    runbook before installing.
    """
    return f"""CREATE TASK IF NOT EXISTS {objs.reconcile_task}
    WAREHOUSE = {warehouse}
    SCHEDULE = '{schedule}'
AS
{_reconcile_body(objs)};"""


def build_task_resume(objs: RemediationObjects) -> str:
    """Resume the reconcile task (tasks are created suspended)."""
    return f"ALTER TASK IF EXISTS {objs.reconcile_task} RESUME"


def build_pending_summary_query(objs: RemediationObjects) -> str:
    """One row: pending vs confirmed registry counts and the handoff total (for the UI)."""
    reg = objs.registry_table
    return (
        "SELECT\n"
        f"    (SELECT COUNT(*) FROM {reg}\n"
        f"        WHERE ENVIRONMENT = '{PROD}' AND STATUS = '{registry.STATUS_FIXED}')\n"
        "        AS PENDING_CONFIRMATION,\n"
        f"    (SELECT COUNT(*) FROM {reg}\n"
        f"        WHERE ENVIRONMENT = '{PROD}' AND STATUS = '{registry.STATUS_CONFIRMED}')\n"
        "        AS CONFIRMED_IN_REGISTRY,\n"
        f"    (SELECT COUNT(*) FROM {objs.confirmed_table}) AS HANDED_OFF,\n"
        f"    (SELECT MAX(CONFIRMED_AT_UTC) FROM {objs.confirmed_table}) AS LAST_CONFIRMED_AT_UTC"
    )
