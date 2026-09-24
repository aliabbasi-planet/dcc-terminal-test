"""Coverage matrix computation over run results."""

from __future__ import annotations

from dcc_console.coverage import (
    CONFIG_AREAS,
    compute_coverage,
    coverage_totals,
)
from dcc_console.execution import TestResult


def mk(**overrides) -> TestResult:
    defaults = dict(
        id="x", test_key="t", bit=8, environment="DEV", login="svc",
        target_type="instance", target="I1", value="dccEnable", mode="SIMULATION",
        status="PASS", error=None, state_before="1", state_after="1", restore_point="1",
        change_persisted=False, transaction="ROLLED BACK (simulation)", sql="EXEC ...",
        messages=[], grids=[], grid_frames=[], duration_s=0.1, timestamp="2026-01-01T00:00:00",
    )
    defaults.update(overrides)
    return TestResult(**defaults)


def _row(rows, area_id):
    return next(r for r in rows if r.area.id == area_id)


def test_registry_has_thirteen_individual_areas():
    assert len(CONFIG_AREAS) == 13


def test_empty_run_marks_everything_not_tested():
    rows = compute_coverage([])
    assert all(r.symbol == "❌" for r in rows)
    assert coverage_totals(rows)["not_tested"] == 13


def test_each_bit8_flag_is_its_own_coverage_row():
    results = [
        mk(value="dccEnable"),
        mk(value="dccEnableAuth"),
        mk(value="dccEnableRefund"),
    ]
    rows = compute_coverage(results)
    assert _row(rows, "bit8_dccEnable").tested
    assert _row(rows, "bit8_dccEnableAuth").tested
    assert _row(rows, "bit8_dccEnableRefund").tested
    assert not _row(rows, "bit8_dccEnableCompletion").tested
    assert not _row(rows, "bit8_dccEnableNfc").tested
    assert not _row(rows, "bit8_dccFlagsEnabled").tested


def test_location_functions_are_scored_separately():
    results = [
        mk(test_key="Bit 1 — DCC Xpress CO (location extra_function)", bit=1,
           target_type="location", target="0000000", value="Add"),
        mk(test_key="Bit 1 — DCC Xpress CO Delayed Terminal (location extra_function)", bit=1,
           target_type="location", target="0000000", value="Add"),
    ]
    rows = compute_coverage(results)
    assert _row(rows, "bit1_co").tested
    assert _row(rows, "bit1_dt").tested
    assert not _row(rows, "bit1_fallback").tested


def test_blocked_area_is_marked_attempted_blocked():
    results = [
        mk(test_key="Bit 4 — Firmware Package (terminal)", bit=4, target_type="terminal",
           target="T1", value="FW", status="BLOCKED", error="EXECUTE permission was denied"),
    ]
    row = _row(compute_coverage(results), "bit4")
    assert row.symbol == "⛔"
    assert row.state == "ATTEMPTED — BLOCKED"


def test_failed_area_is_marked_attempted_failed():
    results = [
        mk(test_key="Bit 16 — DCC Receipt Template (instance)", bit=16,
           value="T", status="FAIL", error="boom"),
    ]
    row = _row(compute_coverage(results), "bit16")
    assert row.symbol == "🔴"


def test_negative_results_do_not_count_as_coverage():
    results = [mk(is_negative=True, status="PASS", value="dccEnable")]
    rows = compute_coverage(results)
    assert _row(rows, "bit8_dccEnable").symbol == "❌"


def test_placeholder_areas_carry_a_note_when_covered():
    results = [
        mk(test_key="Bit 4 — Firmware Package (terminal)", bit=4, target_type="terminal",
           target="T1", value="DCC_Standard"),
    ]
    row = _row(compute_coverage(results), "bit4")
    assert row.tested
    assert "placeholder" in row.detail.lower()


def test_totals_sum_to_area_count():
    rows = compute_coverage([mk(value="dccEnable")])
    totals = coverage_totals(rows)
    assert totals["total"] == 13
    assert (
        totals["covered"] + totals["blocked"] + totals["failed"]
        + totals["review"] + totals["not_tested"]
    ) == 13
