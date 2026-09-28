-- =====================================================================
-- DCC Remediation & Analytics — schema and objects
-- Target: DEV_CORE_AAB.DCC_REMEDIATION  (dedicated schema, per approval)
-- Source of truth for discovery: PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE
--
-- REVIEW BEFORE RUNNING. This file is intentionally NOT executed by the app.
-- It is idempotent (IF NOT EXISTS / OR REPLACE) so it can be re-run safely.
-- No profit/revenue objects, no Excel ingestion (explicitly out of scope).
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS DEV_CORE_AAB.DCC_REMEDIATION;

-- ---------------------------------------------------------------------
-- 1. FLAG_REFERENCE (dimension)
--    Maps a maintenance-table CHECK column to the bit/procedure that
--    fixes it, the identifier kind to bind, and the value to send.
--    FIXABLE gates whether the UI shows a Fix button.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DEV_CORE_AAB.DCC_REMEDIATION.FLAG_REFERENCE (
    CHECK_COLUMN     VARCHAR(80)  NOT NULL PRIMARY KEY,  -- e.g. HANDLER_DCCENABLE_CHECK_O
    FLAG_NAME        VARCHAR(120) NOT NULL,              -- human label
    FLAG_TYPE        VARCHAR(30)  NOT NULL,              -- HANDLER | DCC_XPRESS | TEMPLATE | CONFIG | FIRMWARE | MERCHANT
    FIX_BIT          NUMBER(4,0),                        -- @display_config for the SP (NULL = not fixable here)
    TARGET_ID_KIND   VARCHAR(30),                        -- INSTANCE_IDENTIFIER | TERMINAL_IDENTIFIER | LOCATION_NO
    FIX_VALUE        VARCHAR(120),                        -- value sent (flag name / function); NULL = taken from the row
    SP_COLUMN        VARCHAR(60),                         -- handler column the SP edits (mirror of catalog.sp_column)
    FIXABLE          BOOLEAN      NOT NULL DEFAULT FALSE,
    NOTES            VARCHAR(500)
);

-- ---------------------------------------------------------------------
-- 2. HEALTH_DAILY_SNAPSHOT (fact — BROKEN rows only, one per day)
--    Storage-optimized: only in-scope BROKEN terminals are stored
--    (~15k/day vs ~52k total). Clustered by SNAPSHOT_DATE for pruning.
--    Carries all three identifiers so a fix maps to the correct bit.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DEV_CORE_AAB.DCC_REMEDIATION.HEALTH_DAILY_SNAPSHOT (
    SNAPSHOT_DATE        DATE         NOT NULL,
    TERMINAL_IDENTIFIER  VARCHAR(100) NOT NULL,
    INSTANCE_IDENTIFIER  VARCHAR(100),
    LOCATION_NO          VARCHAR(50),
    -- dimensions for filtering / analytics
    COUNTRY_NAME         VARCHAR(100),
    REGION               VARCHAR(100),
    INDUSTRY_NAME        VARCHAR(100),
    BANK_MERCHANT_ID     VARCHAR(50),
    MERCHANT_NAME        VARCHAR(250),
    CUSTOMER_NAME        VARCHAR(250),
    ACQUIRER_NAME        VARCHAR(250),
    LOCATION_NAME        VARCHAR(500),
    TERMINAL_BRAND_NAME  VARCHAR(100),
    TERMINAL_MODEL_NAME  VARCHAR(200),
    FIRMWARE_VERSION     VARCHAR(200),
    -- per-check broken flags (1 = fails/broken)
    LOCATION_DCCENABLED_CHECK_C            INTEGER,
    HANDLER_DCCENABLE_CHECK_O              INTEGER,
    HANDLER_DCCENABLECOMPLETION_CHECK_O    INTEGER,
    HANDLER_DCCENABLEAUTH_CHECK_O          INTEGER,
    HANDLER_DCCENABLENFC_CHECK_O           INTEGER,
    HANDLER_DCCENABLENFCSINGLETAP_CHECK_O  INTEGER,
    DCCFLAGSENABLED_CHECK_C                INTEGER,
    DCCXPRESSCO_CHECK_O                    INTEGER,
    DCCXPRESSCODT_CHECK_O                  INTEGER,
    DCCXPRESSCOFALLBACK_CHECK_O            INTEGER,
    DCCMERCHANT_NO_CHECK_O                 INTEGER,
    FIRMWARE_VERSION_CHECK                 INTEGER,
    PRINTOUTTYPETEMPLATEDCC_CHECK_C        INTEGER,        -- Bit 16 (receipt template) — fixable
    CONFIGDOWNLOAD_VERSION_CHECK_C         INTEGER,        -- Bit 2 (config download version) — fixable
    IS_DCC_BROKEN        BOOLEAN      NOT NULL,           -- always TRUE here (broken-only), kept explicit
    SOURCE_LAST_ALTERED  TIMESTAMP_NTZ,                   -- freshness of the maintenance table at capture
    CAPTURED_AT          TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (SNAPSHOT_DATE, TERMINAL_IDENTIFIER)
)
CLUSTER BY (SNAPSHOT_DATE);

