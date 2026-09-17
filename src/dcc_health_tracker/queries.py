"""Read-only SQL queries used by the Streamlit dashboard.

Every function returns a query string referencing the output tables from config.
None of these write data — they are SELECT-only for the dashboard pages.
"""

from __future__ import annotations

from . import config as cfg


def summary() -> str:
    return f"SELECT * FROM {cfg.SUMMARY_TABLE}"


def fix_episodes() -> str:
    return f"""SELECT
    e.terminal_identifier, e.flag_column, e.flag_name,
    e.fix_date, e.fix_source,
    e.break_again_date, e.is_open, e.episode_end_date, e.episode_days,
    e.bank_merchant_id, e.customer_name, e.country_name, e.region,
    e.industry_name, e.acquirer_name, e.location_name, e.firmware_version
FROM {cfg.FIX_EPISODE_TABLE} e
ORDER BY e.fix_date DESC, e.terminal_identifier"""


def break_episodes() -> str:
    return f"""SELECT
    e.terminal_identifier, e.flag_column, e.flag_name,
    e.break_date, e.fixed_again_date,
    e.is_open, e.episode_end_date, e.episode_days,
    e.bank_merchant_id, e.customer_name, e.country_name, e.region,
    e.industry_name, e.acquirer_name, e.location_name, e.firmware_version
FROM {cfg.BREAK_EPISODE_TABLE} e
ORDER BY e.break_date DESC, e.terminal_identifier"""


def daily_tracker() -> str:
    return f"SELECT * FROM {cfg.DAILY_TRACKER_TABLE} ORDER BY track_date"


def transition_log() -> str:
    return f"""SELECT
    transition_date, terminal_identifier, flag_column, flag_name,
    transition_type, country_name, region, firmware_version
FROM {cfg.TRANSITION_LOG_TABLE}
ORDER BY transition_date DESC, terminal_identifier"""


def fix_by_flag() -> str:
    return f"""SELECT
    flag_name,
    COUNT(DISTINCT terminal_identifier) AS terminals,
    COUNT(*)                            AS episodes,
    AVG(episode_days)                   AS avg_days
FROM {cfg.FIX_EPISODE_TABLE}
GROUP BY flag_name ORDER BY terminals DESC"""


def break_by_flag() -> str:
    return f"""SELECT
    flag_name,
    COUNT(DISTINCT terminal_identifier) AS terminals,
    COUNT(*)                            AS episodes,
    AVG(episode_days)                   AS avg_days
FROM {cfg.BREAK_EPISODE_TABLE}
GROUP BY flag_name ORDER BY terminals DESC"""


def fix_by_country() -> str:
    return f"""SELECT
    country_name,
    COUNT(DISTINCT terminal_identifier) AS terminals,
    COUNT(*) AS episodes
FROM {cfg.FIX_EPISODE_TABLE}
GROUP BY country_name ORDER BY terminals DESC
LIMIT 20"""


def break_by_country() -> str:
    return f"""SELECT
    country_name,
    COUNT(DISTINCT terminal_identifier) AS terminals,
    COUNT(*) AS episodes
FROM {cfg.BREAK_EPISODE_TABLE}
GROUP BY country_name ORDER BY terminals DESC
LIMIT 20"""
