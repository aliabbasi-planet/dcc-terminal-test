"""Rollback availability and exported result state."""

from __future__ import annotations

from dcc_console.execution import TestResult

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
