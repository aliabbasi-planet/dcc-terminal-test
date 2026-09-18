"""Persistent restore journal for disaster recovery.

Survives app crashes, machine reboots, and browser refreshes.  Every live
test writes its restore point to a local SQLite database *before* the
procedure runs.  If the app dies mid-campaign, the journal retains the
original values so they can be restored on the next launch.

Only activated for UAT and PROD environments.  DEV is excluded because
the risk of a stale restore point overwriting intentional work is higher
than the benefit of automatic recovery on a development server.

Location: ``~/.dcc_console/restore_journal.db``
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_JOURNAL_DIR = Path.home() / ".dcc_console"
_JOURNAL_DB = _JOURNAL_DIR / "restore_journal.db"

# Environments that use the journal.  DEV is deliberately excluded.
_JOURNALED_ENVIRONMENTS = {"UAT", "PROD"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS restore_journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    environment     TEXT    NOT NULL,
    server          TEXT    NOT NULL,
    database_name   TEXT    NOT NULL,
    login           TEXT    NOT NULL,
    test_key        TEXT    NOT NULL,
    bit             INTEGER NOT NULL,
    target_type     TEXT    NOT NULL,
    target          TEXT    NOT NULL,
    config_value    TEXT,
    original_value  TEXT,
    restore_sql     TEXT    NOT NULL,
    campaign_id     TEXT,
    status          TEXT    NOT NULL DEFAULT 'PENDING',
    created_at      TEXT    NOT NULL,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS ix_journal_env_status
    ON restore_journal (environment, status);
"""

_PURGE_DAYS = 30


class RestoreJournal:
    """ACID-safe restore point storage backed by SQLite.

    Uses ``check_same_thread=False`` because Streamlit runs callbacks on
    multiple threads.  WAL mode makes concurrent reads safe.
    """

    def __init__(self) -> None:
        _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(_JOURNAL_DB),
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def is_journaled(environment: str) -> bool:
        return environment.upper() in _JOURNALED_ENVIRONMENTS

    def record_restore_point(
        self,
        *,
        environment: str,
        server: str,
        database_name: str,
        login: str,
        test_key: str,
        bit: int,
        target_type: str,
        target: str,
        config_value: str,
        original_value: Any,
        restore_sql: str,
        campaign_id: str | None = None,
    ) -> int:
        """Write a restore point BEFORE the live procedure call.

        Returns the journal row id.  The write is committed immediately
        (WAL mode) so it survives even if the app crashes on the next line.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = self._conn.execute(
            """INSERT INTO restore_journal
               (environment, server, database_name, login, test_key, bit,
                target_type, target, config_value, original_value,
                restore_sql, campaign_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
            (
                environment, server, database_name, login,
                test_key, bit, target_type, target, config_value,
                str(original_value) if original_value is not None else None,
                restore_sql, campaign_id, now,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def mark_resolved(self, entry_id: int) -> None:
        """Mark a journal entry as successfully rolled back."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE restore_journal SET status = 'RESOLVED', resolved_at = ? WHERE id = ?",
            (now, entry_id),
        )
        self._conn.commit()

    def mark_not_needed(self, entry_id: int) -> None:
        """Mark when the live run did not actually change the value."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE restore_journal SET status = 'NO_CHANGE', resolved_at = ? WHERE id = ?",
            (now, entry_id),
        )
        self._conn.commit()

    def pending_entries(self, environment: str | None = None) -> list[dict]:
        """Return all PENDING entries, optionally filtered by environment."""
        if environment:
            rows = self._conn.execute(
                "SELECT * FROM restore_journal WHERE status = 'PENDING' AND environment = ? "
                "ORDER BY created_at DESC",
                (environment,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM restore_journal WHERE status = 'PENDING' "
                "ORDER BY created_at DESC",
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def all_entries(self, limit: int = 100) -> list[dict]:
        """Return recent journal entries for the recovery UI."""
        rows = self._conn.execute(
            "SELECT * FROM restore_journal ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_entry(self, entry_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM restore_journal WHERE id = ?", (entry_id,),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def purge_old(self) -> int:
        """Delete resolved entries older than 30 days."""
        cutoff = datetime.now(timezone.utc)
        cutoff_str = cutoff.isoformat(timespec="seconds")
        cursor = self._conn.execute(
            "DELETE FROM restore_journal WHERE status != 'PENDING' "
            f"AND created_at < datetime(?, '-{_PURGE_DAYS} days')",
            (cutoff_str,),
        )
        self._conn.commit()
        return cursor.rowcount

    def _row_to_dict(self, row: tuple) -> dict:
        cols = [
            "id", "environment", "server", "database_name", "login",
            "test_key", "bit", "target_type", "target", "config_value",
            "original_value", "restore_sql", "campaign_id",
            "status", "created_at", "resolved_at",
        ]
        return dict(zip(cols, row))


# Module-level singleton.  Initialised lazily by get_journal().
_journal: RestoreJournal | None = None


def get_journal() -> RestoreJournal:
    """Return the singleton journal instance (created on first call)."""
    global _journal
    if _journal is None:
        _journal = RestoreJournal()
        _journal.purge_old()
    return _journal
