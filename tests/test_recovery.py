"""Recovery integrity (§7.2): journal restore kind, recovery guards, rollback state.

Covers the three P0s from the PR #2 review plus the journal/commit safety found
while wiring the live fixer: SP-script entries recover without bound parameters,
recovery refuses a different server/database, a successful procedure rollback is
no longer offered again, a kept fix drops out of crash recovery, and a failed live
procedure call is rolled back rather than committed.
"""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from dcc_console.catalog import TEST_CATALOG
from dcc_console.database import DatabaseConnection
from dcc_console.execution import TestResult, apply_rollback
from dcc_console.recovery import recover_entry
from dcc_console.rollback import METHOD_COLUMN, METHOD_PROCEDURE_SCRIPT, restore_statement

BIT2 = "Bit 2 — Config Download Version (terminal)"
BIT8 = "Bit 8 — DCC Handler Flags (instance)"


@pytest.fixture
def journal(tmp_path, monkeypatch):
    import dcc_console.journal as journal_mod

    importlib.reload(journal_mod)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DIR", tmp_path)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DB", tmp_path / "journal.db")
    instance = journal_mod.RestoreJournal()
    yield instance
    instance.close()


def _record(journal, **overrides) -> int:
    fields = dict(
        environment="UAT",
        server="uat-sql",
        database_name="3CDB",
        login="svc",
        test_key=BIT2,
        bit=2,
        target_type="terminal",
        target="T1",
        config_value="ECB DCC",
        original_value="1",
        restore_sql=restore_statement(TEST_CATALOG[BIT2]),
    )
    fields.update(overrides)
    return journal.record_restore_point(**fields)


class FakeSql:
    """Records writes; answers the verify-column read with ``state``."""

    def __init__(self, server="uat-sql", database="3CDB", state="1", batch_rows=1):
        self.server, self.database = server, database
        self.connection = object()
        self.state = state
        self.batch_rows = batch_rows
        self.writes: list[tuple[str, tuple]] = []
        self.batches: list[list[str]] = []

    def execute_write(self, sql, params=()):
        self.writes.append((sql, tuple(params)))
        return 1

    def execute_batch(self, statements):
        self.batches.append(list(statements))
        return self.batch_rows

    def scalar(self, sql, params=()):
        return self.state


# --- journal ------------------------------------------------------------------


def test_new_entries_are_column_restores(journal):
    entry = journal.get_entry(_record(journal))
    assert entry["restore_kind"] == "COLUMN"
    assert entry["status"] == "PENDING"


def test_procedure_scripts_are_stored_as_json_and_flagged(journal):
    entry_id = _record(journal, test_key=BIT8, bit=8, target_type="instance")
    journal.set_procedure_scripts(entry_id, ["UPDATE h SET x = 'a'", "  ", "UPDATE h SET y = 'b'"])
    entry = journal.get_entry(entry_id)
    assert entry["restore_kind"] == "SP_SCRIPT"
    assert json.loads(entry["restore_sql"]) == ["UPDATE h SET x = 'a'", "UPDATE h SET y = 'b'"]
    assert journal.procedure_scripts(entry) == ["UPDATE h SET x = 'a'", "UPDATE h SET y = 'b'"]


def test_legacy_newline_joined_script_is_one_batch(journal):
    legacy = {"restore_sql": "UPDATE h SET x = 'line1\nline2'"}
    assert journal.procedure_scripts(legacy) == ["UPDATE h SET x = 'line1\nline2'"]


def test_migration_classifies_existing_rows(tmp_path, monkeypatch):
    import dcc_console.journal as journal_mod

    db = tmp_path / "journal.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE restore_journal (id INTEGER PRIMARY KEY AUTOINCREMENT, environment TEXT "
        "NOT NULL, server TEXT NOT NULL, database_name TEXT NOT NULL, login TEXT NOT NULL, "
        "test_key TEXT NOT NULL, bit INTEGER NOT NULL, target_type TEXT NOT NULL, target TEXT "
        "NOT NULL, config_value TEXT, original_value TEXT, restore_sql TEXT NOT NULL, "
        "campaign_id TEXT, status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL, "
        "resolved_at TEXT)"
    )
    insert = (
        "INSERT INTO restore_journal (environment, server, database_name, login, test_key, "
        "bit, target_type, target, restore_sql, created_at) "
        "VALUES ('UAT', 's', 'd', 'l', ?, ?, 't', 'x', ?, '2026-01-01')"
    )
    old.execute(insert, (BIT2, 2, restore_statement(TEST_CATALOG[BIT2])))
    old.execute(insert, (BIT8, 8, "UPDATE [cccintegrang].[handler] SET extra_config = '<x/>'"))
    old.commit()
    old.close()

    importlib.reload(journal_mod)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DIR", tmp_path)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DB", db)
    migrated = journal_mod.RestoreJournal()
    kinds = {e["test_key"]: e["restore_kind"] for e in migrated.pending_entries()}
    migrated.close()
    assert kinds == {BIT2: "COLUMN", BIT8: "SP_SCRIPT"}


def test_kept_fix_leaves_crash_recovery(journal):
    entry_id = _record(journal)
    journal.mark_kept(entry_id)
    assert journal.pending_entries() == []
    assert journal.get_entry(entry_id)["status"] == "KEPT"


# --- recovery guards ----------------------------------------------------------------


def test_sp_script_entry_recovers_without_parameters(journal):
    entry_id = _record(journal, test_key=BIT8, bit=8, target_type="instance")
    journal.set_procedure_scripts(entry_id, ["UPDATE h SET extra_config = '<x/>'"])
    sql = FakeSql()

    outcome = recover_entry(journal.get_entry(entry_id), sql, "UAT", journal)

    assert outcome.ok, outcome.message
    assert sql.batches == [["UPDATE h SET extra_config = '<x/>'"]]
    assert sql.writes == []  # never bound (original_value, target) onto the script
    assert journal.get_entry(entry_id)["status"] == "RESOLVED"


