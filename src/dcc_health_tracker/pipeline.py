"""SQL generators for the transition detection and episode-building pipeline.

Every function returns a SQL string that uses the table names from config.py.
The caller decides when and how to execute (Snowflake worksheet, Streamlit
connection, or a task scheduler). Nothing is executed at import time.

Pipeline stages:
    1. unpivot_snapshot_cte  — wide snapshot → narrow (terminal, flag, value) rows
    2. detect_transitions    — LAG over consecutive days → FIXED / BROKEN events
    3. build_fix_episodes    — FIXED transitions with episode end and duration
    4. build_break_episodes  — BROKEN transitions with episode end and duration
    5. refresh_daily_tracker — per-day aggregate counts
    6. refresh_summary       — single-row KPI summary
"""

from __future__ import annotations

from . import config as cfg


def _flag_list() -> str:
    return ",\n        ".join(cfg.FLAG_COLUMNS)


def unpivot_cte() -> str:
    return f"""unpivoted AS (
    SELECT
        s.snapshot_date,
        s.terminal_identifier,
        s.country_name,
        s.region,
        s.industry_name,
        s.bank_merchant_id,
        s.customer_name,
        s.acquirer_name,
        s.location_name,
        s.firmware_version,
        s.is_dcc_broken,
        t.flag_column,
        t.flag_value
    FROM {cfg.SNAPSHOT_TABLE} AS s,
    LATERAL FLATTEN(ARRAY_CONSTRUCT(
        {', '.join(f"OBJECT_CONSTRUCT('{col}', s.{col})" for col in cfg.FLAG_COLUMNS)}
    )) AS flat,
    LATERAL (
        SELECT key AS flag_column, value::INT AS flag_value
        FROM TABLE(FLATTEN(flat.value))
    ) AS t
)"""


def unpivot_cte_simple() -> str:
    """UNPIVOT-based CTE — cleaner and supported natively in Snowflake."""
    return f"""unpivoted AS (
    SELECT
        snapshot_date,
        terminal_identifier,
        country_name,
        region,
        industry_name,
        bank_merchant_id,
        customer_name,
        acquirer_name,
        location_name,
        firmware_version,
        is_dcc_broken,
        flag_column,
        flag_value
    FROM {cfg.SNAPSHOT_TABLE}
    UNPIVOT (
        flag_value FOR flag_column IN (
            {_flag_list()}
        )
    )
)"""


def transitions_cte() -> str:
    return """transitions AS (
    SELECT
        snapshot_date       AS transition_date,
        terminal_identifier,
        flag_column,
        flag_value          AS current_value,
        LAG(flag_value) OVER (
            PARTITION BY terminal_identifier, flag_column
            ORDER BY snapshot_date
        ) AS prev_value,
        LAG(snapshot_date) OVER (
            PARTITION BY terminal_identifier, flag_column
            ORDER BY snapshot_date
        ) AS prev_snapshot_date,
        CASE
            WHEN LAG(flag_value) OVER (
                     PARTITION BY terminal_identifier, flag_column
                     ORDER BY snapshot_date) = 1
                 AND flag_value = 0 THEN 'FIXED'
            WHEN LAG(flag_value) OVER (
                     PARTITION BY terminal_identifier, flag_column
                     ORDER BY snapshot_date) = 0
                 AND flag_value = 1 THEN 'BROKEN'
        END AS transition_type,
        country_name, region, industry_name, bank_merchant_id,
        customer_name, acquirer_name, location_name, firmware_version
    FROM unpivoted
)"""


def create_transition_log() -> str:
    return f"""CREATE OR REPLACE TABLE {cfg.TRANSITION_LOG_TABLE} AS
WITH
{unpivot_cte_simple()},
{transitions_cte()}
SELECT
    transition_date,
    terminal_identifier,
    flag_column,
    f.flag_name,
    f.flag_type,
    transition_type,
    current_value,
    prev_value,
    prev_snapshot_date,
    t.country_name,
    t.region,
    t.industry_name,
    t.bank_merchant_id,
    t.customer_name,
    t.acquirer_name,
    t.location_name,
    t.firmware_version
FROM transitions t
LEFT JOIN {cfg.FLAG_REF_TABLE} f ON f.flag_column = t.flag_column
WHERE transition_type IS NOT NULL
ORDER BY transition_date, terminal_identifier, flag_column;"""