-- ---------------------------------------------------------------------
-- 3. APP_FIX_LOG (authoritative record of fixes THIS APP applied)
--    The "don't re-fix" source and the audit trail. One row per attempt.
--    Dimensions are denormalized so analytics need no join.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG (
    FIX_ID               NUMBER       AUTOINCREMENT START 1 INCREMENT 1,
    CORRELATION_ID       VARCHAR(64)  NOT NULL,           -- threads pre-check -> apply -> verify -> log
    APPLIED_AT           TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    APPLIED_BY           VARCHAR(150) NOT NULL,           -- SSO identity
    MODE                 VARCHAR(12)  NOT NULL,           -- LIVE | SIMULATION
    ENVIRONMENT          VARCHAR(20)  NOT NULL,           -- DEV | UAT | PROD
    SERVER               VARCHAR(200) NOT NULL,
    DATABASE_NAME        VARCHAR(100) NOT NULL,
    FIX_BIT              NUMBER(4,0)  NOT NULL,
    CHECK_COLUMN         VARCHAR(80),
    FLAG_NAME            VARCHAR(120),
    TARGET_ID_KIND       VARCHAR(30)  NOT NULL,
    TARGET_IDENTIFIER    VARCHAR(100) NOT NULL,           -- the id actually bound to the SP
    INSTANCE_IDENTIFIER  VARCHAR(100),
    TERMINAL_IDENTIFIER  VARCHAR(100),
    LOCATION_NO          VARCHAR(50),
    VALUE_SENT           VARCHAR(120),
    STATE_BEFORE         VARCHAR,                          -- flag/value observed before (live pre-check)
    STATE_AFTER          VARCHAR,                          -- flag/value observed after (verify)
    VERIFIED             BOOLEAN,                          -- did the live read confirm the change?
    ROLLBACK_SCRIPT      VARCHAR,                          -- procedure's own compensating script
    OUTCOME              VARCHAR(30)  NOT NULL,            -- APPLIED | SKIPPED_ALREADY_OK | FAILED | ROLLED_BACK
    ERROR                VARCHAR,
    -- denormalized dims for analytics
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
);

-- ---------------------------------------------------------------------
-- 4. Analytics views (no extra physical tables)
-- ---------------------------------------------------------------------

-- 4a. Latest snapshot per terminal, MINUS terminals this app fixed after the
--     snapshot's source was captured (the stale-data guard). A terminal the app
--     fixed today but that still shows broken in today's (older) snapshot is
--     surfaced as "awaiting refresh" rather than as actionable.
CREATE OR REPLACE VIEW DEV_CORE_AAB.DCC_REMEDIATION.V_CURRENT_BROKEN AS
WITH latest AS (
    SELECT * FROM DEV_CORE_AAB.DCC_REMEDIATION.HEALTH_DAILY_SNAPSHOT
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY TERMINAL_IDENTIFIER ORDER BY SNAPSHOT_DATE DESC
    ) = 1
),
recent_fix AS (
    SELECT TERMINAL_IDENTIFIER, CHECK_COLUMN, MAX(APPLIED_AT) AS LAST_FIX_AT
    FROM DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG
    WHERE OUTCOME = 'APPLIED' AND VERIFIED = TRUE AND MODE = 'LIVE'
    GROUP BY TERMINAL_IDENTIFIER, CHECK_COLUMN
)
SELECT
    l.*,
    rf.LAST_FIX_AT,
    CASE
        WHEN rf.LAST_FIX_AT IS NOT NULL
             AND rf.LAST_FIX_AT > l.SOURCE_LAST_ALTERED
        THEN 'AWAITING_REFRESH'      -- we fixed it after this snapshot's source; don't re-fix
        ELSE 'ACTIONABLE'
    END AS REMEDIATION_STATE
FROM latest l
LEFT JOIN recent_fix rf USING (TERMINAL_IDENTIFIER);

-- 4b. Fix history (app-applied), ready for date/dimension analytics.
CREATE OR REPLACE VIEW DEV_CORE_AAB.DCC_REMEDIATION.V_FIX_HISTORY AS
SELECT
    APPLIED_AT::DATE AS FIX_DATE,
    APPLIED_BY, MODE, ENVIRONMENT, FIX_BIT, CHECK_COLUMN, FLAG_NAME,
    TARGET_ID_KIND, TARGET_IDENTIFIER, VALUE_SENT, VERIFIED, OUTCOME,
    BANK_MERCHANT_ID, MERCHANT_NAME, CUSTOMER_NAME, COUNTRY_NAME, REGION,
    INDUSTRY_NAME, ACQUIRER_NAME, TERMINAL_BRAND_NAME, TERMINAL_MODEL_NAME
FROM DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG;

-- 4c. KPI rollup for the analytics panel.
CREATE OR REPLACE VIEW DEV_CORE_AAB.DCC_REMEDIATION.V_REMEDIATION_KPIS AS
SELECT
    (SELECT COUNT(*) FROM DEV_CORE_AAB.DCC_REMEDIATION.V_CURRENT_BROKEN
        WHERE REMEDIATION_STATE = 'ACTIONABLE')                       AS BROKEN_ACTIONABLE,
    (SELECT COUNT(*) FROM DEV_CORE_AAB.DCC_REMEDIATION.V_CURRENT_BROKEN
        WHERE REMEDIATION_STATE = 'AWAITING_REFRESH')                 AS AWAITING_REFRESH,
    (SELECT COUNT(*) FROM DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG
        WHERE OUTCOME = 'APPLIED' AND MODE = 'LIVE')                  AS TOTAL_LIVE_FIXES,
    (SELECT COUNT(DISTINCT TERMINAL_IDENTIFIER)
        FROM DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG
        WHERE OUTCOME = 'APPLIED' AND MODE = 'LIVE')                  AS UNIQUE_TERMINALS_FIXED,
    (SELECT MAX(SNAPSHOT_DATE) FROM DEV_CORE_AAB.DCC_REMEDIATION.HEALTH_DAILY_SNAPSHOT) AS LATEST_SNAPSHOT_DATE;
