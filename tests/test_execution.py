"""Statement construction and status classification."""

from __future__ import annotations

import json

import pytest

from dcc_console.catalog import TEST_CATALOG, TEST_KEYS
from dcc_console.execution import build_call, classify
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
