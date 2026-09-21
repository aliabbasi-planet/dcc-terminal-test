"""Journal update_restore_sql, isolated to a temp database."""

from __future__ import annotations

import importlib


def _fresh_journal(tmp_path, monkeypatch):
    import dcc_console.journal as journal_mod

    importlib.reload(journal_mod)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DIR", tmp_path)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DB", tmp_path / "journal.db")
    return journal_mod.RestoreJournal()


def test_update_restore_sql_replaces_recorded_statement(tmp_path, monkeypatch):
    journal = _fresh_journal(tmp_path, monkeypatch)
    entry_id = journal.record_restore_point(
        environment="UAT", server="s", database_name="3CDB", login="svc",
        test_key="Bit 8 — DCC Handler Flags (instance)", bit=8, target_type="instance",
        target="I000000021", config_value="dccEnable", original_value="<pc/>",
        restore_sql="UPDATE [cccintegrang].[instance] SET package_config = ...",
    )
    journal.update_restore_sql(
        entry_id, "UPDATE [cccintegrang].[handler] SET extra_config = '<x/>'"
    )

    entry = journal.get_entry(entry_id)
    assert "handler" in entry["restore_sql"]
    assert "instance" not in entry["restore_sql"]
    assert entry["status"] == "PENDING"
    journal.close()
