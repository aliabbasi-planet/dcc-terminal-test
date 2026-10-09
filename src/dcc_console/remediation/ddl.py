"""Schema DDL for the remediation feature (per-user schema + shared fix log).

Generates, for *any* target, the objects each user needs in their own
``DEV_CORE_<x>.DCC_REMEDIATION`` (flag reference, daily snapshot, views) and the
team-wide objects in the shared schema (``APP_FIX_LOG`` and the ``FIX_OPERATORS``
live-fix allowlist). Every statement is idempotent — safe to re-run — and older
schemas are upgraded in place (new fix-log columns, rewritten views).

The snapshot's check columns, the view's per-check logic and the seed rows are all
generated from :mod:`.mapping`, so they cannot drift from the rest of the package.
Database/schema names come from :class:`RemediationObjects`, which validates them
as bare identifiers before they reach any SQL text.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import RemediationObjects, reconcile, registry
from .mapping import FIXABLE_CHECK_COLUMNS, FLAG_FIXES, TRACKED_CHECK_COLUMNS

# Tables in the user's own schema, and in the (possibly identical) shared schema.
OWN_TABLES: tuple[str, ...] = ("FLAG_REFERENCE", "HEALTH_DAILY_SNAPSHOT")
SHARED_TABLES: tuple[str, ...] = (
    "APP_FIX_LOG",
    "FIX_OPERATORS",
    "DCC_FIX_REGISTRY",
    "DCC_CONFIRMED_FIXES",
)
# Columns added to APP_FIX_LOG after its first release (migrated in place).
FIX_LOG_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("SQL_LOGIN", "VARCHAR(128)"),
    ("NOTES", "VARCHAR(1000)"),
    ("CHANGE_REF", "VARCHAR(120)"),
)
# Columns added to FIX_OPERATORS after its first release (migrated in place). CAN_PROD is
# added nullable so the ALTER never needs a default backfill on a non-empty table; the
# operator check treats a missing / NULL value as FALSE (no PROD rights by default).
OPERATORS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (("CAN_PROD", "BOOLEAN"),)
# Columns added to DCC_FIX_REGISTRY after its first release (migrated in place). Set when
# the Cortex source confirms a fix and it is handed to the profit app (see :mod:`.reconcile`).
REGISTRY_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (("CONFIRMED_AT_UTC", "TIMESTAMP_NTZ"),)
# A column only the current V_CURRENT_BROKEN exposes; if absent, views need upgrading.
VIEW_MARKER_COLUMN = "OPEN_FIXABLE_CHECKS"

# The snapshot is built from PROD data, so only PROD outcomes change the worklist.
SNAPSHOT_ENVIRONMENT = "PROD"

# Non-check snapshot columns with their types, in table order (mirrors 001).
_SNAPSHOT_FIXED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("SNAPSHOT_DATE", "DATE NOT NULL"),
    ("TERMINAL_IDENTIFIER", "VARCHAR(100) NOT NULL"),
    ("INSTANCE_IDENTIFIER", "VARCHAR(100)"),
    ("LOCATION_NO", "VARCHAR(50)"),
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


def _lit(value: object) -> str:
    """Render a Python value as a SQL literal for the generated seed."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(_lit(v) for v in values)


# --------------------------------------------------------------------------- #
# Shared schema: fix log + operator allowlist
# --------------------------------------------------------------------------- #


