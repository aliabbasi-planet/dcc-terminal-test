"""Unit tests for the pure batch planner (:mod:`dcc_console.remediation.batch`).

No Streamlit, no DB — ``plan_batch`` turns worklist rows into the de-duplicated set of
procedure calls, pulling in every fixable check each terminal has (the "additional fixes
beyond the one you filtered on" requirement) and separating Bit 16 templates and
non-fixable checks for manual follow-up.
"""

from __future__ import annotations

from dcc_console.remediation import batch
from dcc_console.remediation.mapping import TRACKED_CHECK_COLUMNS

_BASE = {c: 0 for c in TRACKED_CHECK_COLUMNS}


def _row(tid: str, instance: str, location: str, *, fixed: str = "[]", **broken) -> dict:
    return {
        "TERMINAL_IDENTIFIER": tid,
        "INSTANCE_IDENTIFIER": instance,
        "LOCATION_NO": location,
        "SOURCE_LAST_ALTERED": None,
        "FIXED_CHECKS": fixed,
        **_BASE,
        **broken,
    }


def test_plan_includes_every_fixable_check_not_just_the_filtered_one():
    # One terminal broken on a Bit 8 handler flag *and* a Bit 2 config version.
    rows = [
        _row(
            "T-1",
            "INST-1",
            "LOC-1",
            HANDLER_DCCENABLE_CHECK_O=1,
            CONFIGDOWNLOAD_VERSION_CHECK_C=1,
        )
    ]
    plan = batch.plan_batch(rows)

    bits = sorted(it.step.definition.bit for it in plan.items)
    assert bits == [2, 8]  # both checks planned, not only one
    assert plan.fixes_per_terminal["T-1"] == 2
    assert plan.terminals_with_extra_fixes == ("T-1",)


def test_shared_instance_call_is_deduplicated_and_covers_both_terminals():
    rows = [
        _row("T-1", "INST-1", "LOC-1", HANDLER_DCCENABLE_CHECK_O=1),
        _row("T-2", "INST-1", "LOC-2", HANDLER_DCCENABLE_CHECK_O=1),
    ]
    plan = batch.plan_batch(rows)

    assert len(plan.items) == 1  # one instance-level call, not two
    item = plan.items[0]
    assert item.step.target_identifier == "INST-1"
    assert item.terminals == ("T-1", "T-2")  # both selected terminals recorded


def test_bit16_template_is_separated_for_manual_choice_not_auto_run():
    rows = [_row("T-3", "INST-9", "LOC-9", PRINTOUTTYPETEMPLATEDCC_CHECK_C=1)]
    plan = batch.plan_batch(rows)

    assert plan.items == ()  # never auto-applied
    assert len(plan.templates) == 1
    assert plan.templates[0].target_identifier == "INST-9"
    assert plan.templates[0].terminals == ("T-3",)


def test_non_fixable_check_is_listed_as_manual():
    rows = [_row("T-4", "INST-4", "LOC-4", DCCMERCHANT_NO_CHECK_O=1)]
    plan = batch.plan_batch(rows)

    assert plan.items == ()
    assert [m.terminal_identifier for m in plan.manual] == ["T-4"]
    assert "DCC Merchant" in plan.manual[0].flag_name


def test_already_resolved_checks_are_skipped_and_counted():
    rows = [
        _row(
            "T-5",
            "INST-5",
            "LOC-5",
            fixed='["CONFIGDOWNLOAD_VERSION_CHECK_C"]',
            CONFIGDOWNLOAD_VERSION_CHECK_C=1,
        )
    ]
    plan = batch.plan_batch(rows)

    assert plan.items == ()  # the only broken check is already settled this snapshot
    assert plan.resolved == 1


def test_selected_terminals_are_tracked_even_with_no_fixable_work():
    rows = [_row("T-6", "INST-6", "LOC-6", DCCMERCHANT_NO_CHECK_O=1)]
    plan = batch.plan_batch(rows)

    assert plan.selected_terminals == ("T-6",)
    assert not plan.has_work