def create_fix_episodes() -> str:
    return f"""CREATE OR REPLACE TABLE {cfg.FIX_EPISODE_TABLE} AS
WITH fixes AS (
    SELECT * FROM {cfg.TRANSITION_LOG_TABLE} WHERE transition_type = 'FIXED'
),
next_break AS (
    SELECT
        f.*,
        MIN(b.transition_date) AS break_again_date
    FROM fixes f
    LEFT JOIN {cfg.TRANSITION_LOG_TABLE} b
        ON  b.terminal_identifier = f.terminal_identifier
        AND b.flag_column         = f.flag_column
        AND b.transition_type     = 'BROKEN'
        AND b.transition_date     > f.transition_date
    GROUP BY ALL
)
SELECT
    terminal_identifier,
    flag_column,
    flag_name,
    transition_date                                             AS fix_date,
    'SNAPSHOT'                                                  AS fix_source,
    break_again_date,
    IFF(break_again_date IS NULL, 1, 0)                        AS is_open,
    COALESCE(break_again_date, CURRENT_DATE())                 AS episode_end_date,
    DATEDIFF('day', transition_date,
             COALESCE(break_again_date, CURRENT_DATE()))        AS episode_days,
    country_name, region, industry_name, bank_merchant_id,
    customer_name, acquirer_name, location_name, firmware_version
FROM next_break
ORDER BY fix_date, terminal_identifier, flag_column;"""


def create_break_episodes() -> str:
    return f"""CREATE OR REPLACE TABLE {cfg.BREAK_EPISODE_TABLE} AS
WITH breaks AS (
    SELECT * FROM {cfg.TRANSITION_LOG_TABLE} WHERE transition_type = 'BROKEN'
),
next_fix AS (
    SELECT
        b.*,
        MIN(f.transition_date) AS fixed_again_date
    FROM breaks b
    LEFT JOIN {cfg.TRANSITION_LOG_TABLE} f
        ON  f.terminal_identifier = b.terminal_identifier
        AND f.flag_column         = b.flag_column
        AND f.transition_type     = 'FIXED'
        AND f.transition_date     > b.transition_date
    GROUP BY ALL
)
SELECT
    terminal_identifier,
    flag_column,
    flag_name,
    transition_date                                             AS break_date,
    fixed_again_date,
    IFF(fixed_again_date IS NULL, 1, 0)                        AS is_open,
    COALESCE(fixed_again_date, CURRENT_DATE())                 AS episode_end_date,
    DATEDIFF('day', transition_date,
             COALESCE(fixed_again_date, CURRENT_DATE()))        AS episode_days,
    country_name, region, industry_name, bank_merchant_id,
    customer_name, acquirer_name, location_name, firmware_version
FROM next_fix
ORDER BY break_date, terminal_identifier, flag_column;"""


