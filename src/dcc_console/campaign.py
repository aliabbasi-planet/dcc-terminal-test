"""Campaign planning and factual reporting for terminal configuration tests."""

from __future__ import annotations

from collections import Counter

from .catalog import TEST_CATALOG, TestDefinition
from .execution import TestResult


def campaign_tests() -> tuple[TestDefinition, ...]:
    """Return every procedure-backed test that can be run in a campaign."""
    return tuple(TEST_CATALOG.values())


def terminal_tests() -> tuple[TestDefinition, ...]:
    """Return only tests that can safely be applied to an EMV terminal."""
    return tuple(
        definition for definition in TEST_CATALOG.values() if definition.target == "terminal"
    )


def campaign_results(results: list[TestResult], campaign_id: str) -> list[TestResult]:
    return [result for result in results if result.campaign_id == campaign_id]


def campaign_summary(results: list[TestResult]) -> dict[str, int]:
    counts = Counter(result.status for result in results)
    return {
        "total": len(results),
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "review": counts["REVIEW"],
        "blocked": counts["BLOCKED"],
        "awaiting_rollback": sum(result.can_rollback for result in results),
    }


def campaign_markdown(name: str, results: list[TestResult]) -> str:
    """Build a portable factual report for one campaign."""
    summary = campaign_summary(results)
    lines = [
        f"# DCC Configuration Campaign: {name}",
        "",
        "| Total | Passed | Failed | Review | Blocked | Awaiting rollback |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {summary['total']} | {summary['passed']} | {summary['failed']} | "
            f"{summary['review']} | {summary['blocked']} | {summary['awaiting_rollback']} |"
        ),
        "",
        "| Target type | Target | Test | Value | Mode | Status | Persisted | Rollback available |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.target_type} | {result.target} | {result.test_key} | {result.value} | "
            f"{result.mode} | {result.status} | {result.change_persisted} | "
            f"{result.can_rollback} |"
        )
    return "\n".join(lines)