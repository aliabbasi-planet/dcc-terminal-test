"""Environment and runtime settings.

Only non-secret values live here. Database usernames and passwords are always
supplied through the UI at connect time and are never read from disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

PROCEDURE = "[cccai].[spApplyDCCEnablementConfiguration]"

TRACE_FUNCTION = "[db].[fnDisplayTrace]"


@dataclass(frozen=True)
class Environment:
    """A deployment target the console can connect to."""

    name: str
    server: str
    database: str
    badge: str
    note: str
    requires_typed_confirmation: bool = True


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


ENVIRONMENTS: dict[str, Environment] = {
    "DEV": Environment(
        name="DEV",
        server=_env("DEV_DB_SERVER"),
        database=_env("DEV_DB_NAME", "3CDB"),
        badge="\U0001f7e2",
        note="Development — safe for exploratory and live runs.",
    ),
    "UAT": Environment(
        name="UAT",
        server=_env("UAT_DB_SERVER"),
        database=_env("UAT_DB_NAME", "3CDB"),
        badge="\U0001f7e0",
        note="User acceptance — shared environment, live runs need sign-off.",
    ),
    "PROD": Environment(
        name="PROD",
        server=_env("PROD_DB_SERVER"),
        database=_env("PROD_DB_NAME", "3CDB"),
        badge="\U0001f534",
        note="Production — live runs require explicit confirmation and formal approval.",
    ),
}


CANDIDATE_ODBC_DRIVERS: tuple[str, ...] = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)

CONNECT_TIMEOUT_S = 10
