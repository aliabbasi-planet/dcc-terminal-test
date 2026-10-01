"""Rollback availability and exported result state."""

from __future__ import annotations

from dcc_console.catalog import TEST_CATALOG
from dcc_console.execution import TestResult
from dcc_console.rollback import restore_state

TEST_KEY = "Bit 2 — Config Download Version (terminal)"


def make_result(**overrides) -> TestResult:
    defaults = dict(
        id="2-120000000000",
        test_key=TEST_KEY,
        bit=2,
        environment="UAT",
        login="svc-test",
        target_type="terminal",
        target="T-1",
        value="Standard",
        mode="LIVE",
        status="PASS",
        error=None,
        state_before="1",
        state_after="2",
        restore_point="1",
        change_persisted=True,
        transaction="COMMITTED",
        sql="EXEC ...",
        messages=[],
        grids=[],
        grid_frames=[],
        duration_s=0.5,
        timestamp="2026-09-11T10:00:00",
    )
    defaults.update(overrides)
    return TestResult(**defaults)


class TestRollbackAvailability:
    def test_live_persisted_change_can_roll_back(self):
        assert make_result().can_rollback is True

    def test_simulation_never_offers_rollback(self):
        assert make_result(mode="SIMULATION").can_rollback is False

    def test_no_restore_point_blocks_rollback(self):
        assert make_result(restore_point=None).can_rollback is False

    def test_nothing_persisted_means_nothing_to_undo(self):
        assert make_result(change_persisted=False).can_rollback is False

    def test_failed_live_run_still_offers_rollback_when_row_changed(self):
        assert make_result(status="FAIL", error="boom").can_rollback is True


def test_export_drops_dataframes_and_reports_rollback_state():
    payload = make_result().export()
    assert "grid_frames" not in payload
    assert payload["can_rollback"] is True


class RestoreConnection:
    def __init__(self, value="1", fail=False):
        self.value = value
        self.fail = fail
        self.writes = []

    def execute_write(self, sql, params):
        if self.fail:
            raise RuntimeError("restore failed")
        self.writes.append((sql, params))
        return 1

    def scalar(self, sql, params):
        return self.value


def test_restore_state_writes_and_verifies_original_value():
    connection = RestoreConnection(value="1")
    outcome = restore_state(
        connection,
        TEST_CATALOG[TEST_KEY],
        "T-1",
        "1",
        trigger="manual",
    )
    assert outcome.ok is True
    assert outcome.rows == 1
    assert outcome.trigger == "manual"
    assert connection.writes[0][1] == ("1", "T-1")


def test_restore_state_reports_write_failure():
    outcome = restore_state(
        RestoreConnection(fail=True),
        TEST_CATALOG[TEST_KEY],
        "T-1",
        "1",
        trigger="automatic",
    )
    assert outcome.ok is False
    assert outcome.rows == 0
    assert outcome.trigger == "automatic"
    assert "restore failed" in outcome.error
