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

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_JOURNAL_DIR = Path.home() / ".dcc_console"
_JOURNAL_DB = _JOURNAL_DIR / "restore_journal.db"

# Environments that use the journal.  DEV is deliberately excluded.
_JOURNALED_ENVIRONMENTS = {"UAT", "PROD"}

# How an entry is restored. COLUMN: the generic parameterised UPDATE, bound with
# (original_value, target). SP_SCRIPT: the procedure's own complete compensating
# statement(s), stored as a JSON list and run verbatim with no parameters.
RESTORE_COLUMN = "COLUMN"
RESTORE_SP_SCRIPT = "SP_SCRIPT"

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
    resolved_at     TEXT,
    restore_kind    TEXT    NOT NULL DEFAULT 'COLUMN'
);

CREATE INDEX IF NOT EXISTS ix_journal_env_status
    ON restore_journal (environment, status);
"""

_COLUMNS = (
    "id", "environment", "server", "database_name", "login",
    "test_key", "bit", "target_type", "target", "config_value",
    "original_value", "restore_sql", "campaign_id",
    "status", "created_at", "resolved_at", "restore_kind",
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM restore_journal"  # noqa: S608 - constant columns

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
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring a journal created by an earlier release up to the current schema."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(restore_journal)")}
        if "restore_kind" not in columns:
            self._conn.execute(
                "ALTER TABLE restore_journal "
                f"ADD COLUMN restore_kind TEXT NOT NULL DEFAULT '{RESTORE_COLUMN}'"
            )
            # Before this column existed, procedure-managed entries had their generic
            # restore SQL overwritten with the procedure's script. The generic statement
            # always ends with its bound row key (`= ?`); anything else is a script.
            self._conn.execute(
                "UPDATE restore_journal SET restore_kind = ? WHERE rtrim(restore_sql) NOT LIKE ?",
                (RESTORE_SP_SCRIPT, "%= ?"),
            )

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
        The entry starts as a generic column restore; procedure-managed bits
        switch it to the procedure's own script via :meth:`set_procedure_scripts`.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = self._conn.execute(
            """INSERT INTO restore_journal
               (environment, server, database_name, login, test_key, bit,
                target_type, target, config_value, original_value,
                restore_sql, campaign_id, status, created_at, restore_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)""",
            (
                environment, server, database_name, login,
                test_key, bit, target_type, target, config_value,
                str(original_value) if original_value is not None else None,
                restore_sql, campaign_id, now, RESTORE_COLUMN,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def update_restore_sql(self, entry_id: int, restore_sql: str) -> None:
        """Replace the recorded restore SQL for a pending entry, keeping its kind.

        Procedure-managed bits must use :meth:`set_procedure_scripts` instead, so
        recovery knows to run the script without bound parameters.
        """
        self._conn.execute(
            "UPDATE restore_journal SET restore_sql = ? WHERE id = ?",
            (restore_sql, entry_id),
        )
        self._conn.commit()

    def set_procedure_scripts(self, entry_id: int, scripts: list[str]) -> None:
        """Make the procedure's own rollback script(s) the authoritative restore.

        Used for procedure-managed bits (Bit 8, Bit 16): the restore point written
        before the call is the generic column restore, but the real rollback is the
        procedure's returned script, only known after the call. Stored as a JSON list
        and flagged ``SP_SCRIPT`` so recovery runs each statement verbatim.
        """
        statements = [str(s) for s in scripts if str(s).strip()]
        self._conn.execute(
            "UPDATE restore_journal SET restore_sql = ?, restore_kind = ? WHERE id = ?",
            (json.dumps(statements), RESTORE_SP_SCRIPT, entry_id),
        )
        self._conn.commit()

    @staticmethod
    def procedure_scripts(entry: dict) -> list[str]:
        """The statements to run for an ``SP_SCRIPT`` entry.

        Entries written by an earlier release hold the scripts newline-joined rather
        than as JSON; those are returned as one batch, never split (a script's own
        text may contain newlines).
        """
        raw = entry.get("restore_sql") or ""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return [raw] if raw.strip() else []
        if isinstance(parsed, list):
            return [str(s) for s in parsed if str(s).strip()]
        return [raw]

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

    def mark_kept(self, entry_id: int) -> None:
        """Mark a live change as intentionally kept (a verified remediation fix).

        Test runs are expected to be rolled back, so a committed change stays PENDING
        and is offered by crash recovery. A remediation fix is meant to stay applied:
        once it is verified and logged it must drop out of crash recovery, or "Restore
        ALL" would silently undo it. The local row is kept until the normal 30-day
        purge; the durable audit (before-state + rollback script) is ``APP_FIX_LOG``.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE restore_journal SET status = 'KEPT', resolved_at = ? "
            "WHERE id = ? AND status = 'PENDING'",
            (now, entry_id),
        )
        self._conn.commit()

    def pending_entries(self, environment: str | None = None) -> list[dict]:
        """Return all PENDING entries, optionally filtered by environment."""
        if environment:
            rows = self._conn.execute(
                f"{_SELECT} WHERE status = 'PENDING' AND environment = ? "
                "ORDER BY created_at DESC",
                (environment,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"{_SELECT} WHERE status = 'PENDING' ORDER BY created_at DESC",
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def all_entries(self, limit: int = 100) -> list[dict]:
        """Return recent journal entries for the recovery UI."""
        rows = self._conn.execute(
            f"{_SELECT} ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_entry(self, entry_id: int) -> dict | None:
        row = self._conn.execute(f"{_SELECT} WHERE id = ?", (entry_id,)).fetchone()
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
        return dict(zip(_COLUMNS, row, strict=False))


# Module-level singleton.  Initialised lazily by get_journal().
_journal: RestoreJournal | None = None


def get_journal() -> RestoreJournal:
    """Return the singleton journal instance (created on first call)."""
    global _journal
    if _journal is None:
        _journal = RestoreJournal()
        _journal.purge_old()
    return _journal
