"""Unit tests for the DCC Remediation pure query-builders, mapping and DDL.

These import only the pure modules (mapping / snapshot / worklist / analytics /
fixlog / ddl); they never touch Snowflake, Streamlit, or the connector, so they
run in the standard suite with no extra dependencies.
"""

from __future__ import annotations

import pytest

from dcc_console.catalog import TEST_CATALOG
from dcc_console.remediation import (
    DEFAULT_OBJECTS,
    MAINTENANCE_SOURCE,
    RemediationObjects,
    ddl,
    fixlog,
    mapping,
    snapshot,
)
from dcc_console.remediation import analytics as an
from dcc_console.remediation import worklist as wl

# A concrete tenant used across builder tests, plus a *different* one to prove the
# schema is not hardcoded anywhere.
OBJS = RemediationObjects("DEV_CORE_AAB")
OTHER = RemediationObjects("DEV_CORE_XYZ", "MY_SCHEMA")

# --------------------------------------------------------------------------- #
# RemediationObjects (multi-tenant)
# --------------------------------------------------------------------------- #


def test_default_objects_is_dev_core_aab():
    assert DEFAULT_OBJECTS.database == "DEV_CORE_AAB"
    assert DEFAULT_OBJECTS.schema == "DCC_REMEDIATION"


def test_objects_build_fully_qualified_names():
    assert OTHER.schema_fqn == "DEV_CORE_XYZ.MY_SCHEMA"
    assert OTHER.snapshot_table == "DEV_CORE_XYZ.MY_SCHEMA.HEALTH_DAILY_SNAPSHOT"
    assert OTHER.fix_log_table == "DEV_CORE_XYZ.MY_SCHEMA.APP_FIX_LOG"
    assert OTHER.flag_reference_table == "DEV_CORE_XYZ.MY_SCHEMA.FLAG_REFERENCE"
    assert OTHER.v_current_broken == "DEV_CORE_XYZ.MY_SCHEMA.V_CURRENT_BROKEN"
    assert OTHER.v_remediation_kpis == "DEV_CORE_XYZ.MY_SCHEMA.V_REMEDIATION_KPIS"


def test_objects_reject_injection_identifiers():
    for bad in ("bad;name", "db-name", "a.b", "drop table x", "", "1abc"):
        with pytest.raises(ValueError):
            RemediationObjects(bad)
    with pytest.raises(ValueError):
        RemediationObjects("DEV_CORE_AAB", "sch ema")


def test_objects_trim_whitespace():
    obj = RemediationObjects("  DEV_CORE_AAB  ", "  DCC_REMEDIATION ")
    assert obj.database == "DEV_CORE_AAB"
    assert obj.schema == "DCC_REMEDIATION"


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #


def test_tracked_checks_cover_all_flag_fixes():
    assert len(mapping.FLAG_FIXES) == 14
    assert len(mapping.TRACKED_CHECK_COLUMNS) == 14
    assert len(set(mapping.TRACKED_CHECK_COLUMNS)) == 14


def test_fixable_subset_matches_flag_fixes():
    expected = {f.check_column for f in mapping.FLAG_FIXES if f.fixable}
    assert set(mapping.FIXABLE_CHECK_COLUMNS) == expected
    assert "FIRMWARE_VERSION_CHECK" not in mapping.FIXABLE_CHECK_COLUMNS
    assert "DCCMERCHANT_NO_CHECK_O" not in mapping.FIXABLE_CHECK_COLUMNS
    assert "DCCFLAGSENABLED_CHECK_C" not in mapping.FIXABLE_CHECK_COLUMNS
    assert "LOCATION_DCCENABLED_CHECK_C" not in mapping.FIXABLE_CHECK_COLUMNS


def test_handler_split_is_by_check_o_suffix():
    for col in mapping.HANDLER_CHECK_COLUMNS:
        assert col.endswith("_CHECK_O")
    for col in mapping.NON_HANDLER_CHECK_COLUMNS:
        assert not col.endswith("_CHECK_O")
    assert set(mapping.HANDLER_CHECK_COLUMNS) | set(mapping.NON_HANDLER_CHECK_COLUMNS) == set(
        mapping.TRACKED_CHECK_COLUMNS
    )


def test_resolve_and_is_tracked():
    fix = mapping.resolve("HANDLER_DCCENABLE_CHECK_O")
    assert fix.fix_bit == 8
    assert fix.target_id_kind == mapping.INSTANCE
    assert fix.fix_value == "dccEnable"
    assert fix.sp_column == "extra_config"
    assert mapping.is_tracked("HANDLER_DCCENABLE_CHECK_O")
    assert not mapping.is_tracked("NOPE")


def test_catalog_keys_exist_and_sp_columns_align():
    for fix in mapping.FLAG_FIXES:
        if fix.catalog_key is None:
            continue
        assert fix.catalog_key in TEST_CATALOG, fix.catalog_key
        definition = TEST_CATALOG[fix.catalog_key]
        if fix.sp_column is not None and definition.sp_managed:
            assert fix.sp_column == definition.sp_column


