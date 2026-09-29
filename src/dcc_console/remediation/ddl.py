"""Per-user schema DDL for the remediation feature.

Generates the `CREATE SCHEMA` + tables + views and the `FLAG_REFERENCE` seed for
*any* target database/schema, so each user can initialise their own
``DEV_CORE_<x>.DCC_REMEDIATION`` from the app (idempotent — safe to re-run).

Object shapes mirror ``sql/remediation/001_schema.sql``; the snapshot's check
columns and the seed rows are generated from :mod:`.mapping` so they cannot drift
from the rest of the package. Database/schema names come from
:class:`RemediationObjects`, which validates them as bare identifiers before they
reach any SQL text.
"""

from __future__ import annotations

from . import RemediationObjects
from .mapping import FLAG_FIXES, TRACKED_CHECK_COLUMNS

TABLE_NAMES: tuple[str, ...] = ("FLAG_REFERENCE", "HEALTH_DAILY_SNAPSHOT", "APP_FIX_LOG")

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


def _create_fix_log(objs: RemediationObjects) -> str:
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
    PRIMARY KEY (FIX_ID)
)"""


def _create_v_current_broken(objs: RemediationObjects) -> str:
    return f"""CREATE OR REPLACE VIEW {objs.v_current_broken} AS
WITH latest AS (
    SELECT * FROM {objs.snapshot_table}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY TERMINAL_IDENTIFIER ORDER BY SNAPSHOT_DATE DESC
    ) = 1
),
recent_fix AS (
    SELECT TERMINAL_IDENTIFIER, CHECK_COLUMN, MAX(APPLIED_AT) AS LAST_FIX_AT
    FROM {objs.fix_log_table}
    WHERE OUTCOME = 'APPLIED' AND VERIFIED = TRUE AND MODE = 'LIVE'
    GROUP BY TERMINAL_IDENTIFIER, CHECK_COLUMN
)
SELECT
    l.*,
    rf.LAST_FIX_AT,
    CASE
        WHEN rf.LAST_FIX_AT IS NOT NULL
             AND rf.LAST_FIX_AT > l.SOURCE_LAST_ALTERED
        THEN 'AWAITING_REFRESH'
        ELSE 'ACTIONABLE'
    END AS REMEDIATION_STATE
FROM latest l
LEFT JOIN recent_fix rf USING (TERMINAL_IDENTIFIER)"""


def _create_v_fix_history(objs: RemediationObjects) -> str:
    return f"""CREATE OR REPLACE VIEW {objs.v_fix_history} AS
SELECT
    APPLIED_AT::DATE AS FIX_DATE,
    APPLIED_BY, MODE, ENVIRONMENT, FIX_BIT, CHECK_COLUMN, FLAG_NAME,
    TARGET_ID_KIND, TARGET_IDENTIFIER, VALUE_SENT, VERIFIED, OUTCOME,
    BANK_MERCHANT_ID, MERCHANT_NAME, CUSTOMER_NAME, COUNTRY_NAME, REGION,
    INDUSTRY_NAME, ACQUIRER_NAME, TERMINAL_BRAND_NAME, TERMINAL_MODEL_NAME
FROM {objs.fix_log_table}"""


def _create_v_kpis(objs: RemediationObjects) -> str:
    return f"""CREATE OR REPLACE VIEW {objs.v_remediation_kpis} AS
SELECT
    (SELECT COUNT(*) FROM {objs.v_current_broken}
        WHERE REMEDIATION_STATE = 'ACTIONABLE')                       AS BROKEN_ACTIONABLE,
    (SELECT COUNT(*) FROM {objs.v_current_broken}
        WHERE REMEDIATION_STATE = 'AWAITING_REFRESH')                 AS AWAITING_REFRESH,
    (SELECT COUNT(*) FROM {objs.fix_log_table}
        WHERE OUTCOME = 'APPLIED' AND MODE = 'LIVE')                  AS TOTAL_LIVE_FIXES,
    (SELECT COUNT(DISTINCT TERMINAL_IDENTIFIER) FROM {objs.fix_log_table}
        WHERE OUTCOME = 'APPLIED' AND MODE = 'LIVE')                  AS UNIQUE_TERMINALS_FIXED,
    (SELECT MAX(SNAPSHOT_DATE) FROM {objs.snapshot_table})            AS LATEST_SNAPSHOT_DATE"""


def build_create_statements(objs: RemediationObjects) -> list[str]:
    """Return the ordered, idempotent statements that create the schema + objects."""
    return [
        f"CREATE SCHEMA IF NOT EXISTS {objs.schema_fqn}",
        _create_flag_reference(objs),
        _create_snapshot(objs),
        _create_fix_log(objs),
        _create_v_current_broken(objs),
        _create_v_fix_history(objs),
        _create_v_kpis(objs),
    ]


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


def build_objects_present_query(objs: RemediationObjects) -> str:
    """Return SQL counting how many of the 3 core tables already exist (0..3)."""
    names = ", ".join(f"'{n}'" for n in TABLE_NAMES)
    return (
        f"SELECT COUNT(*) AS N\n"
        f"FROM {objs.database}.INFORMATION_SCHEMA.TABLES\n"
        f"WHERE TABLE_SCHEMA = '{objs.schema.upper()}' AND TABLE_NAME IN ({names})"
    )