def _create_fix_log(objs: RemediationObjects) -> str:
    added = ",\n    ".join(f"{name:<20} {ddl}" for name, ddl in FIX_LOG_ADDED_COLUMNS)
    return f"""CREATE TABLE IF NOT EXISTS {objs.fix_log_table} (
    FIX_ID               NUMBER       AUTOINCREMENT START 1 INCREMENT 1,
    CORRELATION_ID       VARCHAR(64)  NOT NULL,
    APPLIED_AT           TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    APPLIED_BY           VARCHAR(150) NOT NULL,
    MODE                 VARCHAR(12)  NOT NULL,
    ENVIRONMENT          VARCHAR(20)  NOT NULL,
    SERVER               VARCHAR(200) NOT NULL,
    DATABASE_NAME        VARCHAR(100) NOT NULL,
    FIX_BIT              NUMBER(4,0)  NOT NULL,
    CHECK_COLUMN         VARCHAR(80),
    FLAG_NAME            VARCHAR(120),
    TARGET_ID_KIND       VARCHAR(30)  NOT NULL,
    TARGET_IDENTIFIER    VARCHAR(100) NOT NULL,
    INSTANCE_IDENTIFIER  VARCHAR(100),
    TERMINAL_IDENTIFIER  VARCHAR(100),
    LOCATION_NO          VARCHAR(50),
    VALUE_SENT           VARCHAR(120),
    STATE_BEFORE         VARCHAR,
    STATE_AFTER          VARCHAR,
    VERIFIED             BOOLEAN,
    ROLLBACK_SCRIPT      VARCHAR,
    OUTCOME              VARCHAR(30)  NOT NULL,
    ERROR                VARCHAR,
    BANK_MERCHANT_ID     VARCHAR(50),
    MERCHANT_NAME        VARCHAR(250),
    CUSTOMER_NAME        VARCHAR(250),
    COUNTRY_NAME         VARCHAR(100),
    REGION               VARCHAR(100),
    INDUSTRY_NAME        VARCHAR(100),
    ACQUIRER_NAME        VARCHAR(250),
    TERMINAL_BRAND_NAME  VARCHAR(100),
    TERMINAL_MODEL_NAME  VARCHAR(200),
    {added},
    PRIMARY KEY (FIX_ID)
)"""


def _upgrade_fix_log(objs: RemediationObjects) -> list[str]:
    """Add columns introduced after the first release to an existing fix log."""
    return [
        f"ALTER TABLE {objs.fix_log_table} ADD COLUMN IF NOT EXISTS {name} {ddl}"
        for name, ddl in FIX_LOG_ADDED_COLUMNS
    ]


def _create_operators(objs: RemediationObjects) -> str:
    return f"""CREATE TABLE IF NOT EXISTS {objs.operators_table} (
    USER_NAME   VARCHAR(150)  NOT NULL PRIMARY KEY,
    ACTIVE      BOOLEAN       NOT NULL DEFAULT TRUE,
    CAN_PROD    BOOLEAN       NOT NULL DEFAULT FALSE,
    ADDED_BY    VARCHAR(150)  NOT NULL DEFAULT CURRENT_USER(),
    ADDED_AT    TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    NOTES       VARCHAR(500)
)"""


def _upgrade_operators(objs: RemediationObjects) -> list[str]:
    """Add columns introduced after the first release to an existing allowlist."""
    return [
        f"ALTER TABLE {objs.operators_table} ADD COLUMN IF NOT EXISTS {name} {ddl}"
        for name, ddl in OPERATORS_ADDED_COLUMNS
    ]


def _upgrade_registry(objs: RemediationObjects) -> list[str]:
    """Add columns introduced after the first release to an existing fix registry."""
    return [
        f"ALTER TABLE {objs.registry_table} ADD COLUMN IF NOT EXISTS {name} {ddl}"
        for name, ddl in REGISTRY_ADDED_COLUMNS
    ]


def _seed_first_operator(objs: RemediationObjects) -> str:
    """Whoever initialises an empty allowlist becomes its first operator."""
    return (
        f"INSERT INTO {objs.operators_table} (USER_NAME, NOTES)\n"  # noqa: S608
        "SELECT CURRENT_USER(), 'First operator (initialised the shared fix log)'\n"
        f"WHERE NOT EXISTS (SELECT 1 FROM {objs.operators_table})"
    )


def build_shared_statements(objs: RemediationObjects) -> list[str]:
    """Idempotent statements for the shared fix log + operator allowlist + fix registry."""
    return [
        f"CREATE SCHEMA IF NOT EXISTS {objs.shared_fqn}",
        _create_fix_log(objs),
        *_upgrade_fix_log(objs),
        _create_operators(objs),
        *_upgrade_operators(objs),
        _seed_first_operator(objs),
        registry.create_table(objs),
        *_upgrade_registry(objs),
        registry.build_backfill_from_log(objs),
        reconcile.create_confirmed_table(objs),
    ]