def test_fixable_flags_have_bit_and_target():
    for fix in mapping.FLAG_FIXES:
        if fix.fixable:
            assert fix.fix_bit is not None
            assert fix.target_id_kind in (mapping.INSTANCE, mapping.TERMINAL, mapping.LOCATION)


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #


def test_refresh_merge_mentions_every_tracked_check():
    sql = snapshot.build_refresh_merge(OBJS)
    for col in mapping.TRACKED_CHECK_COLUMNS:
        assert col in sql
    assert "CONFIGDOWNLOAD_VERSION_CHECK_BASE" in sql


def test_refresh_merge_is_terminal_grain_upsert():
    sql = snapshot.build_refresh_merge(OBJS)
    assert "MERGE INTO" in sql
    assert OBJS.snapshot_table in sql
    assert MAINTENANCE_SOURCE in sql
    assert "GROUP BY TERMINAL_IDENTIFIER" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "IS_DCC_BROKEN = TRUE" in sql


def test_refresh_merge_targets_the_given_schema():
    sql = snapshot.build_refresh_merge(OTHER)
    assert OTHER.snapshot_table in sql
    assert "DEV_CORE_AAB" not in sql.replace(MAINTENANCE_SOURCE, "")


# --------------------------------------------------------------------------- #
# worklist
# --------------------------------------------------------------------------- #


def test_worklist_default_filters_on_actionable():
    sql, params = wl.build_worklist_query(wl.WorklistFilters(), OBJS)
    assert sql.startswith("SELECT * FROM ")
    assert OBJS.v_current_broken in sql
    assert "REMEDIATION_STATE = %s" in sql
    assert params == ["ACTIONABLE"]
    assert sql.strip().endswith("LIMIT 500")


def test_worklist_uses_given_schema():
    sql, _ = wl.build_worklist_query(wl.WorklistFilters(), OTHER)
    assert OTHER.v_current_broken in sql


def test_worklist_no_state_when_none():
    sql, params = wl.build_worklist_query(
        wl.WorklistFilters(remediation_state=None, limit=10), OBJS
    )
    assert "REMEDIATION_STATE" not in sql
    assert params == []
    assert sql.strip().endswith("LIMIT 10")


def test_worklist_dimension_in_clause_binds_values():
    filters = wl.WorklistFilters(
        remediation_state=None,
        dimensions={"COUNTRY_NAME": ("Germany", "France")},
    )
    sql, params = wl.build_worklist_query(filters, OBJS)
    assert "COUNTRY_NAME IN (%s, %s)" in sql
    assert params == ["Germany", "France"]


def test_worklist_check_filter_orders_and_binds_nothing():
    filters = wl.WorklistFilters(
        remediation_state=None,
        check_columns=("HANDLER_DCCENABLE_CHECK_O", "DCCXPRESSCO_CHECK_O"),
    )
    sql, _ = wl.build_worklist_query(filters, OBJS)
    assert "HANDLER_DCCENABLE_CHECK_O = 1" in sql
    assert "DCCXPRESSCO_CHECK_O = 1" in sql


def test_worklist_fixable_only_rejects_non_fixable_check():
    filters = wl.WorklistFilters(
        check_columns=("FIRMWARE_VERSION_CHECK",),
        fixable_only=True,
    )
    with pytest.raises(ValueError, match="non-fixable"):
        wl.build_worklist_query(filters, OBJS)


def test_worklist_non_fixable_check_allowed_when_not_restricted():
    filters = wl.WorklistFilters(
        remediation_state=None,
        check_columns=("FIRMWARE_VERSION_CHECK",),
        fixable_only=False,
    )
    sql, _ = wl.build_worklist_query(filters, OBJS)
    assert "FIRMWARE_VERSION_CHECK = 1" in sql


def test_worklist_invalid_state_and_dimension_raise():
    with pytest.raises(ValueError):
        wl.build_worklist_query(wl.WorklistFilters(remediation_state="BOGUS"), OBJS)
    with pytest.raises(ValueError):
        wl.build_worklist_query(
            wl.WorklistFilters(remediation_state=None, dimensions={"DROP_TABLE": ("x",)}), OBJS
        )


def test_worklist_limit_is_capped():
    sql, _ = wl.build_worklist_query(
        wl.WorklistFilters(remediation_state=None, limit=999999), OBJS
    )
    assert sql.strip().endswith("LIMIT 5000")


def test_distinct_values_query_validates_column():
    assert "COUNTRY_NAME" in wl.build_distinct_values_query("COUNTRY_NAME", OBJS)
    with pytest.raises(ValueError):
        wl.build_distinct_values_query("HAXX", OBJS)


# --------------------------------------------------------------------------- #
# analytics
# --------------------------------------------------------------------------- #


def test_analytics_builders_reference_views():
    assert OBJS.v_current_broken in an.build_state_totals_query(OBJS)
    assert OBJS.v_current_broken in an.build_flag_totals_query(OBJS)
    assert OBJS.v_remediation_kpis in an.build_kpis_query(OBJS)


