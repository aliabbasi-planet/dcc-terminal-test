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
    parse_schema_fqn,
    snapshot,
    suggest,
)
from dcc_console.remediation import analytics as an
from dcc_console.remediation import worklist as wl

# A concrete tenant used across builder tests, plus a *different* one to prove the
# schema is not hardcoded anywhere, plus one that uses the team's shared fix log.
OBJS = RemediationObjects("DEV_CORE_AAB")
OTHER = RemediationObjects("DEV_CORE_XYZ", "MY_SCHEMA")
SHARED = RemediationObjects("DEV_CORE_XYZ", "MY_SCHEMA", "DEV_CORE_AAB", "DCC_REMEDIATION")

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


def test_objects_without_shared_schema_keep_the_log_in_their_own_schema():
    assert OTHER.shared_fqn == OTHER.schema_fqn
    assert OTHER.uses_shared_log is False
    assert OTHER.operators_table == "DEV_CORE_XYZ.MY_SCHEMA.FIX_OPERATORS"


def test_objects_with_shared_schema_route_only_log_and_allowlist_there():
    assert SHARED.snapshot_table == "DEV_CORE_XYZ.MY_SCHEMA.HEALTH_DAILY_SNAPSHOT"
    assert SHARED.v_current_broken == "DEV_CORE_XYZ.MY_SCHEMA.V_CURRENT_BROKEN"
    assert SHARED.fix_log_table == "DEV_CORE_AAB.DCC_REMEDIATION.APP_FIX_LOG"
    assert SHARED.operators_table == "DEV_CORE_AAB.DCC_REMEDIATION.FIX_OPERATORS"
    assert SHARED.uses_shared_log is True
    with pytest.raises(ValueError):
        RemediationObjects("DEV_CORE_AAB", "DCC_REMEDIATION", "bad;db", "X")


def test_parse_schema_fqn_validates_both_parts():
    assert parse_schema_fqn(" DEV_CORE_AAB.DCC_REMEDIATION ") == ("DEV_CORE_AAB", "DCC_REMEDIATION")
    for bad in ("DEV_CORE_AAB", "a.b.c", "a;b.c", ".x", ""):
        with pytest.raises(ValueError):
            parse_schema_fqn(bad)


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
    assert "DCCFLAGSENABLED_CHECK_C" in mapping.FIXABLE_CHECK_COLUMNS
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


def test_worklist_exact_identifier_filters_are_bound_and_trimmed():
    filters = wl.WorklistFilters(
        remediation_state=None,
        identifiers={"LOCATION_NO": " L1 ", "INSTANCE_IDENTIFIER": " I1 "},
    )
    sql, params = wl.build_worklist_query(filters, OBJS)
    assert "INSTANCE_IDENTIFIER = %s" in sql
    assert "LOCATION_NO = %s" in sql
    assert params == ["I1", "L1"]


def test_worklist_identifier_filter_rejects_unknown_column():
    filters = wl.WorklistFilters(identifiers={"DROP_TABLE": "x"})
    with pytest.raises(ValueError, match="Unknown identifier column"):
        wl.build_worklist_query(filters, OBJS)


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


def test_last_live_outcome_query_shape():
    sql = fixlog.build_last_live_outcome_query(OBJS)
    assert "TERMINAL_IDENTIFIER = %s" in sql
    assert "CHECK_COLUMN = %s" in sql
    assert "ENVIRONMENT = %s" in sql
    assert "OUTCOME IN ('APPLIED', 'ROLLED_BACK')" in sql  # a rollback re-opens a fix
    assert sql.strip().endswith("LIMIT 1")


def test_fixlog_accepts_new_outcomes_and_audit_columns():
    for outcome in ("SIMULATED", "NOT_FOUND", "SKIPPED_ALREADY_OK", "ROLLED_BACK"):
        record = {**_valid_fix_record(), "OUTCOME": outcome, "SQL_LOGIN": "svc", "NOTES": "n"}
        sql, params = fixlog.build_insert(record, OBJS)
        assert "SQL_LOGIN" in sql and "NOTES" in sql
        assert outcome in params


