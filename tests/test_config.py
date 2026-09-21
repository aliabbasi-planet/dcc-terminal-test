"""Environment configuration safety checks and config-download version mapping."""

from __future__ import annotations

from dcc_console.catalog import TEST_CATALOG
from dcc_console.config import (
    CONFIGDOWNLOAD_VERSIONS,
    ENVIRONMENTS,
    configdownload_version_text,
)


def test_dev_uat_and_prod_are_available():
    assert set(ENVIRONMENTS) == {"DEV", "UAT", "PROD"}


def test_prod_requires_explicit_confirmation_for_live_runs():
    assert ENVIRONMENTS["PROD"].requires_typed_confirmation is True


# --- Config download version mapping (CAB point 8: 1=Standard, 2=ECB DCC) ---


def test_version_map_is_standard_and_ecb_dcc():
    assert CONFIGDOWNLOAD_VERSIONS == {1: "Standard", 2: "ECB DCC"}


def test_version_text_labels_known_codes():
    assert configdownload_version_text(1) == "1 (Standard)"
    assert configdownload_version_text(2) == "2 (ECB DCC)"
    assert configdownload_version_text("1") == "1 (Standard)"


def test_version_text_flags_unknown_and_null():
    assert configdownload_version_text(None) == "(none)"
    assert configdownload_version_text("") == "(none)"
    assert "unknown version" in configdownload_version_text(7)
    assert "unrecognised" in configdownload_version_text("abc")


def test_bit2_options_are_standard_and_ecb_dcc():
    definition = TEST_CATALOG["Bit 2 — Config Download Version (terminal)"]
    assert definition.value_options == ("Standard", "ECB DCC")
    assert "Enhanced" not in definition.value_options
    assert "Legacy" not in definition.value_options
