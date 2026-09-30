"""Unit tests for the durable fix registry builders (``remediation.registry``)."""

from __future__ import annotations

import pytest

from dcc_console.remediation import RemediationObjects, registry

OBJS = RemediationObjects("DEV_CORE_AAB")
SHARED = RemediationObjects("DEV_CORE_XYZ", "MY_SCHEMA", "DEV_CORE_AAB", "DCC_REMEDIATION")


def _record(**overrides) -> dict:
    base = {
        "ENVIRONMENT": "PROD",
        "TERMINAL_IDENTIFIER": "T1",
        "CHECK_COLUMN": "HANDLER_DCCENABLE_CHECK_O",
        "INSTANCE_IDENTIFIER": "I1",
        "LOCATION_NO": "L1",
        "FIX_BIT": 8,
        "FLAG_NAME": "Handler DCC Enable",
        "VALUE_SENT": "dccEnable",
        "TARGET_ID_KIND": "INSTANCE_IDENTIFIER",
        "TARGET_IDENTIFIER": "I1",
        "CORRELATION_ID": "c1",
        "CHANGE_REF": "CHG1",
        "RESOLUTION": "APPLIED",
        "SP_ROLLBACK_SCRIPT": "UPDATE handler ...",
        "FIXED_BY": "ALIA",
    }
    base.update(overrides)
    return base


def test_create_table_has_key_and_columns():
    sql = registry.create_table(OBJS)
    assert OBJS.registry_table in sql
    assert "PRIMARY KEY (ENVIRONMENT, TERMINAL_IDENTIFIER, CHECK_COLUMN)" in sql
    for column in ("RESOLUTION", "STATUS", "SP_ROLLBACK_SCRIPT", "FIXED_AT_UTC", "CHANGE_REF"):
        assert column in sql


def test_upsert_single_row_is_parameterised_and_server_timed():
    sql, params = registry.build_upsert([_record()], OBJS)
    assert sql.startswith(f"MERGE INTO {OBJS.registry_table} tgt")
    # Key columns drive the match; the rest are updated.
    assert "tgt.ENVIRONMENT = src.ENVIRONMENT" in sql
    assert "tgt.TERMINAL_IDENTIFIER = src.TERMINAL_IDENTIFIER" in sql
    assert "tgt.CHECK_COLUMN = src.CHECK_COLUMN" in sql
    # Fix time + status come from the server clock, never the client.
    assert "STATUS = 'FIXED'" in sql
    assert "CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())" in sql
    assert "%s::NUMBER AS FIX_BIT" in sql  # numeric column is cast so NULLs stay typed
    assert len(params) == len(registry._INSERTABLE)
    assert params[0] == "PROD" and "dccEnable" in params


def test_upsert_multiple_rows_union_all_and_param_order():
    records = [_record(TERMINAL_IDENTIFIER="T1"), _record(TERMINAL_IDENTIFIER="T2")]
    sql, params = registry.build_upsert(records, OBJS)
    assert sql.count("UNION ALL") == 1
    assert len(params) == 2 * len(registry._INSERTABLE)
    # First column of each row is ENVIRONMENT; the terminal is the 2nd column.
    assert params[1] == "T1" and params[1 + len(registry._INSERTABLE)] == "T2"


def test_upsert_validates_columns_and_resolution():
    with pytest.raises(ValueError):
        registry.build_upsert([], OBJS)
    with pytest.raises(ValueError):
        registry.build_upsert([_record(NOPE="x")], OBJS)
    with pytest.raises(ValueError):
        registry.build_upsert([_record(RESOLUTION="MAYBE")], OBJS)
    with pytest.raises(ValueError):
        registry.build_upsert([_record(CHECK_COLUMN=None)], OBJS)


def test_delete_many_dedups_and_binds():
    sql, params = registry.build_delete_many(
        "PROD", ["T1", "T2", "T1", ""], "HANDLER_DCCENABLE_CHECK_O", OBJS
    )
    assert sql.startswith("DELETE FROM ") and OBJS.registry_table in sql
    assert "TERMINAL_IDENTIFIER IN (%s, %s)" in sql  # deduped, empty dropped
    assert params == ["PROD", "HANDLER_DCCENABLE_CHECK_O", "T1", "T2"]
    with pytest.raises(ValueError):
        registry.build_delete_many("PROD", ["", None], "X", OBJS)


def test_backfill_reads_log_and_only_inserts_missing():
    sql = registry.build_backfill_from_log(SHARED)
    assert SHARED.registry_table in sql and SHARED.fix_log_table in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "WHEN MATCHED" not in sql  # never clobber a live registry row
    assert "OUTCOME IN ('APPLIED', 'SKIPPED_ALREADY_OK') AND VERIFIED = TRUE" in sql
    assert "CONVERT_TIMEZONE('UTC', APPLIED_AT)" in sql  # preserves the freshness guard


def test_uses_the_given_shared_schema_only():
    sql, _ = registry.build_upsert([_record()], SHARED)
    assert SHARED.registry_table in sql
    assert "DEV_CORE_XYZ" not in sql  # the registry lives in the shared schema


def test_registered_terminals_query_scopes_to_prod_fixed():
    sql = registry.build_registered_terminals_query(OBJS)
    assert OBJS.registry_table in sql
    assert "ENVIRONMENT = 'PROD'" in sql and "STATUS = 'FIXED'" in sql
