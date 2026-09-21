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

# Sentinel column name for the stored-procedure return code. The harness wraps
# every call as ``EXEC @rc = proc ...; SELECT @rc AS dcc_return_code`` so it can
# report whether the procedure signals via an integer return code. The database
# layer recognises this column, captures the value, and does NOT treat it as a
# preview result set.
RETURN_CODE_COLUMN = "dcc_return_code"

# Bit 2 — configdownload_version numeric code -> human description.
# Confirmed mapping: 1 = Standard (baseline DCC), 2 = ECB DCC (European Central
# Bank conversion-rate variant). These are the only two versions the procedure
# supports; any other stored code is surfaced verbatim so it is not silently
# mislabelled.
CONFIGDOWNLOAD_VERSIONS: dict[int, str] = {
    1: "Standard",
    2: "ECB DCC",
}


def configdownload_version_text(code: object) -> str:
    """Render a configdownload_version code as ``"<code> (<name>)"``.

    Unknown or null codes are labelled explicitly rather than guessed.
    """
    if code is None or (isinstance(code, str) and not code.strip()):
        return "(none)"
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        return f"{code} (unrecognised)"
    name = CONFIGDOWNLOAD_VERSIONS.get(numeric)
    return f"{numeric} ({name})" if name else f"{numeric} (unknown version)"


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
