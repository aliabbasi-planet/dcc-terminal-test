"""Unit tests for the fix-confirmation + profit-handoff builders (``remediation.reconcile``)."""

from __future__ import annotations

from dcc_console.remediation import MAINTENANCE_SOURCE, RemediationObjects, reconcile
from dcc_console.remediation.mapping import FLAG_FIXES

OBJS = RemediationObjects("DEV_CORE_AAB")
SHARED = RemediationObjects("DEV_CORE_XYZ", "MY_SCHEMA", "DEV_CORE_AAB", "DCC_REMEDIATION")


def test_confirmed_table_has_event_key_and_dimensions():
    sql = reconcile.create_confirmed_table(OBJS)
    assert OBJS.confirmed_table in sql
    # Keyed on the fix *event* so a terminal re-fixed later is a new handoff row.
    assert "PRIMARY KEY (ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN, FIXED_AT_UTC)" in sql
    for column in ("CHECK_COLUMN", "FIX_BIT", "CHANGE_REF", "CONFIRMED_AT_UTC", "SRC_LOADED_UTC"):
        assert column in sql
    for dim in ("MERCHANT_NAME", "COUNTRY_NAME", "ACQUIRER_NAME", "TERMINAL_MODEL_NAME"):
        assert dim in sql


def test_confirmed_merge_reads_cortex_and_confirms_per_check():
    sql = reconcile.build_confirmed_merge(OBJS)
    assert sql.startswith(f"MERGE INTO {OBJS.confirmed_table} tgt")
    assert MAINTENANCE_SOURCE in sql and OBJS.registry_table in sql
    # Confirmation = source reloaded after the fix AND this check no longer broken.
    assert "c.SRC_LOADED_UTC > r.FIXED_AT_UTC" in sql
    assert "END) = 0" in sql
    # Every tracked check is resolved to its own Cortex flag by the CASE.
    for fix in FLAG_FIXES:
        assert f"WHEN '{fix.check_column}' THEN c.{fix.check_column}" in sql
    # Only still-pending PROD fixes are considered.
    assert "r.ENVIRONMENT = 'PROD' AND r.STATUS = 'FIXED'" in sql
    # Idempotent: insert the event once, never update an existing handoff row.
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "WHEN MATCHED" not in sql
    for key in ("ENVIRONMENT", "TERMINAL_IDENTIFIER", "CHECK_COLUMN", "FIXED_AT_UTC"):
        assert f"tgt.{key} = src.{key}" in sql
    # Every handoff column is carried through.
    for column in reconcile.HANDOFF_COLUMNS:
        assert f"src.{column}" in sql


def test_mark_registry_confirmed_keeps_row_and_flips_status():
    sql = reconcile.build_mark_registry_confirmed(OBJS)
    assert sql.startswith(f"UPDATE {OBJS.registry_table} r")
    assert "SET STATUS = 'CONFIRMED'" in sql
    # Only rows that actually reached the handoff, and only still-pending ones.
    assert OBJS.confirmed_table in sql and "EXISTS (" in sql
    assert "r.ENVIRONMENT = 'PROD' AND r.STATUS = 'FIXED'" in sql


def test_reconcile_block_is_one_atomic_statement():
    block = reconcile.build_reconcile_block(OBJS)
    assert block.startswith("EXECUTE IMMEDIATE $$")
    assert block.rstrip().endswith("$$")
    assert "BEGIN TRANSACTION;" in block and "COMMIT;" in block
    # Runs both halves: hand off, then mark the registry.
    assert f"MERGE INTO {OBJS.confirmed_table} tgt" in block
    assert "SET STATUS = 'CONFIRMED'" in block


def test_reconcile_task_schedules_the_block_without_execute_immediate():
    task = reconcile.build_reconcile_task(OBJS, "DATA_SCIENCE")
    assert task.startswith(f"CREATE TASK IF NOT EXISTS {OBJS.reconcile_task}")
    assert "WAREHOUSE = DATA_SCIENCE" in task
    assert "SCHEDULE = 'USING CRON 30 6 * * * UTC'" in task
    # The task body is the raw Scripting block (no EXECUTE IMMEDIATE wrapper).
    assert "EXECUTE IMMEDIATE" not in task
    assert "BEGIN TRANSACTION;" in task and task.rstrip().endswith("END;")


def test_task_resume_targets_the_task():
    sql = reconcile.build_task_resume(OBJS)
    assert sql == f"ALTER TASK IF EXISTS {OBJS.reconcile_task} RESUME"


def test_pending_summary_counts_pending_confirmed_and_handed_off():
    sql = reconcile.build_pending_summary_query(OBJS)
    assert OBJS.registry_table in sql and OBJS.confirmed_table in sql
    for alias in ("PENDING_CONFIRMATION", "CONFIRMED_IN_REGISTRY", "HANDED_OFF"):
        assert alias in sql
    assert "ENVIRONMENT = 'PROD'" in sql


def test_builders_use_only_the_shared_schema():
    for sql in (
        reconcile.create_confirmed_table(SHARED),
        reconcile.build_confirmed_merge(SHARED),
        reconcile.build_pending_summary_query(SHARED),
    ):
        assert SHARED.confirmed_table in sql
        # The handoff + registry live in the shared schema, never the user's own.
        assert "DEV_CORE_XYZ" not in sql