def test_breakdown_validates_dimension_and_groups():
    sql = an.build_breakdown_query("REGION", OBJS, top_n=5)
    assert "GROUP BY REGION" in sql
    assert sql.strip().endswith("LIMIT 5")
    with pytest.raises(ValueError):
        an.build_breakdown_query("; DROP", OBJS)


def test_flag_totals_sums_every_tracked_check():
    sql = an.build_flag_totals_query(OBJS)
    for col in mapping.TRACKED_CHECK_COLUMNS:
        assert f"SUM({col})" in sql


# --------------------------------------------------------------------------- #
# fixlog
# --------------------------------------------------------------------------- #


def _valid_fix_record() -> dict:
    return {
        "CORRELATION_ID": "abc-123",
        "APPLIED_BY": "tester@planet",
        "MODE": "LIVE",
        "ENVIRONMENT": "DEV",
        "SERVER": "LU3C01DVSQL01",
        "DATABASE_NAME": "3CDB",
        "FIX_BIT": 8,
        "TARGET_ID_KIND": "INSTANCE_IDENTIFIER",
        "TARGET_IDENTIFIER": "I000000021",
        "OUTCOME": "APPLIED",
        "CHECK_COLUMN": "HANDLER_DCCENABLE_CHECK_O",
        "VALUE_SENT": "dccEnable",
        "VERIFIED": True,
    }


def test_fixlog_insert_builds_parameterised_statement():
    sql, params = fixlog.build_insert(_valid_fix_record(), OBJS)
    assert OBJS.fix_log_table in sql
    assert sql.count("%s") == len(params)
    assert "I000000021" in params
    assert "abc-123" in params


def test_fixlog_insert_uses_given_schema():
    sql, _ = fixlog.build_insert(_valid_fix_record(), OTHER)
    assert OTHER.fix_log_table in sql


def test_fixlog_insert_rejects_unknown_column():
    record = _valid_fix_record()
    record["BOBBY_TABLES"] = 1
    with pytest.raises(ValueError, match="Unknown fix-log column"):
        fixlog.build_insert(record, OBJS)


def test_fixlog_insert_requires_mandatory_columns():
    record = _valid_fix_record()
    del record["CORRELATION_ID"]
    with pytest.raises(ValueError, match="Missing required"):
        fixlog.build_insert(record, OBJS)


def test_fixlog_insert_validates_mode_and_outcome():
    bad_mode = _valid_fix_record()
    bad_mode["MODE"] = "YOLO"
    with pytest.raises(ValueError, match="Invalid MODE"):
        fixlog.build_insert(bad_mode, OBJS)
    bad_outcome = _valid_fix_record()
    bad_outcome["OUTCOME"] = "MAYBE"
    with pytest.raises(ValueError, match="Invalid OUTCOME"):
        fixlog.build_insert(bad_outcome, OBJS)


def test_recent_fix_query_shape():
    sql, params = fixlog.build_recent_fix_query(OBJS)
    assert "MAX(APPLIED_AT)" in sql
    assert "TERMINAL_IDENTIFIER = %s" in sql
    assert "CHECK_COLUMN = %s" in sql
    assert params == []


# --------------------------------------------------------------------------- #
# ddl (per-user schema initialiser)
# --------------------------------------------------------------------------- #


def test_ddl_create_statements_cover_all_objects():
    statements = ddl.build_create_statements(OBJS)
    assert len(statements) == 7
    joined = "\n".join(statements)
    assert f"CREATE SCHEMA IF NOT EXISTS {OBJS.schema_fqn}" in joined
    for table in ("HEALTH_DAILY_SNAPSHOT", "APP_FIX_LOG", "FLAG_REFERENCE"):
        assert f"{OBJS.schema_fqn}.{table}" in joined
    for view in ("V_CURRENT_BROKEN", "V_FIX_HISTORY", "V_REMEDIATION_KPIS"):
        assert f"{OBJS.schema_fqn}.{view}" in joined
    # Snapshot DDL must declare every tracked check column.
    for col in mapping.TRACKED_CHECK_COLUMNS:
        assert f"{col} INTEGER" in joined


def test_ddl_uses_given_schema_only():
    joined = "\n".join(ddl.build_create_statements(OTHER))
    assert "DEV_CORE_XYZ.MY_SCHEMA" in joined
    assert "DEV_CORE_AAB" not in joined


def test_ddl_seed_has_all_flags_from_mapping():
    seed = ddl.build_seed_merge(OBJS)
    assert OBJS.flag_reference_table in seed
    for fix in mapping.FLAG_FIXES:
        assert f"'{fix.check_column}'" in seed
    # Value spelling comes from mapping (catalog spelling), not the old seed file.
    assert "'DCCXpressCOFallback'" in seed


def test_ddl_objects_present_query_uses_information_schema():
    sql = ddl.build_objects_present_query(OTHER)
    assert "DEV_CORE_XYZ.INFORMATION_SCHEMA.TABLES" in sql
    assert "TABLE_SCHEMA = 'MY_SCHEMA'" in sql
    for table in ddl.TABLE_NAMES:
        assert f"'{table}'" in sql
