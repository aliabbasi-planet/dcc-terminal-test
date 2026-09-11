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


def test_bits_are_unique():
    bits = [definition.bit for definition in TEST_CATALOG.values()]
    assert len(bits) == len(set(bits))
