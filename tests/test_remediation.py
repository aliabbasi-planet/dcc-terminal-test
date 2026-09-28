"""Unit tests for the DCC Remediation pure query-builders and mapping.

These import only the pure modules (mapping / snapshot / worklist / analytics /
fixlog); they never touch Snowflake, Streamlit, or the connector, so they run in
the standard suite with no extra dependencies.
"""

from __future__ import annotations

import pytest

from dcc_console.catalog import TEST_CATALOG
from dcc_console.remediation import (
    FIX_LOG_TABLE,
    MAINTENANCE_SOURCE,
    SNAPSHOT_TABLE,
    V_CURRENT_BROKEN,
    fixlog,
    mapping,
    snapshot,
)
from dcc_console.remediation import analytics as an
from dcc_console.remediation import worklist as wl

# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #


def test_tracked_checks_cover_all_flag_fixes():
    assert len(mapping.FLAG_FIXES) == 14
    assert len(mapping.TRACKED_CHECK_COLUMNS) == 14
    # No duplicate check columns.
    assert len(set(mapping.TRACKED_CHECK_COLUMNS)) == 14


def test_fixable_subset_matches_flag_fixes():
    expected = {f.check_column for f in mapping.FLAG_FIXES if f.fixable}
    assert set(mapping.FIXABLE_CHECK_COLUMNS) == expected
    # The four confirmed-unfixable checks stay out of the fixable set.
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
        # Where the mapping declares a handler column, it must match the catalog's.
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
    sql = snapshot.build_refresh_merge()
    for col in mapping.TRACKED_CHECK_COLUMNS:
        assert col in sql
    # Irregular BASE name for config download must be used verbatim.
    assert "CONFIGDOWNLOAD_VERSION_CHECK_BASE" in sql


def test_refresh_merge_is_terminal_grain_upsert():
    sql = snapshot.build_refresh_merge()
    assert f"MERGE INTO {SNAPSHOT_TABLE}" in sql
    assert MAINTENANCE_SOURCE in sql
    assert "GROUP BY TERMINAL_IDENTIFIER" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "IS_DCC_BROKEN = TRUE" in sql


# --------------------------------------------------------------------------- #
# worklist
# --------------------------------------------------------------------------- #


def test_worklist_default_filters_on_actionable():
    sql, params = wl.build_worklist_query(wl.WorklistFilters())
    assert sql.startswith("SELECT * FROM ")
    assert V_CURRENT_BROKEN in sql
    assert "REMEDIATION_STATE = %s" in sql
    assert params == ["ACTIONABLE"]
    assert sql.strip().endswith("LIMIT 500")


def test_worklist_no_state_when_none():
    sql, params = wl.build_worklist_query(wl.WorklistFilters(remediation_state=None, limit=10))
    assert "REMEDIATION_STATE" not in sql
    assert params == []
    assert sql.strip().endswith("LIMIT 10")


def test_worklist_dimension_in_clause_binds_values():
    filters = wl.WorklistFilters(
        remediation_state=None,
        dimensions={"COUNTRY_NAME": ("Germany", "France")},
    )
    sql, params = wl.build_worklist_query(filters)
    assert "COUNTRY_NAME IN (%s, %s)" in sql
    assert params == ["Germany", "France"]


def test_worklist_check_filter_orders_and_binds_nothing():
    filters = wl.WorklistFilters(
        remediation_state=None,
        check_columns=("HANDLER_DCCENABLE_CHECK_O", "DCCXPRESSCO_CHECK_O"),
    )
    sql, _ = wl.build_worklist_query(filters)
    assert "HANDLER_DCCENABLE_CHECK_O = 1" in sql
    assert "DCCXPRESSCO_CHECK_O = 1" in sql


def test_worklist_fixable_only_rejects_non_fixable_check():
    filters = wl.WorklistFilters(
        check_columns=("FIRMWARE_VERSION_CHECK",),
        fixable_only=True,
    )
    with pytest.raises(ValueError, match="non-fixable"):
        wl.build_worklist_query(filters)


def test_worklist_non_fixable_check_allowed_when_not_restricted():
    filters = wl.WorklistFilters(
        remediation_state=None,
        check_columns=("FIRMWARE_VERSION_CHECK",),
        fixable_only=False,
    )
    sql, _ = wl.build_worklist_query(filters)
    assert "FIRMWARE_VERSION_CHECK = 1" in sql


def test_worklist_invalid_state_and_dimension_raise():
    with pytest.raises(ValueError):
        wl.build_worklist_query(wl.WorklistFilters(remediation_state="BOGUS"))
    with pytest.raises(ValueError):
        wl.build_worklist_query(
            wl.WorklistFilters(remediation_state=None, dimensions={"DROP_TABLE": ("x",)})
        )


def test_worklist_limit_is_capped():
    sql, _ = wl.build_worklist_query(wl.WorklistFilters(remediation_state=None, limit=999999))
    assert sql.strip().endswith("LIMIT 5000")


def test_distinct_values_query_validates_column():
    assert "COUNTRY_NAME" in wl.build_distinct_values_query("COUNTRY_NAME")
    with pytest.raises(ValueError):
        wl.build_distinct_values_query("HAXX")


# --------------------------------------------------------------------------- #
# analytics
# --------------------------------------------------------------------------- #


def test_analytics_builders_reference_views():
    assert V_CURRENT_BROKEN in an.build_state_totals_query()
    assert V_CURRENT_BROKEN in an.build_flag_totals_query()
    assert "V_REMEDIATION_KPIS" in an.build_kpis_query()


def test_breakdown_validates_dimension_and_groups():
    sql = an.build_breakdown_query("REGION", top_n=5)
    assert "GROUP BY REGION" in sql
    assert sql.strip().endswith("LIMIT 5")
    with pytest.raises(ValueError):
        an.build_breakdown_query("; DROP")


def test_flag_totals_sums_every_tracked_check():
    sql = an.build_flag_totals_query()
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
    sql, params = fixlog.build_insert(_valid_fix_record())
    assert f"INSERT INTO {FIX_LOG_TABLE}" in sql
    # Column count and placeholder count line up, and values are bound not inlined.
    assert sql.count("%s") == len(params)
    assert "I000000021" in params
    assert "abc-123" in params


def test_fixlog_insert_rejects_unknown_column():
    record = _valid_fix_record()
    record["BOBBY_TABLES"] = 1
    with pytest.raises(ValueError, match="Unknown fix-log column"):
        fixlog.build_insert(record)


def test_fixlog_insert_requires_mandatory_columns():
    record = _valid_fix_record()
    del record["CORRELATION_ID"]
    with pytest.raises(ValueError, match="Missing required"):
        fixlog.build_insert(record)


def test_fixlog_insert_validates_mode_and_outcome():
    bad_mode = _valid_fix_record()
    bad_mode["MODE"] = "YOLO"
    with pytest.raises(ValueError, match="Invalid MODE"):
        fixlog.build_insert(bad_mode)
    bad_outcome = _valid_fix_record()
    bad_outcome["OUTCOME"] = "MAYBE"
    with pytest.raises(ValueError, match="Invalid OUTCOME"):
        fixlog.build_insert(bad_outcome)


def test_recent_fix_query_shape():
    sql, params = fixlog.build_recent_fix_query()
    assert "MAX(APPLIED_AT)" in sql
    assert "TERMINAL_IDENTIFIER = %s" in sql
    assert "CHECK_COLUMN = %s" in sql
    assert params == []
