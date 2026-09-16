"""Catalogue integrity: the declarations the security model depends on."""

from __future__ import annotations

import pytest

from dcc_console.catalog import TEST_CATALOG, TEST_KEYS

ALLOWED_CASTS = {"xml", "tinyint", "varchar(200)"}
ALLOWED_TARGETS = {"location", "terminal", "instance"}


def test_catalog_is_not_empty():
    assert TEST_KEYS
    assert set(TEST_KEYS) == set(TEST_CATALOG)


@pytest.mark.parametrize("key", TEST_KEYS)
def test_every_definition_is_complete(key):
    definition = TEST_CATALOG[key]
    assert definition.bit in {1, 2, 4, 8, 16}
    assert definition.target in ALLOWED_TARGETS
    assert definition.proc_args
    assert definition.summary
    assert definition.business_meaning
    assert definition.mechanism


@pytest.mark.parametrize("key", TEST_KEYS)
def test_verified_column_is_restorable(key):
    verify = TEST_CATALOG[key].verify
    assert verify.table.startswith("[")
    assert verify.column
    assert verify.key
    assert verify.cast in ALLOWED_CASTS


@pytest.mark.parametrize("key", TEST_KEYS)
def test_json_field_matches_target(key):
    definition = TEST_CATALOG[key]
    expected = {
        "location": "location_no",
        "terminal": "terminal_identifier",
        "instance": "instance_identifier",
    }[definition.target]
    assert definition.json_field == expected


def test_keys_are_unique():
    keys = [definition.key for definition in TEST_CATALOG.values()]
    assert len(keys) == len(set(keys))


def test_bit1_entries_carry_distinct_function_names():
    bit1 = [d for d in TEST_CATALOG.values() if d.bit == 1]
    functions = {d.function_name for d in bit1}
    assert functions == {"DCCXpressCO", "DCCXpressCODT", "DCCXpressCOFallback"}
    # Only Bit 1 (location extra_function) tests carry a function name.
    assert all(d.function_name is None for d in TEST_CATALOG.values() if d.bit != 1)


def test_bit8_covers_refund_and_flags_enabled():
    bit8 = TEST_CATALOG["Bit 8 — DCC Handler Flags (instance)"]
    assert "dccEnableRefund" in bit8.value_options
    assert "dccFlagsEnabled" in bit8.value_options


def test_inferred_areas_carry_owner_confirmation_assumptions():
    # DT / Fallback / the Bit 8 extra flags are inferred and must be flagged.
    dt = TEST_CATALOG["Bit 1 — DCC Xpress CO Delayed Terminal (location extra_function)"]
    fallback = TEST_CATALOG["Bit 1 — DCC Xpress CO Fallback (location extra_function)"]
    bit8 = TEST_CATALOG["Bit 8 — DCC Handler Flags (instance)"]
    assert dt.assumption and "confirm" in dt.assumption.lower()
    assert fallback.assumption and "confirm" in fallback.assumption.lower()
    assert bit8.assumption and "dccFlagsEnabled" in bit8.assumption
