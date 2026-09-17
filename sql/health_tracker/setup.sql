-- ============================================================================
-- DCC Terminal Health Tracker V3 — setup DDL
-- ============================================================================
-- Run this once in your Snowflake worksheet to create the required tables.
-- These match the production notebook (dcc_terminal_profit_tracking_v3).
--
-- External source tables (read-only, owned by other teams):
--   PROD_PRESENTATION.CORTEX.CORTEX_TERMINAL_MAINTENANCE
--   PROD_CORE.TRANSFORMATION.TRN_DWH_DCC_REVENUE
--
-- >>> EDIT THIS to match your environment <<<
-- USE SCHEMA DEV_CORE_AAB.PUBLIC;
-- ============================================================================

-- Flag reference table (metadata about each health check flag)
CREATE TABLE IF NOT EXISTS DCC_V3_FLAG_REFERENCE (
    FLAG_COLUMN     VARCHAR(60) PRIMARY KEY,
    FLAG_NAME       VARCHAR(100),
    FLAG_TYPE       VARCHAR(30),
    FIX_CATEGORY    VARCHAR(30)
);

-- Seed reference data
MERGE INTO DCC_V3_FLAG_REFERENCE tgt
USING (SELECT * FROM VALUES
    ('LOCATION_DCCENABLED_CHECK_C','Location DCC Enabled','Configuration','CONFIG'),
    ('HANDLER_DCCENABLE_CHECK_O','Handler DCC Enable','Handler','HANDLER'),
    ('HANDLER_DCCENABLECOMPLETION_CHECK_O','Handler DCC Completion','Handler','HANDLER'),
    ('HANDLER_DCCENABLEAUTH_CHECK_O','Handler DCC Auth','Handler','HANDLER'),
    ('HANDLER_DCCENABLENFC_CHECK_O','Handler DCC NFC','Handler','HANDLER'),
    ('HANDLER_DCCENABLENFCSINGLETAP_CHECK_O','Handler DCC NFC Single Tap','Handler','HANDLER'),
    ('DCCXPRESSCO_CHECK_O','DCC Xpress CO','DCC Xpress','HANDLER'),
    ('DCCXPRESSCODT_CHECK_O','DCC Xpress CO Delayed Terminal','DCC Xpress','HANDLER'),
    ('DCCMERCHANT_NO_CHECK_O','DCC Merchant Number','Merchant','NON_HANDLER'),
    ('FIRMWARE_VERSION','Firmware Version','Firmware','NON_HANDLER')
    AS v(FLAG_COLUMN,FLAG_NAME,FLAG_TYPE,FIX_CATEGORY)) src
ON tgt.FLAG_COLUMN = src.FLAG_COLUMN
WHEN NOT MATCHED THEN INSERT (FLAG_COLUMN,FLAG_NAME,FLAG_TYPE,FIX_CATEGORY)
VALUES (src.FLAG_COLUMN,src.FLAG_NAME,src.FLAG_TYPE,src.FIX_CATEGORY);

-- Daily health snapshot (populated by MERGE from CORTEX_TERMINAL_MAINTENANCE)
CREATE TABLE IF NOT EXISTS DCC_V3_HEALTH_DAILY_SNAPSHOT (
    SNAPSHOT_DATE                       DATE NOT NULL,
    TERMINAL_IDENTIFIER                 VARCHAR(100) NOT NULL,
    COUNTRY_NAME                        VARCHAR(100),
    REGION                              VARCHAR(100),
    INDUSTRY_NAME                       VARCHAR(100),
    BANK_MERCHANT_ID                    VARCHAR(50),
    CUSTOMER_NAME                       VARCHAR(250),
    ACQUIRER_NAME                       VARCHAR(250),
    LOCATION_NAME                       VARCHAR(250),
    LOCATION_DCCENABLED_CHECK_C         INTEGER,
    HANDLER_DCCENABLE_CHECK_O           INTEGER,
    HANDLER_DCCENABLECOMPLETION_CHECK_O INTEGER,
    HANDLER_DCCENABLEAUTH_CHECK_O       INTEGER,
    HANDLER_DCCENABLENFC_CHECK_O        INTEGER,
    HANDLER_DCCENABLENFCSINGLETAP_CHECK_O INTEGER,
    DCCXPRESSCO_CHECK_O                 INTEGER,
    DCCXPRESSCODT_CHECK_O               INTEGER,
    DCCMERCHANT_NO_CHECK_O              INTEGER,
    FIRMWARE_VERSION                    VARCHAR(200),
    IS_DCC_BROKEN                       BOOLEAN NOT NULL,
    PRIMARY KEY (SNAPSHOT_DATE, TERMINAL_IDENTIFIER)
);