def test_column_entry_recovers_with_bound_value_and_verifies(journal):
    entry_id = _record(journal)
    sql = FakeSql(state="1")

    outcome = recover_entry(journal.get_entry(entry_id), sql, "UAT", journal)

    assert outcome.ok, outcome.message
    assert sql.writes == [(restore_statement(TEST_CATALOG[BIT2]), ("1", "T1"))]
    assert journal.get_entry(entry_id)["status"] == "RESOLVED"


def test_column_restore_that_does_not_land_stays_pending(journal):
    entry_id = _record(journal)
    outcome = recover_entry(journal.get_entry(entry_id), FakeSql(state="2"), "UAT", journal)
    assert not outcome.ok
    assert journal.get_entry(entry_id)["status"] == "PENDING"


@pytest.mark.parametrize(
    ("server", "database", "env"),
    [("other-sql", "3CDB", "UAT"), ("uat-sql", "OTHERDB", "UAT"), ("uat-sql", "3CDB", "PROD")],
)
def test_recovery_refuses_a_different_target(journal, server, database, env):
    entry_id = _record(journal)
    sql = FakeSql(server=server, database=database)

    outcome = recover_entry(journal.get_entry(entry_id), sql, env, journal)

    assert not outcome.ok
    assert sql.writes == [] and sql.batches == []
    assert journal.get_entry(entry_id)["status"] == "PENDING"


def test_recovery_refuses_to_write_null(journal):
    entry_id = _record(journal, original_value=None)
    sql = FakeSql()
    outcome = recover_entry(journal.get_entry(entry_id), sql, "UAT", journal)
    assert not outcome.ok and "NULL" in outcome.message
    assert sql.writes == []


def test_recovery_skips_entries_no_longer_pending(journal):
    entry_id = _record(journal)
    stale_copy = journal.get_entry(entry_id)
    journal.mark_kept(entry_id)
    sql = FakeSql()
    outcome = recover_entry(stale_copy, sql, "UAT", journal)
    assert not outcome.ok
    assert sql.writes == []


def test_recovery_requires_a_connection(journal):
    entry = journal.get_entry(_record(journal))
    assert not recover_entry(entry, None, "UAT", journal).ok


# --- rollback availability ------------------------------------------------------


def _live_sp_result(**overrides) -> TestResult:
    fields = dict(
        id="8-1",
        test_key=BIT8,
        bit=8,
        environment="UAT",
        login="svc",
        target_type="instance",
        target="I1",
        value="dccEnable",
        mode="LIVE",
        status="PASS",
        error=None,
        state_before="<pc/>",
        state_after="<pc/>",
        restore_point="<pc/>",
        change_persisted=False,
        transaction="COMMITTED",
        sql="EXEC ...",
        messages=[],
        grids=[],
        grid_frames=[],
        duration_s=0.1,
        timestamp="2026-09-29T12:00:00",
        sp_managed=True,
        journal_id=None,
        sp_rollback_scripts=["UPDATE [cccintegrang].[handler] SET extra_config = '<x/>'"],
    )
    fields.update(overrides)
    return TestResult(**fields)


def test_successful_sp_rollback_is_no_longer_offered():
    result = _live_sp_result()
    assert result.rollback_available is True

    class Conn(FakeSql):
        def query(self, sql, params=()):
            import pandas as pd

            return pd.DataFrame([])

    outcome = apply_rollback(Conn(), TEST_CATALOG[BIT8], result, trigger="manual")

    assert outcome.ok
    assert result.rollback_log[-1]["method"] == METHOD_PROCEDURE_SCRIPT
    assert result.sp_rolled_back is True
    assert result.rollback_available is False
    assert result.effective_change_persisted is False


def test_failed_sp_rollback_is_still_offered():
    result = _live_sp_result()
    result.rollback_log.append(
        {"ok": False, "method": METHOD_PROCEDURE_SCRIPT, "trigger": "manual", "rows": 0}
    )
    assert result.rollback_available is True


def test_automatic_column_restore_does_not_hide_the_sp_rollback():
    result = _live_sp_result()
    result.rollback_log.append({"ok": True, "method": METHOD_COLUMN, "trigger": "automatic"})
    assert result.rollback_available is True


# --- a failed live procedure call is never committed ----------------------------------


class _Cursor:
    def __init__(self, fail: bool):
        self.fail = fail
        self.messages = []
        self.description = None

    def execute(self, sql, params):
        if self.fail:
            raise RuntimeError("procedure raised")

    def nextset(self):
        return False

    def close(self):
        pass


class _Pyodbc:
    def __init__(self, fail: bool):
        self.fail = fail
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self.fail)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize(
    ("fail", "simulation", "commits"),
    [
        (True, False, 0),  # live call raised -> rolled back, never committed
        (False, False, 1),  # live call succeeded -> committed
        (False, True, 0),  # simulation -> always rolled back
    ],
)
def test_call_procedure_only_commits_a_successful_live_call(fail, simulation, commits):
    db = DatabaseConnection("s", "d", "u", "p")
    db.connection = _Pyodbc(fail)
    if fail:
        with pytest.raises(RuntimeError):
            db.call_procedure("EXEC x", (), rollback=simulation)
    else:
        db.call_procedure("EXEC x", (), rollback=simulation)
    assert db.connection.commits == commits
    assert db.connection.rollbacks == 1 - commits
