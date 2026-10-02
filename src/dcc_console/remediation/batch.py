"""Pure batch planner: turn a set of selected worklist rows into the fixes to run.

The operator filters the worklist (e.g. region + one check) and selects terminals;
batch then fixes **every** fixable broken check those terminals have — not only the
check that was filtered on. Because a worklist row is ``SELECT *`` over
``V_CURRENT_BROKEN`` it already carries every check column, so :func:`fixer.plan_for_row`
enumerates all of a terminal's fixable checks for free.

This module is pure (no Streamlit, no DB): it groups the per-terminal steps into the
minimum set of procedure calls, so one instance/location call is not issued twice when
several selected terminals share it. :mod:`.batch_panel` executes the plan by driving
the same :mod:`.fixer` primitives the one-by-one panel uses.

Decision (2026-10-02): the Bit 16 receipt-template fix is **not** auto-run in batch — its
template is chosen per terminal and must be confirmed by the operator, so those checks
are surfaced as ``templates`` (do them on the Single-fix page) rather than applied blind.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import fixer


@dataclass(frozen=True)
class BatchItem:
    """One procedure call (one check on one target) that batch will run.

    ``covered_rows`` are the selected terminals this one call repairs — several when they
    share an instance (Bit 8/16) or a location (Bit 1); exactly one for a terminal-scoped
    Bit 2. The call physically changes the whole target, so the panel may surface other
    listed-but-unselected terminals on it; the registry/log still records one row each.
    """

    step: fixer.FixStep
    covered_rows: tuple[dict, ...]

    @property
    def key(self) -> tuple[str, str, str, str | None]:
        return (
            self.step.check_column,
            self.step.target_kind,
            self.step.target_identifier,
            self.step.config_value,
        )

    @property
    def terminals(self) -> tuple[str, ...]:
        return tuple(str(r.get("TERMINAL_IDENTIFIER")) for r in self.covered_rows)


@dataclass(frozen=True)
class TemplateItem:
    """A Bit 16 receipt-template check that needs a manual, per-terminal template choice."""

    check_column: str
    flag_name: str
    target_kind: str
    target_identifier: str
    covered_rows: tuple[dict, ...]

    @property
    def terminals(self) -> tuple[str, ...]:
        return tuple(str(r.get("TERMINAL_IDENTIFIER")) for r in self.covered_rows)


@dataclass(frozen=True)
class ManualItem:
    """A broken check the procedure cannot fix (surfaced for manual follow-up)."""

    terminal_identifier: str
    check_column: str
    flag_name: str
    reason: str


@dataclass(frozen=True)
class BatchPlan:
    """The result of planning a batch over the selected rows."""

    items: tuple[BatchItem, ...] = ()
    templates: tuple[TemplateItem, ...] = ()
    manual: tuple[ManualItem, ...] = ()
    resolved: int = 0  # fixable steps already fixed/awaiting refresh this snapshot (skipped)
    selected_terminals: tuple[str, ...] = ()
    fixes_per_terminal: dict[str, int] = field(default_factory=dict)

    @property
    def has_work(self) -> bool:
        return bool(self.items)

    @property
    def terminals_with_extra_fixes(self) -> tuple[str, ...]:
        """Selected terminals needing more than one auto-fix (the 'additional fixes' case)."""
        return tuple(tid for tid, n in sorted(self.fixes_per_terminal.items()) if n > 1)


def _sort_rows(rows: list[dict]) -> tuple[dict, ...]:
    return tuple(sorted(rows, key=lambda r: str(r.get("TERMINAL_IDENTIFIER"))))


def plan_batch(rows: list[dict]) -> BatchPlan:
    """Group every fixable broken check on the selected ``rows`` into batch calls.

    * deterministic fixes (Bit 1/2/8) are de-duplicated by
      ``(check, target_kind, target_identifier, value)`` so a shared instance/location is
      fixed once, with every covered selected terminal recorded;
    * Bit 16 receipt-template checks are returned under ``templates`` (manual);
    * non-fixable checks are returned under ``manual``;
    * steps already resolved in this snapshot are counted and skipped.
    """
    item_rows: dict[tuple, list[dict]] = {}
    item_step: dict[tuple, fixer.FixStep] = {}
    template_rows: dict[tuple, list[dict]] = {}
    template_flag: dict[tuple, object] = {}
    manual: list[ManualItem] = []
    resolved = 0
    fixes_per_terminal: dict[str, int] = {}
    selected: list[str] = []

    for row in rows:
        tid = str(row.get("TERMINAL_IDENTIFIER"))
        selected.append(tid)
        fixes_per_terminal.setdefault(tid, 0)
        steps, blocked = fixer.plan_for_row(row)
        for nf in blocked:
            manual.append(ManualItem(tid, nf.check_column, nf.flag.flag_name, nf.reason))
        for step in steps:
            if step.resolved:
                resolved += 1
                continue
            if step.needs_template:  # Bit 16 — template chosen per terminal, not auto-run
                tkey = (step.check_column, step.target_kind, step.target_identifier)
                template_rows.setdefault(tkey, []).append(row)
                template_flag[tkey] = step.flag
                continue
            key = (step.check_column, step.target_kind, step.target_identifier, step.config_value)
            item_rows.setdefault(key, []).append(row)
            item_step.setdefault(key, step)
            fixes_per_terminal[tid] += 1

    items = tuple(
        BatchItem(step=item_step[key], covered_rows=_sort_rows(item_rows[key]))
        for key in sorted(item_rows, key=lambda k: (item_step[k].definition.bit, k[0], k[2]))
    )
    templates = tuple(
        TemplateItem(
            check_column=tkey[0],
            flag_name=getattr(template_flag[tkey], "flag_name", tkey[0]),
            target_kind=tkey[1],
            target_identifier=tkey[2],
            covered_rows=_sort_rows(template_rows[tkey]),
        )
        for tkey in sorted(template_rows)
    )
    return BatchPlan(
        items=items,
        templates=templates,
        manual=tuple(manual),
        resolved=resolved,
        selected_terminals=tuple(dict.fromkeys(selected)),
        fixes_per_terminal=fixes_per_terminal,
    )