# --------------------------------------------------------------------------- #
# The user's own schema: flag reference, snapshot, views
# --------------------------------------------------------------------------- #


def _create_flag_reference(objs: RemediationObjects) -> str:
    return f"""CREATE TABLE IF NOT EXISTS {objs.flag_reference_table} (
    CHECK_COLUMN     VARCHAR(80)  NOT NULL PRIMARY KEY,
    FLAG_NAME        VARCHAR(120) NOT NULL,
    FLAG_TYPE        VARCHAR(30)  NOT NULL,
    FIX_BIT          NUMBER(4,0),
    TARGET_ID_KIND   VARCHAR(30),
    FIX_VALUE        VARCHAR(120),
    SP_COLUMN        VARCHAR(60),
    FIXABLE          BOOLEAN      NOT NULL DEFAULT FALSE,
    NOTES            VARCHAR(500)
)"""


def _create_snapshot(objs: RemediationObjects) -> str:
    fixed = ",\n    ".join(f"{name:<20} {ddl}" for name, ddl in _SNAPSHOT_FIXED_COLUMNS)
    checks = ",\n    ".join(f"{c} INTEGER" for c in TRACKED_CHECK_COLUMNS)
    return f"""CREATE TABLE IF NOT EXISTS {objs.snapshot_table} (
    {fixed},
    {checks},
    IS_DCC_BROKEN        BOOLEAN      NOT NULL,
    SOURCE_LAST_ALTERED  TIMESTAMP_NTZ,
    CAPTURED_AT          TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (SNAPSHOT_DATE, TERMINAL_IDENTIFIER)
)
CLUSTER BY (SNAPSHOT_DATE)"""


def _open_fixable_expr() -> str:
    """Count of fixable checks broken on the row and not yet resolved."""
    return "\n        + ".join(
        f"IFF({c} = 1 AND NOT ARRAY_CONTAINS('{c}'::VARIANT, FIXED_CHECKS), 1, 0)"
        for c in FIXABLE_CHECK_COLUMNS
    )


def _create_v_current_broken(objs: RemediationObjects) -> str:
    return f"""CREATE OR REPLACE VIEW {objs.v_current_broken} AS
WITH latest AS (
    -- Only the most recent load: a terminal repaired since an earlier snapshot has no
    -- row in the newest one and must drop out rather than resurface from an old date.
    SELECT * FROM {objs.snapshot_table}
    WHERE SNAPSHOT_DATE = (SELECT MAX(SNAPSHOT_DATE) FROM {objs.snapshot_table})
),
fixed AS (
    -- Durable registry of verified {SNAPSHOT_ENVIRONMENT} fixes (DCC_FIX_REGISTRY): a row
    -- is written when a live fix is verified by a fresh SQL-Server read (or a live
    -- pre-check proves the check already satisfied) and DELETED when a rollback is
    -- confirmed, so a later rollback re-opens the check. It survives the daily refresh,
    -- and UAT/DEV rehearsals never hide {SNAPSHOT_ENVIRONMENT} work (only
    -- ENVIRONMENT = '{SNAPSHOT_ENVIRONMENT}' rows count). Only fixes newer than the source
    -- load the snapshot was built from count, so a terminal re-broken in a later load
    -- resurfaces (FIXED_AT_UTC and SOURCE_LAST_ALTERED are both UTC).
    SELECT l.TERMINAL_IDENTIFIER,
           ARRAY_AGG(DISTINCT g.CHECK_COLUMN) AS FIXED_CHECKS,
           MAX(g.FIXED_AT_UTC) AS LAST_FIX_AT_UTC
    FROM latest l
    JOIN {objs.registry_table} g
      ON g.TERMINAL_IDENTIFIER = l.TERMINAL_IDENTIFIER
     AND g.ENVIRONMENT = '{SNAPSHOT_ENVIRONMENT}' AND g.STATUS IN ('FIXED', 'CONFIRMED')
     AND g.FIXED_AT_UTC > l.SOURCE_LAST_ALTERED
    GROUP BY l.TERMINAL_IDENTIFIER
),
joined AS (
    SELECT l.*,
           COALESCE(f.FIXED_CHECKS, ARRAY_CONSTRUCT()) AS FIXED_CHECKS,
           f.LAST_FIX_AT_UTC
    FROM latest l
    LEFT JOIN fixed f ON f.TERMINAL_IDENTIFIER = l.TERMINAL_IDENTIFIER
),
scored AS (
    SELECT *,
        {_open_fixable_expr()} AS {VIEW_MARKER_COLUMN}
    FROM joined
)
-- AWAITING_REFRESH: something was resolved and no fixable check is still open, so the
-- next load should clear it. Terminals whose only broken checks are not fixable here
-- stay ACTIONABLE (visible with "Fixable checks only" switched off).
SELECT *,
    CASE WHEN ARRAY_SIZE(FIXED_CHECKS) > 0 AND {VIEW_MARKER_COLUMN} = 0
         THEN 'AWAITING_REFRESH' ELSE 'ACTIONABLE' END AS REMEDIATION_STATE
FROM scored"""