def test_fixlog_history_and_operator_queries_use_the_shared_log():
    history = fixlog.build_terminal_history_query(SHARED, limit=10)
    assert SHARED.fix_log_table in history
    assert "TERMINAL_IDENTIFIER = %s" in history
    assert history.strip().endswith("LIMIT 10")
    operator = fixlog.build_operator_check_query(SHARED)
    assert SHARED.operators_table in operator
    assert "CURRENT_USER()" in operator  # identity comes from the SSO session


# --------------------------------------------------------------------------- #
# ddl (per-user schema + shared fix log)
# --------------------------------------------------------------------------- #


def test_ddl_initialise_covers_all_objects_in_dependency_order():
    statements = ddl.build_initialise_statements(OBJS)
    joined = "\n".join(statements)
    assert f"CREATE SCHEMA IF NOT EXISTS {OBJS.schema_fqn}" in joined
    for table in ("HEALTH_DAILY_SNAPSHOT", "APP_FIX_LOG", "FLAG_REFERENCE", "FIX_OPERATORS"):
        assert f"{OBJS.schema_fqn}.{table}" in joined
    for view in ("V_CURRENT_BROKEN", "V_FIX_HISTORY", "V_REMEDIATION_KPIS"):
        assert f"{OBJS.schema_fqn}.{view}" in joined
    for col in mapping.TRACKED_CHECK_COLUMNS:
        assert f"{col} INTEGER" in joined
    # The fix log must exist before the views that read it.
    log_at = next(
        i for i, s in enumerate(statements) if "TABLE IF NOT EXISTS" in s and "APP_FIX_LOG" in s
    )
    view_at = next(i for i, s in enumerate(statements) if "V_CURRENT_BROKEN AS" in s)
    assert log_at < view_at
    assert ddl.build_initialise_statements(OBJS, include_shared=False) == (
        ddl.build_create_statements(OBJS)
    )


def test_ddl_uses_given_schema_only():
    joined = "\n".join(ddl.build_initialise_statements(OTHER))
    assert "DEV_CORE_XYZ.MY_SCHEMA" in joined
    assert "DEV_CORE_AAB" not in joined


def test_ddl_shared_log_is_read_by_own_views_and_created_only_in_shared_schema():
    own = "\n".join(ddl.build_create_statements(SHARED))
    assert f"{SHARED.schema_fqn}.V_CURRENT_BROKEN" in own
    assert SHARED.fix_log_table in own  # views read the team log
    assert "TABLE IF NOT EXISTS DEV_CORE_AAB" not in own
    shared = "\n".join(ddl.build_shared_statements(SHARED))
    assert SHARED.fix_log_table in shared and SHARED.operators_table in shared
    assert "DEV_CORE_XYZ" not in shared


def test_ddl_upgrades_existing_fix_log_and_seeds_first_operator():
    shared = ddl.build_shared_statements(OBJS)
    for name, _ in ddl.FIX_LOG_ADDED_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {name}" in "\n".join(shared)
    seed = next(s for s in shared if s.startswith("INSERT INTO"))
    assert "CURRENT_USER()" in seed and "WHERE NOT EXISTS" in seed


def test_view_reads_only_the_latest_snapshot_date():
    view = next(s for s in ddl.build_create_statements(OBJS) if "V_CURRENT_BROKEN AS" in s)
    assert "SNAPSHOT_DATE = (SELECT MAX(SNAPSHOT_DATE)" in view
    assert "ORDER BY SNAPSHOT_DATE DESC" not in view  # no per-terminal "latest row" fallback


def test_view_reads_the_fix_registry_for_prod_resolutions():
    view = next(s for s in ddl.build_create_statements(OBJS) if "V_CURRENT_BROKEN AS" in s)
    # Suppression is now driven by the durable registry (survives the daily refresh and
    # is deleted on rollback), scoped to PROD/FIXED and guarded by the snapshot's
    # source-load time so a terminal re-broken in a later load resurfaces.
    assert OBJS.registry_table in view
    assert "g.ENVIRONMENT = 'PROD'" in view and "g.STATUS IN ('FIXED', 'CONFIRMED')" in view
    assert "g.FIXED_AT_UTC > l.SOURCE_LAST_ALTERED" in view
    for col in mapping.FIXABLE_CHECK_COLUMNS:
        assert f"ARRAY_CONTAINS('{col}'::VARIANT, FIXED_CHECKS)" in view
    assert ddl.VIEW_MARKER_COLUMN in view


