"""Snowflake connection for the remediation tab (SSO / external browser).

Kept deliberately parallel to :class:`dcc_console.database.DatabaseConnection`:
``connect() -> (ok, message)`` then ``query`` / ``execute`` / ``scalar``. Reads
are returned as DataFrames built from the cursor (no pyarrow dependency).

The ``snowflake.connector`` import is soft so the rest of the console still loads
if the connector has not been installed yet; :func:`connector_available` reports
this and :meth:`SnowflakeConnection.connect` fails with a clear message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

try:  # soft import — see module docstring
    import snowflake.connector as _sf

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depends on environment
    _sf = None
    _IMPORT_ERROR = exc


def connector_available() -> bool:
    return _sf is not None


@dataclass
class SnowflakeSettings:
    """Non-secret connection settings supplied by the operator at connect time."""

    account: str
    user: str
    role: str
    warehouse: str
    database: str = "DEV_CORE_AAB"
    schema: str = "DCC_REMEDIATION"
    authenticator: str = "externalbrowser"

    def normalised(self) -> SnowflakeSettings:
        return SnowflakeSettings(
            account=self.account.strip(),
            user=self.user.strip(),
            role=self.role.strip(),
            warehouse=self.warehouse.strip(),
            database=self.database.strip(),
            schema=self.schema.strip(),
            authenticator=self.authenticator.strip() or "externalbrowser",
        )


class SnowflakeConnection:
    """Thin Snowflake wrapper mirroring the SQL-Server connection's surface."""

    def __init__(self, settings: SnowflakeSettings) -> None:
        self.settings = settings.normalised()
        self.connection = None

    def connect(self) -> tuple[bool, str]:
        if _sf is None:
            return False, (
                "The `snowflake-connector-python` package is not installed in this "
                f"environment. Install it and reload. (import error: {_IMPORT_ERROR})"
            )
        s = self.settings
        required = {"account": s.account, "user": s.user, "role": s.role, "warehouse": s.warehouse}
        blank = [name for name, value in required.items() if not value]
        if blank:
            return False, f"Enter {', '.join(blank)} before connecting."
        try:
            self.connection = _sf.connect(
                account=s.account,
                user=s.user,
                role=s.role,
                warehouse=s.warehouse,
                database=s.database,
                schema=s.schema,
                authenticator=s.authenticator,
                client_session_keep_alive=True,
            )
            who = self.scalar(
                "SELECT CURRENT_USER() || ' / ' || CURRENT_ROLE() "
                "|| ' / ' || CURRENT_WAREHOUSE()"
            )
            return True, f"Connected as {who}"
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug("Snowflake connect failed: %s", exc)
            self.connection = None
            return False, f"Could not connect to Snowflake account `{s.account}`.\n\n{exc}"

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    def _require(self):
        if self.connection is None:
            raise RuntimeError("Not connected to Snowflake.")
        return self.connection

    def query(self, sql: str, params: tuple | list | None = None) -> pd.DataFrame:
        cursor = self._require().cursor()
        try:
            cursor.execute(sql, params)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = [tuple(row) for row in cursor.fetchall()]
            return pd.DataFrame(rows, columns=columns)
        finally:
            cursor.close()

    def scalar(self, sql: str, params: tuple | list | None = None):
        cursor = self._require().cursor()
        try:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()

    def execute(self, sql: str, params: tuple | list | None = None) -> int:
        """Run one write and commit it; roll back on failure."""
        connection = self._require()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            affected = cursor.rowcount or 0
            cursor.close()
            connection.commit()
            return affected
        except Exception:
            try:
                cursor.close()
            except Exception as close_error:
                logger.debug("Cursor close suppressed: %s", close_error)
            connection.rollback()
            raise
