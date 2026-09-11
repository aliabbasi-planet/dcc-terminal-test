"""pyodbc connection management.

Every statement issued through this module is parameterised. Identifiers are
only ever taken from :mod:`dcc_console.catalog`, never from user input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
import pyodbc

from .config import CANDIDATE_ODBC_DRIVERS, CONNECT_TIMEOUT_S

logger = logging.getLogger(__name__)


class ConnectionError_(RuntimeError):
    """Raised when no installed ODBC driver can reach the server."""


@dataclass
class ProcedureOutput:
    """Everything a stored procedure emitted during one call."""

    grids: list[pd.DataFrame]
    messages: list[str]


class DatabaseConnection:
    """Thin pyodbc wrapper with explicit transaction control."""

    def __init__(self, server: str, database: str, username: str, password: str) -> None:
        self.server = server.strip()
        self.database = database.strip()
        self.username = username.strip()
        self._password = password
        self.connection: pyodbc.Connection | None = None
        self.driver: str | None = None

    # -- connection ---------------------------------------------------------

    def _connection_string(self, driver: str, encrypt: bool) -> str:
        parts = [
            f"Driver={{{driver}}}",
            f"Server={self.server}",
            f"Database={self.database}",
            f"UID={self.username}",
            f"PWD={self._password}",
            "Trusted_Connection=no",
        ]
        if encrypt:
            parts += ["Encrypt=yes", "TrustServerCertificate=yes"]
        return ";".join(parts) + ";"

    def connect(self) -> tuple[bool, str]:
        """Try each installed driver, with and without encryption."""
        if not all([self.server, self.database, self.username, self._password]):
            return False, "Enter server, database, username and password before connecting."

        available = set(pyodbc.drivers())
        last_error: Exception | None = None

        for driver in CANDIDATE_ODBC_DRIVERS:
            if driver not in available:
                continue
            for encrypt in (True, False):
                try:
                    connection = pyodbc.connect(
                        self._connection_string(driver, encrypt),
                        timeout=CONNECT_TIMEOUT_S,
                    )
                    connection.autocommit = False
                    self.connection = connection
                    self.driver = driver
                    return True, f"Connected with `{driver}`"
                except Exception as exc:
                    last_error = exc
                    logger.debug("Connect failed: %s encrypt=%s: %s", driver, encrypt, exc)

        return False, (
            f"Could not connect to `{self.server}`.\n\n"
            f"Installed ODBC drivers: {', '.join(sorted(available)) or 'none'}\n\n"
            f"Last driver error: {last_error}"
        )

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    # -- statements ---------------------------------------------------------

    def _require_connection(self) -> pyodbc.Connection:
        if self.connection is None:
            raise ConnectionError_("Not connected to a database.")
        return self.connection

    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        cursor = self._require_connection().cursor()
        try:
            cursor.execute(sql, params)
            columns = [d[0] for d in cursor.description]
            rows = [tuple(row) for row in cursor.fetchall()]
            return pd.DataFrame(rows, columns=columns)
        finally:
            cursor.close()

    def scalar(self, sql: str, params: tuple = ()):
        cursor = self._require_connection().cursor()
        try:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()

    def execute_write(self, sql: str, params: tuple = ()) -> int:
        """Run a DML statement and commit it. Rolls back on failure."""
        connection = self._require_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            affected = cursor.rowcount
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

    def call_procedure(self, sql: str, params: tuple, rollback: bool) -> ProcedureOutput:
        """Execute a procedure, drain every result set, then commit or roll back."""
        connection = self._require_connection()
        cursor = connection.cursor()
        grids: list[pd.DataFrame] = []
        messages: list[str] = []
        try:
            cursor.execute(sql, params)
            while True:
                if cursor.description:
                    columns = [d[0] for d in cursor.description]
                    rows = [tuple(row) for row in cursor.fetchall()]
                    grids.append(pd.DataFrame(rows, columns=columns))
                if not cursor.nextset():
                    break
            messages = [str(message[1]) for message in (cursor.messages or [])]
        finally:
            cursor.close()
            if rollback:
                connection.rollback()
            else:
                connection.commit()
        return ProcedureOutput(grids=grids, messages=messages)

    def safe_rollback(self) -> None:
        if self.connection is not None:
            try:
                self.connection.rollback()
            except Exception as exc:
                logger.debug("Rollback suppressed: %s", exc)