def test_kpis_separate_prod_fixes_from_rehearsals():
    kpis = next(s for s in ddl.build_create_statements(OBJS) if "V_REMEDIATION_KPIS AS" in s)
    assert "AS TOTAL_LIVE_FIXES" in kpis and "AS REHEARSAL_FIXES" in kpis
    assert "AS FIXABLE_TERMINALS" in kpis
    assert "AS ALREADY_OK_PROD" in kpis and "AS PROD_SIMULATIONS" in kpis
    assert "AS LIVE_APPLIED_UAT_DEV" in kpis
    assert "OUTCOME = 'SKIPPED_ALREADY_OK'" in kpis
    assert "MODE = 'SIMULATION' AND ENVIRONMENT IN ('UAT', 'DEV')" in kpis


def test_fix_activity_separates_prod_outcomes_from_uat_dev_dry_runs():
    query = an.build_fix_activity_query(OBJS)
    assert "AS LIVE_APPLIED_PROD" in query
    assert "AS ALREADY_OK_PROD" in query
    assert "AS DRY_RUN_PROD" in query
    assert "AS DRY_RUN_UAT" in query
    assert "AS DRY_RUN_DEV" in query
    assert "AS LIVE_APPLIED_UAT_DEV" in query
    assert "SELECT DISTINCT CAST(APPLIED_AT AS DATE)" in query
    assert "CORRELATION_ID, APPLIED_AT, MODE, ENVIRONMENT, OUTCOME" in query


def test_fix_coverage_counts_unique_targets_and_terminals_by_registry_status():
    query = an.build_fix_coverage_query(OBJS)
    assert OBJS.registry_table in query
    assert "COUNT(DISTINCT TARGET_IDENTIFIER) AS REGISTERED_TARGETS" in query
    assert "COUNT(DISTINCT IFF(STATUS = 'CONFIRMED'" in query
    assert "COUNT(DISTINCT TERMINAL_IDENTIFIER) AS TERMINALS_COVERED" in query
    assert "ENVIRONMENT = 'PROD' AND STATUS IN ('FIXED', 'CONFIRMED')" in query


def test_bit_fix_coverage_separates_calls_registry_checks_and_terminals():
    query = an.build_bit_fix_coverage_query(OBJS)
    assert "FROM VALUES (1), (2), (8), (16)" in query
    assert "AS LIVE_APPLIED_CALLS" in query
    assert "AS ALREADY_OK_CALLS" in query
    assert "AS DRY_RUN_ATTEMPTS" in query
    assert "AS REGISTERED_CHECKS" in query
    assert "AS PENDING_CONFIRMATION_CHECKS" in query
    assert "AS CONFIRMED_CHECKS" in query
    assert "AS TERMINALS_COVERED" in query


def test_snapshot_stores_source_load_time_in_utc():
    merge = snapshot.build_refresh_merge(OBJS)
    assert "CONVERT_TIMEZONE('UTC', LAST_ALTERED)::TIMESTAMP_NTZ" in merge


def test_ddl_seed_has_all_flags_from_mapping():
    seed = ddl.build_seed_merge(OBJS)
    assert OBJS.flag_reference_table in seed
    for fix in mapping.FLAG_FIXES:
        assert f"'{fix.check_column}'" in seed
    # Value spelling comes from mapping (catalog spelling), not the old seed file.
    assert "'DCCXpressCOFallback'" in seed


def test_ddl_objects_present_query_checks_own_and_shared_schemas():
    sql = ddl.build_objects_present_query(SHARED)
    assert "DEV_CORE_XYZ.INFORMATION_SCHEMA.TABLES" in sql
    assert "TABLE_SCHEMA = 'MY_SCHEMA'" in sql
    assert "DEV_CORE_AAB.INFORMATION_SCHEMA.TABLES" in sql
    assert "TABLE_SCHEMA = 'DCC_REMEDIATION'" in sql
    for table in ddl.OWN_TABLES + ddl.SHARED_TABLES:
        assert f"'{table}'" in sql
    assert "DCC_FIX_REGISTRY" in sql  # the durable registry is part of readiness
    assert "DCC_CONFIRMED_FIXES" in sql  # the profit handoff table too
    assert "TABLE_NAME = 'DCC_FIX_REGISTRY'" in sql and "'CONFIRMED_AT_UTC'" in sql
    assert "TABLE_NAME = 'FIX_OPERATORS'" in sql and "'CAN_PROD'" in sql
    assert f"COLUMN_NAME = '{ddl.VIEW_MARKER_COLUMN}'" in sql


