"""Broken terminal detection from the connected SQL Server database.

Queries handler flags (from instance package_config XML) and location DCC
settings (from location extra_function XML) to identify misconfigured DCC
entries.

Strategy: query instances and locations using the same table/column patterns
proven in catalog.py and reference.py, convert XML to strings, and parse the
flag values in Python.  This avoids XQuery syntax issues and unknown join
column names.
"""

from __future__ import annotations

import logging
import re

import pandas as pd

from .database import DatabaseConnection

logger = logging.getLogger(__name__)

MAX_ROWS = 2000


def _extract_xml_flag(xml_str: str | None, flag_name: str) -> str | None:
    """Extract a flag value from an XML string using regex."""
    if xml_str is None:
        return None
    pattern = rf"<{flag_name}[^>]*>(.*?)</{flag_name}>"
    match = re.search(pattern, str(xml_str), re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def find_broken_instances(
    connection: DatabaseConnection,
    limit: int = MAX_ROWS,
) -> pd.DataFrame:
    """Return instances with their package_config as a string for flag parsing."""
    sql = """
    SELECT TOP (?)
        i.instance_identifier,
        i.instance_name,
        i.is_online,
        CONVERT(nvarchar(max), i.package_config) AS package_config_text
    FROM [cccintegrang].[instance] AS i
    WHERE i.is_deleted = 0
      AND i.instance_identifier IS NOT NULL
    ORDER BY i.instance_identifier
    """
    df = connection.query(sql, (int(limit),))
    if df.empty:
        return df

    for flag in INSTANCE_FLAGS:
        df[f"handler_{flag}"] = df["package_config_text"].apply(
            lambda xml, f=flag: _extract_xml_flag(xml, f)
        )
    return df


def find_broken_locations(
    connection: DatabaseConnection,
    limit: int = MAX_ROWS,
) -> pd.DataFrame:
    """Return DCC-enabled locations with extra_function check."""
    sql = """
    SELECT TOP (?)
        l.location_no,
        l.location_name,
        l.is_dcc,
        CONVERT(nvarchar(max), l.extra_function) AS extra_function_text
    FROM [ccc].[location] AS l
    WHERE l.is_deleted = 0
      AND l.location_no IS NOT NULL
      AND ISNULL(l.is_dcc, 0) = 1
    ORDER BY l.location_no
    """
    df = connection.query(sql, (int(limit),))
    if df.empty:
        return df

    df["location_DCCXpressCO"] = df["extra_function_text"].apply(
        lambda xml: "true" if xml and "DCCXpressCO" in str(xml) else "false"
    )
    return df


def find_broken_terminals(
    connection: DatabaseConnection,
    only_active: bool = True,
    limit: int = MAX_ROWS,
) -> pd.DataFrame:
    """Return terminals joined with instance flags and location DCC status.

    Uses the same table patterns as reference.py (load_terminals) and
    catalog.py (verified columns).
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
        l.is_dcc,
        CONVERT(nvarchar(max), l.extra_function) AS extra_function_text
    FROM [cccintegrang].[emv_terminal] AS t
    LEFT JOIN [ccc].[location] AS l
        ON l.location_id = t.location_id
    WHERE t.is_deleted = 0
      AND t.terminal_identifier IS NOT NULL
      AND (? = 0 OR (t.is_online = 1 AND ISNULL(l.is_dcc, 0) = 1))
    ORDER BY t.terminal_identifier
    """
    df = connection.query(sql, (int(limit), 1 if only_active else 0))
    if df.empty:
        return df

    df["location_DCCXpressCO"] = df["extra_function_text"].apply(
        lambda xml: "true" if xml and "DCCXpressCO" in str(xml) else "false"
    )

    # Now enrich with instance handler flags if instances can be loaded
    try:
        instances = find_broken_instances(connection, limit=limit)
        if not instances.empty:
            inst_cols = ["instance_identifier"] + [
                c for c in instances.columns if c.startswith("handler_")
            ]
            df = df.merge(
                instances[inst_cols],
                left_on="terminal_identifier",
                right_on="instance_identifier",
                how="left",
                suffixes=("", "_inst"),
            )
    except Exception as exc:
        # Instance join failed — still return terminal+location data.
        logger.debug("Instance flag enrichment skipped: %s", exc)

    return df


# The flags we check and their expected "healthy" values.
INSTANCE_FLAGS = (
    "dccEnable",
    "dccEnableAuth",
    "dccEnableCompletion",
    "dccEnableNfc",
    "dccEnableNfcSingleTap",
)

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
    if broken_cols:
        out["broken_flag_count"] = out[broken_cols].sum(axis=1).astype(int)
    else:
        out["broken_flag_count"] = 0
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
    has_count = "broken_flag_count" in df.columns
    broken = df[df["broken_flag_count"] > 0] if has_count else df.iloc[0:0]
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