def refresh_daily_tracker() -> str:
    return f"""CREATE OR REPLACE TABLE {cfg.DAILY_TRACKER_TABLE} AS
WITH fix_daily AS (
    SELECT
        fix_date                                            AS track_date,
        COUNT(DISTINCT terminal_identifier)                 AS terminals_fixed,
        COUNT(*)                                            AS flag_fix_events,
        COUNT(DISTINCT flag_column)                         AS distinct_flags_fixed,
        LISTAGG(DISTINCT flag_name, ', ') WITHIN GROUP
            (ORDER BY flag_name)                            AS flags_fixed_list
    FROM {cfg.FIX_EPISODE_TABLE}
    GROUP BY fix_date
),
break_daily AS (
    SELECT
        break_date                                          AS track_date,
        COUNT(DISTINCT terminal_identifier)                 AS terminals_broken,
        COUNT(*)                                            AS flag_break_events,
        COUNT(DISTINCT flag_column)                         AS distinct_flags_broken,
        LISTAGG(DISTINCT flag_name, ', ') WITHIN GROUP
            (ORDER BY flag_name)                            AS flags_broken_list
    FROM {cfg.BREAK_EPISODE_TABLE}
    GROUP BY break_date
)
SELECT
    COALESCE(f.track_date, b.track_date)                    AS track_date,
    TO_CHAR(COALESCE(f.track_date, b.track_date), 'YYYY-MM-DD (DY)') AS day_label,
    COALESCE(f.terminals_fixed,   0)                        AS terminals_fixed,
    COALESCE(f.flag_fix_events,   0)                        AS flag_fix_events,
    COALESCE(f.flags_fixed_list,  '')                       AS flags_fixed_list,
    COALESCE(b.terminals_broken,  0)                        AS terminals_broken,
    COALESCE(b.flag_break_events, 0)                        AS flag_break_events,
    COALESCE(b.flags_broken_list, '')                       AS flags_broken_list,
    SUM(COALESCE(f.terminals_fixed, 0)) OVER
        (ORDER BY COALESCE(f.track_date, b.track_date))     AS cum_terminals_fixed,
    SUM(COALESCE(b.terminals_broken, 0)) OVER
        (ORDER BY COALESCE(f.track_date, b.track_date))     AS cum_terminals_broken
FROM fix_daily f
FULL OUTER JOIN break_daily b ON f.track_date = b.track_date
ORDER BY track_date;"""


def refresh_summary() -> str:
    return f"""CREATE OR REPLACE TABLE {cfg.SUMMARY_TABLE} AS
SELECT
    (SELECT COUNT(*)
     FROM {cfg.FIX_EPISODE_TABLE})                          AS total_fix_episodes,
    (SELECT COUNT(DISTINCT terminal_identifier)
     FROM {cfg.FIX_EPISODE_TABLE})                          AS unique_terminals_fixed,
    (SELECT SUM(is_open)
     FROM {cfg.FIX_EPISODE_TABLE})                          AS open_fix_episodes,
    (SELECT AVG(episode_days)
     FROM {cfg.FIX_EPISODE_TABLE})                          AS avg_fix_episode_days,
    (SELECT COUNT(*)
     FROM {cfg.BREAK_EPISODE_TABLE})                        AS total_break_episodes,
    (SELECT COUNT(DISTINCT terminal_identifier)
     FROM {cfg.BREAK_EPISODE_TABLE})                        AS unique_terminals_broken,
    (SELECT SUM(is_open)
     FROM {cfg.BREAK_EPISODE_TABLE})                        AS open_break_episodes,
    (SELECT AVG(episode_days)
     FROM {cfg.BREAK_EPISODE_TABLE})                        AS avg_break_episode_days,
    (SELECT MIN(snapshot_date)
     FROM {cfg.SNAPSHOT_TABLE})                              AS earliest_snapshot,
    (SELECT MAX(snapshot_date)
     FROM {cfg.SNAPSHOT_TABLE})                              AS latest_snapshot,
    CURRENT_TIMESTAMP()                                      AS refreshed_at;"""


def full_refresh_script() -> str:
    """Return the complete pipeline as a single multi-statement script."""
    steps = [
        "-- Step 1: detect all day-over-day transitions (FIXED + BROKEN)",
        create_transition_log(),
        "",
        "-- Step 2: build fix episodes (broken → healthy, with optional re-break)",
        create_fix_episodes(),
        "",
        "-- Step 3: build break episodes (healthy → broken, with optional re-fix)",
        create_break_episodes(),
        "",
        "-- Step 4: daily tracker (fixes and breaks per day)",
        refresh_daily_tracker(),
        "",
        "-- Step 5: single-row summary",
        refresh_summary(),
    ]
    return "\n\n".join(steps)