def _create_v_fix_history(objs: RemediationObjects) -> str:
    return f"""CREATE OR REPLACE VIEW {objs.v_fix_history} AS
SELECT
    APPLIED_AT, APPLIED_AT::DATE AS FIX_DATE, CORRELATION_ID,
    APPLIED_BY, SQL_LOGIN, MODE, ENVIRONMENT, FIX_BIT, CHECK_COLUMN, FLAG_NAME,
    TARGET_ID_KIND, TARGET_IDENTIFIER, TERMINAL_IDENTIFIER, VALUE_SENT, VERIFIED,
    OUTCOME, ERROR, NOTES,
    BANK_MERCHANT_ID, MERCHANT_NAME, CUSTOMER_NAME, COUNTRY_NAME, REGION,
    INDUSTRY_NAME, ACQUIRER_NAME, TERMINAL_BRAND_NAME, TERMINAL_MODEL_NAME
FROM {objs.fix_log_table}"""


def _create_v_kpis(objs: RemediationObjects) -> str:
    live_applied = "OUTCOME = 'APPLIED' AND MODE = 'LIVE'"
    prod = f"ENVIRONMENT = '{SNAPSHOT_ENVIRONMENT}'"
    return f"""CREATE OR REPLACE VIEW {objs.v_remediation_kpis} AS
SELECT
    (SELECT COUNT(*) FROM {objs.v_current_broken}
        WHERE REMEDIATION_STATE = 'ACTIONABLE')                       AS BROKEN_ACTIONABLE,
    (SELECT COUNT(*) FROM {objs.v_current_broken}
        WHERE REMEDIATION_STATE = 'AWAITING_REFRESH')                 AS AWAITING_REFRESH,
    (SELECT COUNT(*) FROM {objs.v_current_broken}
        WHERE OPEN_FIXABLE_CHECKS > 0)                                 AS FIXABLE_TERMINALS,
    -- One CORRELATION_ID per procedure call; one row per terminal it covered.
    (SELECT COUNT(*) FROM (
        SELECT DISTINCT CORRELATION_ID, APPLIED_AT FROM {objs.fix_log_table}
        WHERE {live_applied} AND {prod}
    ))                                                                AS TOTAL_LIVE_FIXES,
    (SELECT COUNT(*) FROM (
        SELECT DISTINCT CORRELATION_ID, APPLIED_AT FROM {objs.fix_log_table}
        WHERE MODE = 'LIVE' AND OUTCOME = 'SKIPPED_ALREADY_OK' AND {prod}
    ))
                                                                      AS ALREADY_OK_PROD,
    (SELECT COUNT(*) FROM (
        SELECT DISTINCT CORRELATION_ID, APPLIED_AT FROM {objs.fix_log_table}
        WHERE MODE = 'SIMULATION' AND {prod}
    ))                                                                AS PROD_SIMULATIONS,
    (SELECT COUNT(DISTINCT TERMINAL_IDENTIFIER) FROM {objs.fix_log_table}
        WHERE {live_applied} AND {prod})                              AS UNIQUE_TERMINALS_FIXED,
    (SELECT COUNT(*) FROM (
        SELECT DISTINCT CORRELATION_ID, APPLIED_AT FROM {objs.fix_log_table}
        WHERE MODE = 'SIMULATION' AND ENVIRONMENT IN ('UAT', 'DEV')
    ))                                                                AS REHEARSAL_FIXES,
    (SELECT COUNT(*) FROM (
        SELECT DISTINCT CORRELATION_ID, APPLIED_AT FROM {objs.fix_log_table}
        WHERE MODE = 'LIVE' AND OUTCOME = 'APPLIED' AND ENVIRONMENT IN ('UAT', 'DEV')
    ))                                                                AS LIVE_APPLIED_UAT_DEV,
    (SELECT COUNT(*) FROM {objs.registry_table}
        WHERE {prod} AND STATUS IN ('{registry.STATUS_FIXED}', '{registry.STATUS_CONFIRMED}'))
                                                                      AS REGISTERED_FIXES,
    (SELECT COUNT(*) FROM {objs.registry_table}
        WHERE {prod} AND STATUS = '{registry.STATUS_FIXED}')          AS PENDING_CONFIRMATION,
    (SELECT COUNT(*) FROM {objs.registry_table}
        WHERE {prod} AND STATUS = '{registry.STATUS_CONFIRMED}')      AS CONFIRMED_FIXES,
    (SELECT COUNT(*) FROM {objs.confirmed_table})                     AS HANDED_OFF_FIXES,
    (SELECT MAX(SNAPSHOT_DATE) FROM {objs.snapshot_table})            AS LATEST_SNAPSHOT_DATE"""


