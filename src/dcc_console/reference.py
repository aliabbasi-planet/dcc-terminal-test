"""Reference-data queries for target pickers."""

from __future__ import annotations

import pandas as pd

from .config import configdownload_version_text
from .database import DatabaseConnection

MAX_REFERENCE_ROWS = 500


def load_instances(connection: DatabaseConnection) -> pd.DataFrame:
    return connection.query(
        """
        SELECT TOP (?)
            instance_identifier,
            instance_name,
            is_online,
            is_locked
        FROM [cccintegrang].[instance]
        WHERE is_deleted = 0
          AND instance_identifier IS NOT NULL
        ORDER BY instance_identifier
        """,
        (MAX_REFERENCE_ROWS,),
    )


def load_locations(connection: DatabaseConnection) -> pd.DataFrame:
    return connection.query(
        """
        SELECT TOP (?)
            location_no,
            location_name,
            is_dcc,
            is_online
        FROM [ccc].[location]
        WHERE is_deleted = 0
          AND location_no IS NOT NULL
          AND LTRIM(RTRIM(location_no)) <> ''
        ORDER BY location_no
        """,
        (MAX_REFERENCE_ROWS,),
    )


def load_terminals(
    connection: DatabaseConnection,
    only_online: bool,
    only_unlocked: bool,
    limit: int,
) -> pd.DataFrame:
    frame = connection.query(
        """
        SELECT TOP (?)
            t.terminal_identifier,
            t.emv_terminal_id,
            t.terminal_model,
            t.configdownload_version,
            t.firmware_version,
            t.software_version,
            t.is_online,
            t.is_locked,
            l.location_no,
            l.location_name
        FROM [cccintegrang].[emv_terminal] AS t
        LEFT JOIN [ccc].[location] AS l
            ON l.location_id = t.location_id
        WHERE t.is_deleted = 0
          AND t.terminal_identifier IS NOT NULL
          AND LTRIM(RTRIM(t.terminal_identifier)) <> ''
          AND (? = 0 OR t.is_online = 1)
          AND (? = 0 OR t.is_locked = 0)
        ORDER BY t.terminal_identifier
        """,
        (int(limit), 1 if only_online else 0, 1 if only_unlocked else 0),
    )
    # Label the numeric configdownload_version using the single source of truth
    # in config.py (1 = Standard, 2 = ECB DCC) so the picker never shows a bare,
    # ambiguous code.
    if "configdownload_version" in frame.columns:
        frame.insert(
            frame.columns.get_loc("configdownload_version") + 1,
            "configdownload_version_desc",
            frame["configdownload_version"].map(configdownload_version_text),
        )
    return frame
