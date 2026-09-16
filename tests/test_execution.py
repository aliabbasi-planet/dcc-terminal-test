"""Statement construction and status classification."""

from __future__ import annotations

import json

import pytest

from dcc_console.catalog import TEST_CATALOG, TEST_KEYS
from dcc_console.execution import TestResult, build_call, classify
from dcc_console.rollback import restore_statement


@pytest.mark.parametrize("key", TEST_KEYS)
def test_build_call_is_fully_parameterised(key):
    definition = TEST_CATALOG[key]
    value = definition.value_options[0] if definition.value_options else "SAMPLE"
    sql, params, _ = build_call(definition, "TARGET-1", value, simulation=True)

    assert sql.count("?") == len(params)
    assert "TARGET-1" not in sql  # the identifier is bound, never inlined


@pytest.mark.parametrize("key", TEST_KEYS)
def test_target_travels_as_json_parameter(key):
    definition = TEST_CATALOG[key]
    value = definition.value_options[0] if definition.value_options else "SAMPLE"
    _, params, _ = build_call(definition, "TARGET-1", value, simulation=True)

    payload = json.loads(params[1])
    assert payload == [{definition.json_field: "TARGET-1"}]


@pytest.mark.parametrize("simulation,expected_flag", [(True, 1), (False, 0)])
def test_simulation_flag_is_last_parameter(simulation, expected_flag):
    definition = TEST_CATALOG["Bit 2 — Config Download Version (terminal)"]
    _, params, _ = build_call(definition, "T1", "Standard", simulation=simulation)
    assert params[-1] == expected_flag


def test_injection_attempt_stays_inside_a_parameter():
    definition = TEST_CATALOG["Bit 2 — Config Download Version (terminal)"]
    hostile = "x'; DROP TABLE users; --"
    sql, params, _ = build_call(definition, hostile, "Standard", simulation=True)

    assert "DROP TABLE" not in sql
    assert hostile in params[1]


def test_bit1_add_and_remove_map_to_flags():
    definition = TEST_CATALOG["Bit 1 — DCC Xpress CO (location extra_function)"]
    _, add_params, _ = build_call(definition, "L1", "Add", simulation=True)
    _, remove_params, _ = build_call(definition, "L1", "Remove", simulation=True)
    assert add_params[3] == 1
    assert remove_params[3] == 0


@pytest.mark.parametrize(
    "key,expected_function",
    [
        ("Bit 1 — DCC Xpress CO (location extra_function)", "DCCXpressCO"),
        (
            "Bit 1 — DCC Xpress CO Delayed Terminal (location extra_function)",
            "DCCXpressCODT",
        ),
        ("Bit 1 — DCC Xpress CO Fallback (location extra_function)", "DCCXpressCOFallback"),
    ],
)
def test_bit1_function_name_flows_into_extra_function_name(key, expected_function):
    definition = TEST_CATALOG[key]
    _, params, rendered = build_call(definition, "L1", "Add", simulation=True)
    # params = (bit, payload, function_name, add, sim)
    assert params[2] == expected_function
    assert expected_function in rendered


@pytest.mark.parametrize("flag", ["dccEnableRefund", "dccFlagsEnabled"])
def test_new_bit8_flags_build_a_call(flag):
    definition = TEST_CATALOG["Bit 8 — DCC Handler Flags (instance)"]
    _, params, rendered = build_call(definition, "I1", flag, simulation=True)
    # params = (bit, payload, flag, config_value=1, sim)
    assert params[2] == flag
    assert params[3] == 1
    assert flag in rendered


def _result(**overrides) -> TestResult:
    defaults = dict(
        id="x", test_key="t", bit=8, environment="DEV", login="svc",
        target_type="instance", target="I1", value="dccEnable", mode="SIMULATION",
        status="PASS", error=None, state_before="1", state_after="1", restore_point="1",
        change_persisted=False, transaction="ROLLED BACK (simulation)", sql="EXEC ...",
        messages=[], grids=[], grid_frames=[], duration_s=0.1, timestamp="2026-01-01T00:00:00",
    )
    defaults.update(overrides)
    return TestResult(**defaults)


class TestVerdictVocabulary:
    def test_simulation_pass_is_simulated_ok_not_applied(self):
        r = _result(mode="SIMULATION", status="PASS")
        assert r.verdict_code == "SIMULATED-OK"
        assert "not that a config changed" in r.verdict_detail

    def test_live_persisted_change_is_applied(self):
        r = _result(mode="LIVE", status="PASS", change_persisted=True)
        assert r.verdict_code == "APPLIED"

    def test_blocked_maps_to_blocked(self):
        r = _result(status="BLOCKED", error="EXECUTE permission was denied")
        assert r.verdict_code == "BLOCKED"

    def test_negative_rejected_is_rejected_as_expected(self):
        r = _result(is_negative=True, status="PASS", error="not found")
        assert r.verdict_code == "REJECTED-AS-EXPECTED"

    def test_negative_not_rejected_is_flagged(self):
        r = _result(is_negative=True, status="REVIEW", error=None)
        assert r.verdict_code == "NOT-REJECTED"

    def test_export_includes_verdict(self):
        payload = _result().export()
        assert payload["verdict_code"] == "SIMULATED-OK"
        assert "verdict_detail" in payload
        assert "grid_frames" not in payload


class TestClassify:
    def test_permission_error_is_blocked(self):
        error = "42000 ... The EXECUTE permission was denied on the object 'fnDisplayTrace'"
        assert classify(True, error, False) == "BLOCKED"

    def test_other_errors_fail(self):
        assert classify(True, "Timeout expired", False) == "FAIL"

    def test_clean_simulation_passes(self):
        assert classify(True, None, False) == "PASS"

    def test_simulation_that_persists_fails(self):
        assert classify(True, None, True) == "FAIL"

    def test_live_change_passes(self):
        assert classify(False, None, True) == "PASS"

    def test_live_no_op_needs_review(self):
        assert classify(False, None, False) == "REVIEW"


@pytest.mark.parametrize("key", TEST_KEYS)
def test_restore_statement_binds_value_and_key(key):
    definition = TEST_CATALOG[key]
    sql = restore_statement(definition)

    assert sql.count("?") == 2
    assert sql.startswith(f"UPDATE {definition.verify.table}")
    assert definition.verify.cast in sql