def build_create_statements(objs: RemediationObjects) -> list[str]:
    """Idempotent statements for the user's own schema (needs the shared log to exist)."""
    return [
        f"CREATE SCHEMA IF NOT EXISTS {objs.schema_fqn}",
        _create_flag_reference(objs),
        _create_snapshot(objs),
        _create_v_current_broken(objs),
        _create_v_fix_history(objs),
        _create_v_kpis(objs),
    ]


def build_initialise_statements(objs: RemediationObjects, include_shared: bool = True) -> list[str]:
    """Everything Initialise runs, in dependency order (shared log before the views).

    ``include_shared=False`` skips the shared objects — used when they already exist
    and are current, so teammates who cannot alter the shared schema never try to.
    """
    shared = build_shared_statements(objs) if include_shared else []
    return shared + build_create_statements(objs)


def build_seed_merge(objs: RemediationObjects) -> str:
    """Return the idempotent ``FLAG_REFERENCE`` seed, generated from mapping."""
    rows = ",\n        ".join(
        "("
        + ", ".join(
            _lit(v)
            for v in (
                f.check_column,
                f.flag_name,
                f.flag_type,
                f.fix_bit,
                f.target_id_kind,
                f.fix_value,
                f.sp_column,
                f.fixable,
                f.notes,
            )
        )
        + ")"
        for f in FLAG_FIXES
    )
    return f"""MERGE INTO {objs.flag_reference_table} tgt
USING (
    SELECT * FROM VALUES
        {rows}
    AS v(CHECK_COLUMN, FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND,
         FIX_VALUE, SP_COLUMN, FIXABLE, NOTES)
) src
ON tgt.CHECK_COLUMN = src.CHECK_COLUMN
WHEN MATCHED THEN UPDATE SET
    FLAG_NAME = src.FLAG_NAME, FLAG_TYPE = src.FLAG_TYPE, FIX_BIT = src.FIX_BIT,
    TARGET_ID_KIND = src.TARGET_ID_KIND, FIX_VALUE = src.FIX_VALUE,
    SP_COLUMN = src.SP_COLUMN, FIXABLE = src.FIXABLE, NOTES = src.NOTES
WHEN NOT MATCHED THEN INSERT
    (CHECK_COLUMN, FLAG_NAME, FLAG_TYPE, FIX_BIT, TARGET_ID_KIND, FIX_VALUE,
     SP_COLUMN, FIXABLE, NOTES)
    VALUES (src.CHECK_COLUMN, src.FLAG_NAME, src.FLAG_TYPE, src.FIX_BIT,
            src.TARGET_ID_KIND, src.FIX_VALUE, src.SP_COLUMN, src.FIXABLE, src.NOTES)"""


