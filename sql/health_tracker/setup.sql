-- ============================================================================
-- DCC Terminal Health Tracker — setup DDL
-- ============================================================================
-- Run this once in your Snowflake worksheet to create the pipeline output
-- tables.  Replace the schema prefix below with your own before executing.
--
-- The pipeline (src/dcc_health_tracker/pipeline.py) populates these tables
-- from the input snapshot table using day-over-day transition detection.
-- ============================================================================

-- >>> EDIT THIS to match your environment <<<
-- USE SCHEMA DEV_CORE_AAB.PUBLIC;

-- Step 1: Transition log — every day-over-day flag change (FIXED or BROKEN)
CREATE TABLE IF NOT EXISTS DCC_TRANSITION_LOG (
    transition_date     DATE            NOT NULL,
    terminal_identifier VARCHAR(100)    NOT NULL,
    flag_column         VARCHAR(60)     NOT NULL,
    flag_name           VARCHAR(100),
    flag_type           VARCHAR(30),
    transition_type     VARCHAR(10)     NOT NULL,  -- FIXED or BROKEN
    current_value       INT,
    prev_value          INT,
    prev_snapshot_date  DATE,
    country_name        VARCHAR(100),
    region              VARCHAR(100),
    industry_name       VARCHAR(100),
    bank_merchant_id    VARCHAR(50),
    customer_name       VARCHAR(250),
    acquirer_name       VARCHAR(250),
    location_name       VARCHAR(500),
    firmware_version    VARCHAR(200),
    PRIMARY KEY (transition_date, terminal_identifier, flag_column)
);

-- Step 2: Fix episodes — broken-to-healthy transitions with duration
CREATE TABLE IF NOT EXISTS DCC_FIX_EPISODE (
    terminal_identifier VARCHAR(100)    NOT NULL,
    flag_column         VARCHAR(60)     NOT NULL,
    flag_name           VARCHAR(100),
    fix_date            DATE            NOT NULL,
    fix_source          VARCHAR(20)     NOT NULL DEFAULT 'SNAPSHOT',
    break_again_date    DATE,
    is_open             INT             NOT NULL,
    episode_end_date    DATE,
    episode_days        INT,
    country_name        VARCHAR(100),
    region              VARCHAR(100),
    industry_name       VARCHAR(100),
    bank_merchant_id    VARCHAR(50),
    customer_name       VARCHAR(250),
    acquirer_name       VARCHAR(250),
    location_name       VARCHAR(500),
    firmware_version    VARCHAR(200),
    PRIMARY KEY (terminal_identifier, flag_column, fix_date)
);

-- Step 3: Break episodes — healthy-to-broken transitions with duration
CREATE TABLE IF NOT EXISTS DCC_BREAK_EPISODE (
    terminal_identifier VARCHAR(100)    NOT NULL,
    flag_column         VARCHAR(60)     NOT NULL,
    flag_name           VARCHAR(100),
    break_date          DATE            NOT NULL,
    fixed_again_date    DATE,
    is_open             INT             NOT NULL,
    episode_end_date    DATE,
    episode_days        INT,
    country_name        VARCHAR(100),
    region              VARCHAR(100),
    industry_name       VARCHAR(100),
    bank_merchant_id    VARCHAR(50),
    customer_name       VARCHAR(250),
    acquirer_name       VARCHAR(250),
    location_name       VARCHAR(500),
    firmware_version    VARCHAR(200),
    PRIMARY KEY (terminal_identifier, flag_column, break_date)
);

-- Step 4: Daily tracker — aggregate fix/break counts per day
CREATE TABLE IF NOT EXISTS DCC_DAILY_TRACKER (
    track_date              DATE        NOT NULL PRIMARY KEY,
    day_label               VARCHAR(30),
    terminals_fixed         INT,
    flag_fix_events         INT,
    flags_fixed_list        VARCHAR(2000),
    terminals_broken        INT,
    flag_break_events       INT,
    flags_broken_list       VARCHAR(2000),
    cum_terminals_fixed     INT,
    cum_terminals_broken    INT
);

-- Step 5: Single-row summary
CREATE TABLE IF NOT EXISTS DCC_HEALTH_SUMMARY (
    total_fix_episodes      INT,
    unique_terminals_fixed  INT,
    open_fix_episodes       INT,
    avg_fix_episode_days    FLOAT,
    total_break_episodes    INT,
    unique_terminals_broken INT,
    open_break_episodes     INT,
    avg_break_episode_days  FLOAT,
    earliest_snapshot       DATE,
    latest_snapshot         DATE,
    refreshed_at            TIMESTAMP_LTZ
);
