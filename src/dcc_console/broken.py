"""Broken terminal detection from the connected SQL Server database.

Queries handler flags (from instance package_config XML), location DCC settings
(from location extra_function XML), and terminal config download version to
identify terminals with misconfigured DCC settings.

All queries use the same pyodbc connection the console already has open.
"""

from __future__ import annotations

import pandas as pd

from .database import DatabaseConnection

# Maximum rows returned by the broken-terminal query.
MAX_BROKEN_ROWS = 2000


def find_broken_terminals(
    connection: DatabaseConnection,
    only_active: bool = True,
    limit: int = MAX_BROKEN_ROWS,
) -> pd.DataFrame:
    """Return terminals with at least one broken DCC handler flag.

    Joins emv_terminal → instance → location and parses the instance
    package_config XML for each known handler flag.  A flag is considered
    broken if its XML node value is 'false', '0', missing, or the XML
    itself is NULL.
    """
    sql = """
    SELECT TOP (?)
        t.terminal_identifier,
        t.emv_terminal_id,
        t.terminal_model,
        t.firmware_version,
        t.configdownload_version,
        t.is_online,
        l.location_no,
        l.location_name,
        i.instance_identifier,
        i.instance_name,
        -- Handler flags from package_config XML
        CASE WHEN i.package_config IS NULL THEN NULL
             ELSE i.package_config.value(
                 '(//dccEnable)[1]', 'varchar(10)')
        END AS handler_dccEnable,
        CASE WHEN i.package_config IS NULL THEN NULL
             ELSE i.package_config.value(
                 '(//dccEnableAuth)[1]', 'varchar(10)')
        END AS handler_dccEnableAuth,
        CASE WHEN i.package_config IS NULL THEN NULL
             ELSE i.package_config.value(
                 '(//dccEnableCompletion)[1]', 'varchar(10)')
        END AS handler_dccEnableCompletion,
        CASE WHEN i.package_config IS NULL THEN NULL
             ELSE i.package_config.value(
                 '(//dccEnableNfc)[1]', 'varchar(10)')
        END AS handler_dccEnableNfc,
        CASE WHEN i.package_config IS NULL THEN NULL
             ELSE i.package_config.value(
                 '(//dccEnableNfcSingleTap)[1]', 'varchar(10)')
        END AS handler_dccEnableNfcSingleTap,
        -- Location DCC Xpress CO
        CASE WHEN l.extra_function IS NULL THEN NULL
             WHEN CONVERT(varchar(max), l.extra_function)
                  LIKE '%DCCXpressCO%' THEN 'true'
             ELSE 'false'
        END AS location_DCCXpressCO,
        -- DCC enabled at location level
        l.is_dcc AS location_dcc_enabled
    FROM [cccintegrang].[emv_terminal] AS t
    LEFT JOIN [cccintegrang].[instance] AS i
        ON i.instance_id = t.instance_id
    LEFT JOIN [ccc].[location] AS l
        ON l.location_id = t.location_id
    WHERE t.is_deleted = 0
      AND t.terminal_identifier IS NOT NULL
      AND (? = 0 OR (t.is_online = 1 AND ISNULL(l.is_dcc, 0) = 1))
    ORDER BY t.terminal_identifier
    """
    return connection.query(sql, (int(limit), 1 if only_active else 0))


# The flags we check and their expected "healthy" values.
HANDLER_FLAGS = (
    ("handler_dccEnable", "dccEnable"),
    ("handler_dccEnableAuth", "dccEnableAuth"),
    ("handler_dccEnableCompletion", "dccEnableCompletion"),
    ("handler_dccEnableNfc", "dccEnableNfc"),
    ("handler_dccEnableNfcSingleTap", "dccEnableNfcSingleTap"),
    ("location_DCCXpressCO", "DCCXpressCO"),
)


def _is_flag_broken(value) -> bool:
    """A flag is broken if it's false, 0, empty, or NULL."""
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in ("false", "0", "", "null")


def compute_broken_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Add per-flag broken columns and a total broken count."""
    if df.empty:
        return df
    out = df.copy()
    broken_cols = []
    for col, label in HANDLER_FLAGS:
        broken_col = f"{label}_broken"
        if col in out.columns:
            out[broken_col] = out[col].apply(_is_flag_broken)
            broken_cols.append(broken_col)
    out["broken_flag_count"] = out[broken_cols].sum(axis=1).astype(int)
    return out


def broken_summary(df: pd.DataFrame) -> dict:
    """Compute summary metrics from the broken-flags DataFrame."""
    if df.empty:
        return {
            "total_terminals": 0,
            "broken_terminals": 0,
            "healthy_terminals": 0,
            "by_flag": {},
        }
    broken = df[df["broken_flag_count"] > 0]
    by_flag = {}
    for _, label in HANDLER_FLAGS:
        col = f"{label}_broken"
        if col in df.columns:
            by_flag[label] = int(df[col].sum())
    return {
        "total_terminals": len(df),
        "broken_terminals": len(broken),
        "healthy_terminals": len(df) - len(broken),
        "by_flag": by_flag,
    }