# --------------------------------------------------------------------------- #
# Readiness: is everything present and current?
# --------------------------------------------------------------------------- #


def build_objects_present_query(objs: RemediationObjects) -> str:
    """One row describing which objects exist (own tables, shared tables, upgrades)."""
    added = tuple(name for name, _ in FIX_LOG_ADDED_COLUMNS)
    operator_added = tuple(name for name, _ in OPERATORS_ADDED_COLUMNS)
    registry_added = tuple(name for name, _ in REGISTRY_ADDED_COLUMNS)
    return f"""SELECT
    (SELECT COUNT(*) FROM {objs.database}.INFORMATION_SCHEMA.TABLES
      WHERE TABLE_SCHEMA = '{objs.schema.upper()}'
        AND TABLE_NAME IN ({_in_list(OWN_TABLES)}))                 AS OWN_TABLES,
    (SELECT COUNT(*) FROM {objs.shared_database}.INFORMATION_SCHEMA.TABLES
      WHERE TABLE_SCHEMA = '{objs.shared_schema.upper()}'
        AND TABLE_NAME IN ({_in_list(SHARED_TABLES)}))              AS SHARED_TABLES,
    (SELECT COUNT(*) FROM {objs.shared_database}.INFORMATION_SCHEMA.COLUMNS
      WHERE TABLE_SCHEMA = '{objs.shared_schema.upper()}' AND TABLE_NAME = 'APP_FIX_LOG'
        AND COLUMN_NAME IN ({_in_list(added)}))                     AS FIX_LOG_COLUMNS,
    (SELECT COUNT(*) FROM {objs.shared_database}.INFORMATION_SCHEMA.COLUMNS
      WHERE TABLE_SCHEMA = '{objs.shared_schema.upper()}' AND TABLE_NAME = 'FIX_OPERATORS'
        AND COLUMN_NAME IN ({_in_list(operator_added)}))            AS OPERATOR_COLUMNS,
    (SELECT COUNT(*) FROM {objs.shared_database}.INFORMATION_SCHEMA.COLUMNS
      WHERE TABLE_SCHEMA = '{objs.shared_schema.upper()}' AND TABLE_NAME = 'DCC_FIX_REGISTRY'
        AND COLUMN_NAME IN ({_in_list(registry_added)}))            AS REGISTRY_COLUMNS,
    (SELECT COUNT(*) FROM {objs.database}.INFORMATION_SCHEMA.COLUMNS
      WHERE TABLE_SCHEMA = '{objs.schema.upper()}' AND TABLE_NAME = 'V_CURRENT_BROKEN'
        AND COLUMN_NAME = '{VIEW_MARKER_COLUMN}')                     AS VIEW_CURRENT"""


@dataclass(frozen=True)
class SchemaStatus:
    own_ready: bool
    shared_ready: bool
    views_ready: bool
    missing: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.own_ready and self.shared_ready and self.views_ready


def schema_status(row: dict) -> SchemaStatus:
    """Interpret one row of :func:`build_objects_present_query`."""

    def count(key: str) -> int:
        value = row.get(key)
        return int(value) if value is not None else 0

    own = count("OWN_TABLES") >= len(OWN_TABLES)
    shared = (
        count("SHARED_TABLES") >= len(SHARED_TABLES)
        and count("FIX_LOG_COLUMNS") >= len(FIX_LOG_ADDED_COLUMNS)
        and count("OPERATOR_COLUMNS") >= len(OPERATORS_ADDED_COLUMNS)
        and count("REGISTRY_COLUMNS") >= len(REGISTRY_ADDED_COLUMNS)
    )
    views = count("VIEW_CURRENT") >= 1
    missing = []
    if not own:
        missing.append("snapshot / flag-reference tables")
    if not shared:
        missing.append("shared fix log / operator allowlist / fix registry (or new columns)")
    if not views:
        missing.append("current worklist views")
    return SchemaStatus(own, shared, views, tuple(missing))