-- Fix episodes (broken→healthy transitions including firmware changes)
CREATE TABLE IF NOT EXISTS DCC_V3_FIX_EPISODES (
    TERMINAL_IDENTIFIER     VARCHAR(100) NOT NULL,
    FLAG_COLUMN             VARCHAR(60) NOT NULL,
    FIX_DATE                DATE NOT NULL,
    FIX_SOURCE              VARCHAR(20) NOT NULL,       -- CAMPAIGN or SNAPSHOT
    CAMPAIGN_ID             VARCHAR(20),
    CAMPAIGN_NAME           VARCHAR(300),
    FLAG_NAME               VARCHAR(100),
    BANK_MERCHANT_ID        VARCHAR(50),
    CUSTOMER_NAME           VARCHAR(250),
    COUNTRY_NAME            VARCHAR(100),
    REGION                  VARCHAR(100),
    INDUSTRY_NAME           VARCHAR(100),
    ACQUIRER_NAME           VARCHAR(250),
    LOCATION_NAME           VARCHAR(500),
    TERMINAL_MODEL_NAME     VARCHAR(200),
    FIRMWARE_VERSION        VARCHAR(200),
    PREV_FIRMWARE_VERSION   VARCHAR(200),
    BREAK_AGAIN_DATE        DATE,
    IS_OPEN                 INTEGER NOT NULL,
    EPISODE_END_DATE        DATE,
    EPISODE_DAYS            INTEGER,
    PRIMARY KEY (TERMINAL_IDENTIFIER, FLAG_COLUMN, FIX_DATE)
);

-- Fix episodes with profit allocation (joined with TRN_DWH_DCC_REVENUE)
CREATE TABLE IF NOT EXISTS DCC_V3_FIX_EPISODE_PROFIT (
    TERMINAL_IDENTIFIER     VARCHAR(100) NOT NULL,
    FLAG_COLUMN             VARCHAR(60) NOT NULL,
    FIX_DATE                DATE NOT NULL,
    FIX_SOURCE              VARCHAR(20),
    CAMPAIGN_ID             VARCHAR(20),
    CAMPAIGN_NAME           VARCHAR(300),
    FLAG_NAME               VARCHAR(100),
    BANK_MERCHANT_ID        VARCHAR(50),
    CUSTOMER_NAME           VARCHAR(250),
    COUNTRY_NAME            VARCHAR(100),
    REGION                  VARCHAR(100),
    INDUSTRY_NAME           VARCHAR(100),
    ACQUIRER_NAME           VARCHAR(250),
    LOCATION_NAME           VARCHAR(500),
    TERMINAL_MODEL_NAME     VARCHAR(200),
    FIRMWARE_VERSION        VARCHAR(200),
    MARKET_GROUP            VARCHAR(100),
    BRAND                   VARCHAR(100),
    BREAK_AGAIN_DATE        DATE,
    IS_OPEN                 INTEGER,
    EPISODE_END_DATE        DATE,
    EPISODE_DAYS            INTEGER,
    ALLOCATED_DCC_PROFIT    FLOAT,
    PRIMARY KEY (TERMINAL_IDENTIFIER, FLAG_COLUMN, FIX_DATE)
);

-- Daily tracker (aggregate fix counts and profit per day)
CREATE TABLE IF NOT EXISTS DCC_V3_FIX_DAILY_TRACKER (
    TRACK_DATE                      DATE NOT NULL PRIMARY KEY,
    DAY_LABEL                       VARCHAR(30),
    DAY_NUMBER                      INTEGER,
    TERMINALS_FIXED                 INTEGER,
    PROFIT_RECOVERED                FLOAT,
    FLAG_FIX_EVENTS                 INTEGER,
    DISTINCT_FLAGS_FIXED            INTEGER,
    FLAGS_FIXED_LIST                VARCHAR(2000),
    FIXED_TERMINAL_LIST             VARCHAR(16777216),
    CUMULATIVE_TERMINALS_FIXED      INTEGER,
    CUMULATIVE_PROFIT_RECOVERED     FLOAT
);

-- Single-row summary (refreshed each pipeline run)
CREATE TABLE IF NOT EXISTS DCC_V3_FIX_PROFIT_SUMMARY (
    TOTAL_FIX_EPISODES      INTEGER,
    UNIQUE_TERMINALS_FIXED  INTEGER,
    HISTORICAL_EPISODES     INTEGER,        -- from CAMPAIGN source
    DETECTED_EPISODES       INTEGER,        -- from SNAPSHOT source
    TOTAL_ALLOCATED_PROFIT  FLOAT,
    OPEN_EPISODES           INTEGER,
    CLOSED_EPISODES         INTEGER,
    AVG_EPISODE_DAYS        FLOAT,
    ACTIVE_CAMPAIGNS        INTEGER,
    BASELINE_DATE           DATE,
    REFRESHED_AT            TIMESTAMP_LTZ
);