@pytest.mark.parametrize(
    ("row", "ready", "missing"),
    [
        (
            {
                "OWN_TABLES": 2,
                "SHARED_TABLES": 4,
                "FIX_LOG_COLUMNS": 3,
                "OPERATOR_COLUMNS": 1,
                "REGISTRY_COLUMNS": 1,
                "VIEW_CURRENT": 1,
            },
            True,
            0,
        ),
        # The 2026-09-28 schema: tables exist, but no allowlist/registry, new columns or view.
        (
            {
                "OWN_TABLES": 2,
                "SHARED_TABLES": 1,
                "FIX_LOG_COLUMNS": 0,
                "OPERATOR_COLUMNS": 0,
                "REGISTRY_COLUMNS": 0,
                "VIEW_CURRENT": 0,
            },
            False,
            2,
        ),
        # Fix log present, but the operator allowlist (CAN_PROD) and registry (CONFIRMED_AT_UTC)
        # upgrades are missing, and the handoff table is absent.
        (
            {
                "OWN_TABLES": 2,
                "SHARED_TABLES": 3,
                "FIX_LOG_COLUMNS": 3,
                "OPERATOR_COLUMNS": 0,
                "REGISTRY_COLUMNS": 0,
                "VIEW_CURRENT": 1,
            },
            False,
            1,
        ),
        (
            {
                "OWN_TABLES": 0,
                "SHARED_TABLES": 0,
                "FIX_LOG_COLUMNS": 0,
                "OPERATOR_COLUMNS": 0,
                "REGISTRY_COLUMNS": 0,
                "VIEW_CURRENT": 0,
            },
            False,
            3,
        ),
        ({"OWN_TABLES": None}, False, 3),
    ],
)
def test_schema_status(row, ready, missing):
    status = ddl.schema_status(row)
    assert status.ready is ready
    assert len(status.missing) == missing


# --------------------------------------------------------------------------- #
# shared-target worklist + Bit 16 suggestions
# --------------------------------------------------------------------------- #


def test_shared_target_query_validates_and_binds():
    sql = wl.build_shared_target_query("INSTANCE_IDENTIFIER", "HANDLER_DCCENABLE_CHECK_O", OBJS)
    assert "INSTANCE_IDENTIFIER = %s AND HANDLER_DCCENABLE_CHECK_O = 1" in sql
    with pytest.raises(ValueError):
        wl.build_shared_target_query("MERCHANT_NAME; DROP", "HANDLER_DCCENABLE_CHECK_O", OBJS)
    with pytest.raises(ValueError):
        wl.build_shared_target_query("LOCATION_NO", "NOT_A_CHECK", OBJS)


def test_template_suggestions_bind_profile_and_skip_missing_levels():
    full = suggest.build_template_suggestion_queries(
        {"TERMINAL_BRAND_NAME": "PAX", "TERMINAL_MODEL_NAME": "A920", "ACQUIRER_NAME": "Elavon"}
    )
    assert [len(q.params) for q in full] == [3, 2]
    assert full[0].params == ["PAX", "A920", "Elavon"]
    assert "%s" in full[0].sql and "PAX" not in full[0].sql
    no_acquirer = suggest.build_template_suggestion_queries(
        {"TERMINAL_BRAND_NAME": "PAX", "TERMINAL_MODEL_NAME": "A920", "ACQUIRER_NAME": None}
    )
    assert [len(q.params) for q in no_acquirer] == [2]
    assert suggest.build_template_suggestion_queries({}) == []
    assert MAINTENANCE_SOURCE in suggest.build_known_templates_query()


def test_template_suggestion_summary_has_shares():
    import pandas as pd

    frame = pd.DataFrame({"TEMPLATE": ["Planet_PAX", "Six_PAX"], "TERMINALS": [95, 5]})
    ranked = suggest.summarise(frame)
    assert ranked[0].template == "Planet_PAX" and ranked[0].share == pytest.approx(0.95)
    assert suggest.summarise(None) == []
