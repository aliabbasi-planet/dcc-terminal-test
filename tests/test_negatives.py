"""Negative validation battery: case definitions and call construction."""

from __future__ import annotations

import json

import pandas as pd

from dcc_console.negatives import (
    _looks_rejected,
    build_negative_call,
    default_negative_cases,
)


def test_battery_has_the_expected_cases():
    names = {case.name for case in default_negative_cases()}
    assert names == {
        "Invalid instance identifier",
        "Invalid terminal identifier",
        "Invalid location number",
        "Invalid handler flag name",
        "Invalid location function name",
        "Invalid config download version",
        "Duplicate handler flag (re-apply already-set flag)",
        "Duplicate location function (re-add existing node)",
        "Missing target JSON (empty array)",
    }


def test_battery_covers_invalid_duplicate_and_nonexistent():
    categories = {case.category for case in default_negative_cases()}
    # CAB point 7: all three scenario families must be exercised.
    assert {"non-existent", "invalid", "duplicate"} <= categories


def test_duplicate_cases_target_valid_rows():
    duplicates = [c for c in default_negative_cases() if c.category == "duplicate"]
    assert len(duplicates) >= 2
    # A duplicate must re-apply to a real target, not a bogus id.
    assert all("9999" not in case.target for case in duplicates)


def test_empty_payload_case_sends_an_empty_json_array():
    case = next(c for c in default_negative_cases() if c.empty_payload)
    _, params, rendered = build_negative_call(case)
    assert params[1] == "[]"
    assert "'[]'" in rendered


def test_function_override_replaces_the_extra_function_name():
    case = next(c for c in default_negative_cases() if c.function_override)
    _, params, rendered = build_negative_call(case)
    # Bit 1 params = (bit, payload, function_name, add, sim)
    assert params[2] == "DCCNotARealFunction"
    assert "DCCNotARealFunction" in rendered


def test_every_case_builds_a_parameterised_call():
    for case in default_negative_cases():
        sql, params, _ = build_negative_call(case)
        assert sql.count("?") == len(params)
        # The target id is always bound inside the JSON payload, never inlined.
        assert case.target not in sql


def test_invalid_targets_travel_as_json():
    case = next(c for c in default_negative_cases() if c.name == "Invalid instance identifier")
    _, params, _ = build_negative_call(case)
    assert json.loads(params[1]) == [{"instance_identifier": "I000099999"}]


def test_looks_rejected_detects_error_markers():
    assert _looks_rejected(["Msg 50000: instance does not exist"], [])
    assert _looks_rejected([], [pd.DataFrame([{"result": "ERROR: invalid flag"}])])
    assert not _looks_rejected(["proposing dccEnable=true"], [])
