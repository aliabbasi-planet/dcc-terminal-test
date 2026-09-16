"""Canonical DCC configuration coverage registry and matrix computation.

CAB issues #2 and #3 are about *coverage*: reviewers must see every individual
configuration item the procedure supports and, for each, whether it was exercised
and with what evidence. Each handler flag is its own row — grouping five flags
into one "core" row hides individual coverage and won't satisfy a reviewer.

The matrix is derived from real run results, so it can never claim coverage that
was not run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .execution import TestResult

# Result statuses that prove the procedure body actually executed for an area.
_EXECUTED_STATUSES = {"PASS", "REVIEW"}


@dataclass(frozen=True)
class ConfigArea:
    """One CAB-facing configuration capability."""

    id: str
    label: str
    family: str
    requirement: str
    catalog_key: str | None = None
    bit8_value: str | None = None
    assumption: str | None = None
    # False for areas where the test used a placeholder value that hasn't been
    # confirmed as a real identifier (e.g. Bit 4 firmware, Bit 16 template).
    confirmed_values: bool = True

    def matches(self, result: TestResult) -> bool:
        if result.is_negative:
            return False
        if self.bit8_value is not None:
            return result.bit == 8 and result.value == self.bit8_value
        if self.catalog_key is not None:
            return result.test_key == self.catalog_key
        return False


CONFIG_AREAS: tuple[ConfigArea, ...] = (
    ConfigArea(
        id="bit1_co",
        label="DCCXpressCO",
        family="Bit 1 — Location extra_function",
        requirement="A valid location and the DCCXpressCO node.",
        catalog_key="Bit 1 — DCC Xpress CO (location extra_function)",
    ),
    ConfigArea(
        id="bit1_dt",
        label="DCCXpressCODT",
        family="Bit 1 — Location extra_function",
        requirement="A valid location and the DCCXpressCODT node.",
        catalog_key="Bit 1 — DCC Xpress CO Delayed Terminal (location extra_function)",
        assumption=(
            "Exercised via @extra_function_name='DCCXpressCODT' with @add=1; node name "
            "should be confirmed with the procedure owner."
        ),
    ),
    ConfigArea(
        id="bit1_fallback",
        label="DCCXpressCOFallback",
        family="Bit 1 — Location extra_function",
        requirement="A valid location and the DCCXpressCOFallback node.",
        catalog_key="Bit 1 — DCC Xpress CO Fallback (location extra_function)",
        assumption=(
            "Exercised via @extra_function_name='DCCXpressCOFallback' with @add=1; node name "
            "should be confirmed with the procedure owner."
        ),
    ),
    ConfigArea(
        id="bit2",
        label="Config Download Version",
        family="Bit 2 — Terminal",
        requirement="Valid terminals; both already-Standard and different-version rows.",
        catalog_key="Bit 2 — Config Download Version (terminal)",
    ),
    ConfigArea(
        id="bit4",
        label="Firmware Package",
        family="Bit 4 — Terminal",
        requirement=(
            "Downstream helper [cccintegrang].[spManageEMVTerminalFirmwarePackage], an "
            "approved firmware package name, and a test terminal."
        ),
        catalog_key="Bit 4 — Firmware Package (terminal)",
        confirmed_values=False,
    ),
    ConfigArea(
        id="bit8_dccEnable",
        label="dccEnable",
        family="Bit 8 — Instance handler flags",
        requirement="A valid instance.",
        bit8_value="dccEnable",
    ),
    ConfigArea(
        id="bit8_dccEnableAuth",
        label="dccEnableAuth",
        family="Bit 8 — Instance handler flags",
        requirement="A valid instance.",
        bit8_value="dccEnableAuth",
    ),
    ConfigArea(
        id="bit8_dccEnableCompletion",
        label="dccEnableCompletion",
        family="Bit 8 — Instance handler flags",
        requirement="A valid instance.",
        bit8_value="dccEnableCompletion",
    ),
    ConfigArea(
        id="bit8_dccEnableNfc",
        label="dccEnableNfc",
        family="Bit 8 — Instance handler flags",
        requirement="A valid instance.",
        bit8_value="dccEnableNfc",
    ),
    ConfigArea(
        id="bit8_dccEnableNfcSingleTap",
        label="dccEnableNfcSingleTap",
        family="Bit 8 — Instance handler flags",
        requirement="A valid instance.",
        bit8_value="dccEnableNfcSingleTap",
    ),
    ConfigArea(
        id="bit8_dccEnableRefund",
        label="dccEnableRefund",
        family="Bit 8 — Instance handler flags",
        requirement="A valid instance.",
        bit8_value="dccEnableRefund",
        assumption="Treated as a boolean flag (@Config_value=1), consistent with dccEnable*.",
    ),
    ConfigArea(
        id="bit8_dccFlagsEnabled",
        label="dccFlagsEnabled",
        family="Bit 8 — Instance handler flags",
        requirement="A valid instance.",
        bit8_value="dccFlagsEnabled",
        assumption=(
            "Exercised as a boolean (@Config_value=1). If it is an integer bitmask, its "
            "numeric semantics must be confirmed with the procedure owner."
        ),
    ),
    ConfigArea(
        id="bit16",
        label="DCC Receipt Template",
        family="Bit 16 — Instance",
        requirement="A valid instance and an approved DCC receipt template value.",
        catalog_key="Bit 16 — DCC Receipt Template (instance)",
        confirmed_values=False,
    ),
)


@dataclass
class CoverageRow:
    """Computed coverage for one area."""

    area: ConfigArea
    symbol: str
    state: str
    run_count: int
    detail: str

    @property
    def tested(self) -> bool:
        return self.symbol == "✅"


def _area_state(matches: list[TestResult]) -> tuple[str, str, str]:
    """Return (symbol, state, detail) for the results matching one area."""
    if not matches:
        return "❌", "NOT TESTED", "No execution evidence in this run."

    executed = [r for r in matches if r.status in _EXECUTED_STATUSES]
    if executed:
        has_preview = any(r.proposed_change_observed for r in executed)
        all_null = all(
            r.state_before is None and r.state_after is None for r in executed
        )
        codes = sorted({r.verdict_code for r in executed})
        if has_preview:
            return "✅", "COVERED", f"{len(executed)} run(s); verdict(s): {', '.join(codes)}."
        if all_null:
            return (
                "🟡",
                "EXERCISED — NO PREVIEW EVIDENCE",
                f"{len(executed)} run(s) accepted without error, but the procedure "
                "returned no preview result set and the verified column was null "
                "before and after. The parameter may have been silently ignored. "
                "Confirm with the procedure owner that this branch actually ran.",
            )
        return (
            "✅",
            "COVERED",
            f"{len(executed)} run(s); verdict(s): {', '.join(codes)}. "
            "(No preview grid returned, but verified column has a non-null value.)",
        )

    if any(r.status == "BLOCKED" for r in matches):
        return "⛔", "ATTEMPTED — BLOCKED", "Login lacked EXECUTE; procedure body did not run."

    if any(r.status == "FAIL" for r in matches):
        errors = "; ".join(sorted({(r.error or "SQL error")[:120] for r in matches}))
        return "🔴", "ATTEMPTED — FAILED", f"Ran but errored: {errors}"

    return "🟡", "ATTEMPTED — REVIEW", "Ran but produced an ambiguous outcome."


def compute_coverage(results: list[TestResult]) -> list[CoverageRow]:
    rows: list[CoverageRow] = []
    for area in CONFIG_AREAS:
        matches = [r for r in results if area.matches(r)]
        symbol, state, detail = _area_state(matches)
        if area.confirmed_values is False and symbol == "✅":
            detail += (
                " NOTE: placeholder value used to exercise the procedure path — "
                "confirm with the owner that the tested value is a real identifier."
            )
        rows.append(
            CoverageRow(
                area=area,
                symbol=symbol,
                state=state,
                run_count=len(matches),
                detail=detail,
            )
        )
    return rows


def coverage_totals(rows: list[CoverageRow]) -> dict[str, int]:
    return {
        "total": len(rows),
        "covered": sum(1 for row in rows if row.symbol == "✅"),
        "weak_evidence": sum(
            1 for row in rows if row.symbol == "🟡" and "NO PREVIEW" in row.state
        ),
        "blocked": sum(1 for row in rows if row.symbol == "⛔"),
        "failed": sum(1 for row in rows if row.symbol == "🔴"),
        "review": sum(
            1 for row in rows if row.symbol == "🟡" and "NO PREVIEW" not in row.state
        ),
        "not_tested": sum(1 for row in rows if row.symbol == "❌"),
    }


__all__ = [
    "ConfigArea",
    "CONFIG_AREAS",
    "CoverageRow",
    "compute_coverage",
    "coverage_totals",
]
