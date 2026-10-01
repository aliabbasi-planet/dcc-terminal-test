from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dcc_console.journal as journal_module
from dcc_console.journal import RestoreJournal


def make_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_module, "_JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(journal_module, "_JOURNAL_DB", tmp_path / "journal" / "restore.db")
    return RestoreJournal()


def test_journal_environment_policy():
    assert RestoreJournal.is_journaled("uat") is True
    assert RestoreJournal.is_journaled("PROD") is True
    assert RestoreJournal.is_journaled("DEV") is False


def test_record_pending_update_and_resolve(tmp_path, monkeypatch):
    journal = make_journal(tmp_path, monkeypatch)
    entry_id = journal.record_restore_point(
        environment="UAT",
        server="sql",
        database_name="db",
        login="tester",
        test_key="Bit 1",
        bit=1,
        target_type="location",
        target="000001",
        config_value="Add",
        original_value="<old/>",
        restore_sql="UPDATE old",
        campaign_id="campaign-1",
    )
    entry = journal.get_entry(entry_id)
    assert entry["status"] == "PENDING"
    assert entry["original_value"] == "<old/>"
    assert journal.pending_entries("UAT")[0]["id"] == entry_id

    journal.update_restore_sql(entry_id, "UPDATE procedure_script")
    assert journal.get_entry(entry_id)["restore_sql"] == "UPDATE procedure_script"
    journal.mark_resolved(entry_id)
    assert journal.get_entry(entry_id)["status"] == "RESOLVED"
    assert journal.get_entry(entry_id)["resolved_at"] is not None
    journal.close()


def test_mark_not_needed_and_all_entries_filter(tmp_path, monkeypatch):
    journal = make_journal(tmp_path, monkeypatch)
    first = journal.record_restore_point(
        environment="UAT", server="s", database_name="d", login="l",
        test_key="Bit 1", bit=1, target_type="location", target="1",
        config_value="Add", original_value=None, restore_sql="SQL",
    )
    second = journal.record_restore_point(
        environment="PROD", server="s", database_name="d", login="l",
        test_key="Bit 2", bit=2, target_type="terminal", target="2",
        config_value="Standard", original_value=1, restore_sql="SQL",
    )
    journal.mark_not_needed(first)
    assert journal.pending_entries("UAT") == []
    assert journal.pending_entries("PROD")[0]["id"] == second
    assert len(journal.all_entries(limit=10)) == 2
    journal.close()


def test_purge_old_removes_only_old_resolved_rows(tmp_path, monkeypatch):
    journal = make_journal(tmp_path, monkeypatch)
    old_id = journal.record_restore_point(
        environment="UAT", server="s", database_name="d", login="l",
        test_key="old", bit=1, target_type="location", target="1",
        config_value="Add", original_value="x", restore_sql="SQL",
    )
    journal.mark_resolved(old_id)
    old_time = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    journal._conn.execute(
        "UPDATE restore_journal SET created_at = ? WHERE id = ?", (old_time, old_id)
    )
    journal._conn.commit()

    pending_id = journal.record_restore_point(
        environment="UAT", server="s", database_name="d", login="l",
        test_key="pending", bit=1, target_type="location", target="2",
        config_value="Add", original_value="x", restore_sql="SQL",
    )
    assert journal.purge_old() == 1
    assert journal.get_entry(old_id) is None
    assert journal.get_entry(pending_id)["status"] == "PENDING"
    journal.close()
