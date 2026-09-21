"""Bit 8 procedure-managed rollback: capture and run the SP's own rollback_script."""

from __future__ import annotations

import pandas as pd

from dcc_console.catalog import TEST_CATALOG
from dcc_console.execution import (
    TestResult,
    _extract_rollback_scripts,
    apply_rollback,
)
from dcc_console.rollback import restore_via_scripts

BIT8 = "Bit 8 — DCC Handler Flags (instance)"
BIT2 = "Bit 2 — Config Download Version (terminal)"


class FakeConn:
    """Connection stub exposing only execute_batch."""

    def __init__(self, rows: int = 1, fail: bool = False) -> None:
        self.batches: list[list[str]] = []
        self._rows = rows
        self._fail = fail

    def execute_batch(self, statements: list[str]) -> int:
        self.batches.append(list(statements))
        if self._fail:
            raise RuntimeError("boom")
        return self._rows


def make_result(**overrides) -> TestResult:
    defaults = dict(
        id="8-1", test_key=BIT8, bit=8, environment="UAT", login="svc",
        target_type="instance", target="I000000021", value="dccEnable", mode="LIVE",
        status="PASS", error=None, state_before="<pc/>", state_after="<pc/>",
        restore_point="<pc/>", change_persisted=False, transaction="COMMITTED",
        sql="EXEC ...", messages=[], grids=[], grid_frames=[], duration_s=0.3,
        timestamp="2026-09-21T20:00:00", sp_managed=True,
        sp_rollback_scripts=["UPDATE [cccintegrang].[handler] SET extra_config = '<x/>' WHERE 1=1"],
    )
    defaults.update(overrides)
    return TestResult(**defaults)


# --- catalogue -------------------------------------------------------------


def test_bit8_is_sp_managed_and_bit2_is_not():
    assert TEST_CATALOG[BIT8].sp_managed is True
    assert TEST_CATALOG[BIT2].sp_managed is False


def test_every_bit8_handler_flag_shares_the_sp_managed_rollback_path():
    """dccEnable, dccEnableAuth, dccEnableCompletion, ... are options on the SAME
    definition, so all inherit sp_managed — no flag gets a different rollback."""
    definition = TEST_CATALOG[BIT8]
    assert definition.sp_managed is True
    for flag in (
        "dccEnable", "dccEnableAuth", "dccEnableCompletion", "dccEnableNfc",
        "dccEnableNfcSingleTap", "dccEnableRefund", "dccFlagsEnabled",
    ):
        assert flag in definition.value_options


def test_build_call_is_flag_agnostic_for_bit8():
    """The EXEC is identical across flags: only @Extra_Config_Name changes, and
    @Config_value is always 1. Proves the rollback path cannot differ by flag."""
    from dcc_console.execution import build_call

    definition = TEST_CATALOG[BIT8]
    sql_enable, params_enable, _ = build_call(definition, "I1", "dccEnable", simulation=False)
    sql_compl, params_compl, _ = build_call(
        definition, "I1", "dccEnableCompletion", simulation=False
    )
    assert sql_enable == sql_compl  # same EXEC template
    assert params_enable[2] == "dccEnable"
    assert params_compl[2] == "dccEnableCompletion"
    assert params_enable[3] == 1 and params_compl[3] == 1  # @Config_value always 1


# --- script extraction -----------------------------------------------------


def test_extract_rollback_scripts_from_grids():
    grids = [
        pd.DataFrame([{"rollback_script": "UPDATE a", "handler_name": "h1"}]),
        pd.DataFrame([{"instance_identifier": "I1", "new_extra_config": "<x/>"}]),
    ]
    assert _extract_rollback_scripts(grids) == ["UPDATE a"]


def test_extract_handles_multiple_handler_rows_and_skips_blanks():
    grids = [pd.DataFrame([
        {"rollback_script": "UPDATE a"},
        {"rollback_script": None},
        {"rollback_script": "UPDATE b"},
    ])]
    assert _extract_rollback_scripts(grids) == ["UPDATE a", "UPDATE b"]


def test_extract_returns_empty_when_no_column():
    assert _extract_rollback_scripts([pd.DataFrame([{"foo": 1}])]) == []


# --- availability properties ----------------------------------------------


def test_has_sp_rollback_and_rollback_available():
    r = make_result()
    assert r.has_sp_rollback is True
    assert r.can_rollback is False  # generic column never moved
    assert r.rollback_available is True


def test_simulation_has_no_sp_rollback():
    assert make_result(mode="SIMULATION").has_sp_rollback is False


def test_no_scripts_means_no_sp_rollback():
    assert make_result(sp_rollback_scripts=[]).has_sp_rollback is False


# --- restore_via_scripts ---------------------------------------------------


def test_restore_via_scripts_runs_batch_and_succeeds():
    conn = FakeConn(rows=1)
    outcome = restore_via_scripts(conn, ["UPDATE a", "UPDATE b"], trigger="manual")
    assert outcome.ok is True
    assert outcome.rows == 1
    assert conn.batches == [["UPDATE a", "UPDATE b"]]


def test_restore_via_scripts_zero_rows_is_not_ok():
    outcome = restore_via_scripts(FakeConn(rows=0), ["UPDATE a"], trigger="manual")
    assert outcome.ok is False


def test_restore_via_scripts_captures_error():
    outcome = restore_via_scripts(FakeConn(fail=True), ["UPDATE a"], trigger="manual")
    assert outcome.ok is False
    assert "boom" in outcome.error


def test_restore_via_scripts_empty_list():
    outcome = restore_via_scripts(FakeConn(), [], trigger="manual")
    assert outcome.ok is False
    assert "No procedure rollback script" in outcome.error


# --- apply_rollback prefers the SP script ----------------------------------


def test_apply_rollback_prefers_sp_script():
    result = make_result(journal_id=None)
    conn = FakeConn(rows=1)
    outcome = apply_rollback(conn, TEST_CATALOG[BIT8], result, trigger="manual")
    assert outcome.ok is True
    # It ran the SP script, not a generic column-restore.
    assert conn.batches == [result.sp_rollback_scripts]
    assert "procedure script" in result.transaction
    assert result.rollback_log[-1]["ok"] is True
