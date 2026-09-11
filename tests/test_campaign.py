"""Campaign planning and reporting behavior."""

from __future__ import annotations

from dcc_console.campaign import (
    campaign_markdown,
    campaign_results,
    campaign_summary,
    campaign_tests,
    terminal_tests,
)
from dcc_console.execution import TestResult


def make_result(**overrides) -> TestResult:
    defaults = {
        "id": "test-1",
        "test_key": "Bit 2 — Config Download Version (terminal)",
        "bit": 2,
        "environment": "DEV",
        "login": "tester",
        "target_type": "terminal",
        "target": "T-1",
        "value": "Standard",
        "mode": "SIMULATION",
        "status": "PASS",
        "error": None,
        "state_before": "1",
        "state_after": "1",
        "restore_point": "1",
        "change_persisted": False,
        "transaction": "ROLLED BACK (simulation)",
        "sql": "EXEC ...",
        "messages": [],
        "grids": [],
        "grid_frames": [],
        "duration_s": 0.1,
        "timestamp": "2026-09-11T12:00:00",
        "campaign_id": "campaign-1",
    }
    defaults.update(overrides)
    return TestResult(**defaults)


def test_terminal_campaigns_only_offer_terminal_tests():
    assert {definition.bit for definition in terminal_tests()} == {2, 4}


def test_campaigns_offer_every_supported_configuration_test():
    assert {definition.bit for definition in campaign_tests()} == {1, 2, 4, 8, 16}


def test_campaign_results_excludes_other_campaigns():
    matching = make_result()
    other = make_result(id="test-2", campaign_id="campaign-2")
    assert campaign_results([matching, other], "campaign-1") == [matching]


def test_campaign_summary_counts_statuses_and_pending_rollbacks():
    live = make_result(
        id="test-2",
        mode="LIVE",
        status="FAIL",
        change_persisted=True,
        state_after="2",
    )
    summary = campaign_summary([make_result(), live])
    assert summary == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "review": 0,
        "blocked": 0,
        "awaiting_rollback": 1,
    }


def test_campaign_markdown_names_each_terminal_and_status():
    report = campaign_markdown("Pilot", [make_result(target="T-42")])
    assert "# DCC Configuration Campaign: Pilot" in report
    assert "T-42" in report
    assert "PASS" in report