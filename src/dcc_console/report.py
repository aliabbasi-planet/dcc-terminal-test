"""CAB-grade evidence report generator.

Turns a list of :class:`~dcc_console.execution.TestResult` (positive and negative)
into a Markdown report structured exactly the way a Change Advisory Board reviewer
asked for:

* Section A — Procedure execution evidence (proves the procedure actually ran).
* Section B — Coverage matrix over all nine configuration areas.
* Section C — Negative validation (proves the rejection paths work).
* Section D — Capability & limitation analysis (precise verified-vs-out-of-scope).

The wording deliberately never says "PASS = configuration changed". A simulation
run is reported as *SIMULATED-OK*: the procedure executed and rolled back, which
is different from a persisted configuration change.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .coverage import compute_coverage, coverage_totals
from .execution import TestResult

_MAX_GRID_ROWS = 25
_MAX_CELL = 200


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")[:_MAX_CELL]


def _clip(value: object, limit: int = 1200) -> str:
    text = "(null)" if value is None else str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more characters]"


def _records_to_table(records: list[dict]) -> list[str]:
    if not records:
        return ["_(empty result set)_"]
    columns = list(records[0].keys())
    lines = [
        "| " + " | ".join(_cell(col) for col in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in records[:_MAX_GRID_ROWS]:
        lines.append("| " + " | ".join(_cell(row.get(col)) for col in columns) + " |")
    if len(records) > _MAX_GRID_ROWS:
        lines.append(f"_… {len(records) - _MAX_GRID_ROWS} more row(s) omitted._")
    return lines


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


# ---------------------------------------------------------------------------
# header, legend
# ---------------------------------------------------------------------------


def _header(meta: dict, positives: list[TestResult], negatives: list[TestResult]) -> list[str]:
    generated = meta.get("generated_at") or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    executed = [r for r in positives if r.status in {"PASS", "REVIEW"}]
    rejected = [r for r in negatives if r.verdict_code == "REJECTED-AS-EXPECTED"]
    not_rejected = [r for r in negatives if r.verdict_code == "NOT-REJECTED"]
    rows = compute_coverage(positives)
    totals = coverage_totals(rows)
    assumption_count = sum(
        1 for r in rows if r.area.assumption and r.symbol in {"✅", "🟡"}
    )

    lines = [
        "# DCC Enablement Configuration — CAB Validation Report",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Procedure | `{meta.get('procedure', '[cccai].[spApplyDCCEnablementConfiguration]')}` |",
        f"| Server | `{meta.get('server', '?')}` |",
        f"| Database | `{meta.get('database', '?')}` |",
        f"| Environment | {meta.get('environment', '?')} |",
        f"| Executed by (login) | `{meta.get('login', '?')}` |",
        f"| Report generated (UTC) | {generated} |",
        f"| Execution mode | {meta.get('mode', 'SIMULATION')} |",
        "",
        "## Executive summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Positive procedure executions | {len(executed)} |",
        f"| Procedure surfaces exercised | "
        f"{totals['covered'] + totals['weak_evidence']} / {totals['total']} |",
        f"| — with preview evidence (✅) | {totals['covered']} |",
        f"| — accepted but no preview (🟡 weak — needs confirmation) | {totals['weak_evidence']} |",
        f"| — assumption-based (pending owner confirmation) | {assumption_count} |",
        f"| Areas blocked | {totals['blocked']} |",
        f"| Areas not tested | {totals['not_tested']} |",
    ]
    # Negative validation as a finding, not a pass/fail metric.
    if negatives:
        lines += [
            f"| Negative validation cases executed | {len(negatives)} |",
            f"| — rejected as expected | {len(rejected)} |",
            f"| — **NOT rejected (finding)** | **{len(not_rejected)}** |",
        ]
    lines += [
        "",
        "> This report proves the procedure **executed** and how it **behaved** under "
        "simulation. Under `@is_simulation = 1` every transaction is rolled back, so a "
        "positive result means *the call ran and proposed a change*, **not** that a live "
        "configuration was persisted. Persisted changes are only claimed where the mode is "
        "LIVE and the verified column actually changed (verdict APPLIED).",
    ]
    if not_rejected:
        lines.append(
            f"> **Negative validation finding**: {len(not_rejected)} of {len(negatives)} "
            "deliberately invalid inputs were NOT rejected by the procedure. This requires "
            "procedure-owner review before negative validation coverage can be claimed. "
            "See Section C."
        )
    return lines


def _procedure_version(meta: dict) -> list[str]:
    version = meta.get("procedure_version")
    lines = ["", "## Procedure version under test", ""]
    if not version:
        lines.append(
            "_Procedure version evidence was not captured for this run. Re-run pre-flight "
            "readiness (which records object_id, last-modified date and a definition hash) so "
            "this section pins the exact procedure build that was validated._"
        )
        return lines

    sha = version.get("definition_sha256", "unavailable")
    sha_available = bool(sha) and sha != "unavailable"
    server_sha = version.get("sha256_server_utf16", "unavailable")
    server_available = bool(server_sha) and server_sha != "unavailable"
    def_bytes = version.get("definition_bytes")

    lines += [
        "This section establishes the **three-part identity** of the procedure body that was "
        "exercised: which object, when it was last deployed, and a digest that changes if the "
        "body is edited.",
        "",
        "| Attribute | Value |",
        "| --- | --- |",
        f"| Schema.object | `{version.get('object', '?')}` |",
        f"| Object ID | {version.get('object_id', '?')} |",
        f"| Created | {version.get('create_date', '?')} |",
        f"| **Last modified** | **{version.get('modify_date', '?')}** |",
        f"| Definition length (bytes) | {def_bytes if def_bytes is not None else 'unavailable'} |",
        f"| **SHA-256 (UTF-8, harness)** | `{sha}` |",
        f"| SHA-256 (UTF-16LE, server) | `{server_sha}` |",
    ]

    lines += ["", "### Digest methods", ""]
    method_utf8 = version.get("definition_sha256_method")
    method_server = version.get("sha256_server_method")
    if method_utf8:
        lines.append(f"- **UTF-8 digest**: {method_utf8}")
    if method_server:
        lines.append(f"- **UTF-16LE digest**: {method_server}")

    if sha_available and server_available:
        lines += [
            "",
            "**The two digests are expected to differ.** They hash different byte encodings of "
            "the same procedure text. The UTF-8 digest is the primary reference for this report. "
            "The UTF-16LE digest is provided so a reviewer with only SSMS access can verify "
            "independently by running:",
            "",
            "```sql",
            "SELECT CONVERT(char(64), HASHBYTES('SHA2_256', m.definition), 2) AS sha256_utf16,",
            "       DATALENGTH(m.definition)                                  AS definition_bytes",
            "FROM sys.sql_modules AS m",
            f"WHERE m.object_id = OBJECT_ID(N'{version.get('object', '?')}', N'P');",
            "```",
        ]
    elif sha_available:
        lines += [
            "",
            "The UTF-8 digest is the reference for this report. The server-side `HASHBYTES` "
            "cross-check was not available for this run.",
        ]

    if not sha_available:
        reason = version.get("unavailable_reason") or (
            "The definition could not be read from `sys.sql_modules`."
        )
        lines += [
            "",
            f"**Definition digest unavailable.** {reason}",
            "",
            "Without a digest, a reviewer cannot confirm which procedure body was tested. The "
            "`object_id` and last-modified timestamp above still establish object identity and "
            "deployment time.",
        ]
        if version.get("is_encrypted"):
            lines.append(
                "Because the procedure is encrypted, the appropriate evidence is the digest of "
                "the deployment artefact from the release package, obtained from the change "
                "record rather than from the database."
            )

    # Scope statement on artefact correspondence — deliberately does not overclaim.
    lines += [
        "",
        "### Correspondence to the deployment artefact",
        "",
        "The procedure body validated here is pinned by `object_id`, last-modified timestamp, "
        "and definition digest. If the procedure is altered after this run, the timestamp moves "
        "and the digest changes — so this evidence proves the tested build is the build in the "
        "environment at the time of test.",
        "",
        "Correspondence to the release artefact file is established by the deployment process "
        "and change record, **not** by this test harness. A byte-for-byte comparison against a "
        "`.sql` release file is not offered here because `sys.sql_modules.definition` stores only "
        "the `CREATE` statement text, excluding batch separators, file headers, existence guards "
        "and line-ending conventions present in the artefact — a mismatch on those would carry "
        "no information about procedure correctness.",
    ]
    lines += _trace_instrumentation(meta)
    return lines


def _trace_instrumentation(meta: dict) -> list[str]:
    """Describe whether the procedure's trace function could be read."""
    sig = meta.get("trace_signature")
    lines = ["", "### Trace instrumentation", ""]
    if not sig:
        lines.append(
            "_Trace availability was not recorded for this run. Re-run pre-flight readiness "
            "so the report can state whether the procedure's internal trace was readable._"
        )
        return lines

    usable = bool(sig.get("usable"))
    lines += [
        "| Attribute | Value |",
        "| --- | --- |",
        f"| Trace function | `{sig.get('function', '?')}` |",
        f"| Exists | {_yes_no(bool(sig.get('exists')))} |",
        f"| Table-valued | {sig.get('is_table_valued')} |",
        f"| Parameters | {sig.get('parameter_count', 0)} |",
        f"| Readable by this harness | {_yes_no(usable)} |",
    ]
    if sig.get("call_sql"):
        lines += ["", "Call form used to read the trace:", "", "```sql", sig["call_sql"], "```"]
    if usable:
        lines += [
            "",
            "Because the trace is readable, a validation failure recorded only in the trace "
            "**is** detected by the negative validation battery in Section C.",
        ]
    else:
        lines += [
            "",
            f"**Trace not readable.** {sig.get('unavailable_reason') or 'No reason recorded.'}",
            "",
            "This is material to Section C: if the procedure records validation failures only "
            "in its trace, a `NOT-REJECTED` verdict cannot be treated as a confirmed validation "
            "gap until the trace can be read.",
        ]
    return lines


def _legend() -> list[str]:
    return [
        "",
        "## Verdict legend",
        "",
        "| Verdict | Meaning |",
        "| --- | --- |",
        "| `SIMULATED-OK` | Procedure executed under `@is_simulation=1`; transaction rolled "
        "back; nothing persisted. Proves the call ran, not that a config changed. |",
        "| `APPLIED` | Live run committed and the verified column changed — configuration "
        "actually applied. |",
        "| `REVIEW` | Live run completed without error but the verified column did not "
        "change — accepted without acting. |",
        "| `REJECTED-AS-EXPECTED` | A deliberately invalid input was rejected — the "
        "validation path works. |",
        "| `NOT-REJECTED` | A deliberately invalid input was **not** rejected — a finding to "
        "review with the procedure owner. |",
        "| `BLOCKED` | Login lacked EXECUTE; the procedure body never ran (environment gap). |",
        "| `FAIL` | The call raised an unexpected SQL error. |",
    ]


# ---------------------------------------------------------------------------
# Section A — procedure execution evidence
# ---------------------------------------------------------------------------


def _evidence_block(index: int, result: TestResult) -> list[str]:
    lines = [
        "",
        f"### A{index}. {result.test_key} — `{result.verdict_code}`",
        "",
        f"- **Target**: {result.target_type} = `{result.target}`",
        f"- **Value sent**: `{result.value}`",
        f"- **Mode**: {result.mode}",
        f"- **Verdict**: `{result.verdict_code}` — {result.verdict_detail}",
        f"- **Procedure parameters**: {', '.join(result.proc_args) or '(see EXEC below)'}",
        "",
        "**EXEC statement actually issued**",
        "",
        "```sql",
        result.sql,
        "```",
        "",
        "**Input JSON payload**",
        "",
        "```json",
        _clip(result.input_json, 600) or "(none)",
        "```",
        "",
        "**Before / proposed / after evidence**",
        "",
        "| Verified column | Value |",
        "| --- | --- |",
        f"| Before (restore point) | {_cell(_clip(result.state_before, 400))} |",
        f"| After | {_cell(_clip(result.state_after, 400))} |",
        f"| Change persisted | {_yes_no(result.change_persisted)} |",
        f"| Procedure preview/trace returned | {_yes_no(result.proposed_change_observed)} |",
        f"| Transaction | {result.transaction} |",
        f"| Duration | {result.duration_s}s |",
        "",
        "**Rollback verification**",
        "",
    ]
    if result.mode == "SIMULATION":
        if not result.change_persisted:
            lines.append(
                "Simulation transaction rolled back successfully — the verified column's "
                "value after the call matches the value before the call, proving no data was "
                "persisted. This is a mathematical proof (column fingerprint equality), not "
                "an assertion."
            )
        else:
            lines.append(
                "**WARNING**: Simulation call left a persistent change (`change_persisted = "
                "True`). The rollback did NOT fully revert the verified column. Investigate "
                "before treating this as validated."
            )
    elif result.rollback_log:
        entries = [
            f"- {e['trigger'].capitalize()} rollback at {e['at']}: "
            f"{'succeeded' if e['ok'] else 'FAILED — ' + str(e.get('error', '?'))}"
            for e in result.rollback_log
        ]
        lines += entries
    else:
        lines.append(
            "Live run — no rollback was performed. The change is committed to the database."
        )

    # Bit 2 same-value advisory: if before == after and the test was for Config Download
    # Version, the terminal was already at the target version. Flag it honestly.
    if (
        result.bit == 2
        and result.state_before is not None
        and str(result.state_before) == str(result.state_after)
        and not result.change_persisted
    ):
        lines += [
            "",
            "**Bit 2 note**: The selected terminal was already configured with Config "
            "Download Version = the target value; therefore this test demonstrates procedure "
            "execution rather than a version transition. Validation against a terminal with a "
            "different starting version is recommended for complete behavioural coverage.",
        ]

    if result.error:
        lines += ["", "**SQL error**", "", "```", result.error, "```"]

    if result.messages:
        lines += ["", "**Server messages (PRINT / RAISERROR)**", "", "```"]
        lines += [str(message) for message in result.messages]
        lines.append("```")

    lines += _trace_block(result)

    if result.grids:
        for gi, grid in enumerate(result.grids, start=1):
            lines += [
                "",
                f"**Procedure result set {gi} (proposed change / preview evidence)**",
                "",
            ]
            lines += _records_to_table(grid)
    else:
        lines += [
            "",
            "_Procedure returned no tabular result set for this call; rely on the server "
            "messages, the procedure trace, and the before/after values._",
        ]
    return lines


_TRACE_STATUS_NOTE = {
    "CAPTURED": None,
    "EMPTY": (
        "The trace function was called successfully but returned no rows for this "
        "call. The procedure recorded nothing in its trace."
    ),
    "UNAVAILABLE": (
        "The procedure trace could not be read for this run. It was attempted, not "
        "skipped — the reason is stated below."
    ),
    "ERRORED": "The trace read raised an error; the reason is stated below.",
    "NOT_ATTEMPTED": (
        "The procedure trace was not read for this call because its signature had "
        "not been discovered. Re-run pre-flight readiness to enable trace capture."
    ),
}


def _trace_block(result: TestResult) -> list[str]:
    """Render the fnDisplayTrace output, or explain why it is absent."""
    status = getattr(result, "trace_status", "NOT_ATTEMPTED")
    rows = getattr(result, "trace_rows", []) or []
    reason = getattr(result, "trace_reason", None)

    lines = ["", f"**Procedure trace (`fnDisplayTrace`) — {status}**", ""]
    if rows:
        lines += _records_to_table(rows)
        if reason:
            lines += ["", f"_{reason}_"]
        return lines

    note = _TRACE_STATUS_NOTE.get(status)
    if note:
        lines.append(note)
    if reason:
        lines += ["", f"_{reason}_"]
    return lines


def _section_a(positives: list[TestResult]) -> list[str]:
    lines = [
        "",
        "## Section A — Procedure execution evidence",
        "",
        "Direct proof that `[cccai].[spApplyDCCEnablementConfiguration]` was executed: the "
        "exact `EXEC` statement, its parameters and input JSON, the procedure's own "
        "preview/trace output, and the verified column before and after the (rolled-back) "
        "call.",
    ]
    if not positives:
        lines += ["", "_No positive executions were recorded in this run._"]
        return lines
    for index, result in enumerate(positives, start=1):
        lines += _evidence_block(index, result)
    return lines


# ---------------------------------------------------------------------------
# Section B — coverage matrix
# ---------------------------------------------------------------------------


def _section_b(positives: list[TestResult]) -> list[str]:
    rows = compute_coverage(positives)
    lines = [
        "",
        "## Section B — Coverage matrix",
        "",
        "Every configuration item the procedure exposes, and whether this run produced "
        "execution evidence for it. `✅` = procedure executed and returned preview evidence; "
        "`🟡` = procedure accepted the call but returned no preview (weak — needs "
        "confirmation); `⛔` = attempted but blocked; `🔴` = ran but errored; `❌` = not "
        "tested in this run.",
        "",
        "| Config area | Family | Covered | State | Runs | Evidence |",
        "| --- | --- | :---: | --- | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.area.label} | {row.area.family} | {row.symbol} | {row.state} | "
            f"{row.run_count} | {_cell(row.detail)} |"
        )
    return lines


# ---------------------------------------------------------------------------
# Section C — negative validation
# ---------------------------------------------------------------------------


def _negative_evidence(result: TestResult) -> str:
    """Pick the strongest available rejection evidence for one negative case."""
    if result.error:
        return result.error
    if result.messages:
        return result.messages[0]
    trace_text = getattr(result, "trace_text", "")
    if trace_text:
        return f"(from trace) {trace_text}"
    status = getattr(result, "trace_status", "NOT_ATTEMPTED")
    if status in {"UNAVAILABLE", "ERRORED", "NOT_ATTEMPTED"}:
        return f"no error raised; trace {status.lower()}"
    return "no error raised; trace returned no rows"


def _section_c(negatives: list[TestResult]) -> list[str]:
    lines = [
        "",
        "## Section C — Negative validation",
        "",
        "Deliberately invalid input. The pass condition is a **rejection** — a raised SQL "
        "error, a rejection marker in server messages, or a rejection recorded in the "
        "procedure's own `fnDisplayTrace` output. Silent acceptance of bad input is a finding.",
    ]
    if not negatives:
        lines += [
            "",
            "_No negative cases were run. Execute the negative battery so the validation "
            "paths are evidenced (invalid instance/terminal/location, unknown flag, unknown "
            "location function, unknown version, and empty target JSON)._",
        ]
        return lines
    lines += [
        "",
        "| Negative case | Target | Value | Verdict | Trace | Evidence |",
        "| --- | --- | --- | :---: | :---: | --- |",
    ]
    for result in negatives:
        evidence = _negative_evidence(result)
        trace_status = getattr(result, "trace_status", "NOT_ATTEMPTED")
        lines.append(
            f"| {result.test_key.replace('Negative — ', '')} | `{_cell(result.target)}` | "
            f"`{_cell(result.value)}` | `{result.verdict_code}` | {trace_status} | "
            f"{_cell(_clip(evidence, 240))} |"
        )

    rejected = sum(1 for r in negatives if r.verdict_code == "REJECTED-AS-EXPECTED")
    not_rejected = sum(1 for r in negatives if r.verdict_code == "NOT-REJECTED")

    # If the trace was never readable, a NOT-REJECTED verdict is not yet conclusive.
    trace_blind = [
        r
        for r in negatives
        if getattr(r, "trace_status", "NOT_ATTEMPTED")
        in {"UNAVAILABLE", "ERRORED", "NOT_ATTEMPTED"}
    ]

    if not_rejected > 0:
        lines += [
            "",
            f"**Finding**: {not_rejected} of {len(negatives)} negative case(s) returned "
            "`NOT-REJECTED` — the procedure did **not** raise a SQL exception and no "
            "rejection marker was detected in the server messages, result sets, or trace "
            "output that could be captured.",
        ]
        if trace_blind:
            lines += [
                "",
                f"**This finding is not yet conclusive for {len(trace_blind)} of those "
                "case(s).** The procedure's internal trace could not be read for them, so a "
                "rejection recorded only in the trace would not have been detected. Resolve "
                "the trace availability reason shown in Section A, re-run the battery, and "
                "re-assess before treating these as genuine validation gaps.",
            ]
        else:
            lines += [
                "",
                "The trace was readable for every case above, so the absence of a rejection "
                "marker is a genuine observation rather than a gap in instrumentation. The "
                "procedure owner should confirm whether these inputs are expected to be "
                "rejected. Until confirmed, negative validation coverage cannot be claimed.",
            ]
    if rejected == len(negatives):
        lines += [
            "",
            f"All {rejected} negative case(s) were rejected as expected. The procedure's "
            "validation path is confirmed for these inputs.",
        ]
    return lines


# ---------------------------------------------------------------------------
# Section D — capability & limitation analysis
# ---------------------------------------------------------------------------


def _section_d(positives: list[TestResult], meta: dict) -> list[str]:
    rows = compute_coverage(positives)
    covered_confirmed = [r for r in rows if r.symbol == "✅" and r.area.confirmed_values]
    covered_placeholder = [r for r in rows if r.symbol == "✅" and not r.area.confirmed_values]
    blocked = [r for r in rows if r.symbol == "⛔"]
    failed = [r for r in rows if r.symbol == "🔴"]
    not_tested = [r for r in rows if r.symbol == "❌"]
    assumptions = [
        f"**{r.area.label}** — {r.area.assumption}"
        for r in rows
        if r.area.assumption and r.symbol in {"✅", "🟡"}
    ]

    lines = ["", "## Section D — Capability & limitation analysis", ""]

    lines.append("**Confirmed tested** (procedure executed under simulation with known inputs):")
    if covered_confirmed:
        lines += [f"- {r.area.label} ({r.area.family})" for r in covered_confirmed]
    else:
        lines.append("- _None in this run._")

    if covered_placeholder:
        lines += [
            "",
            "**Exercised with placeholder values** (procedure path ran, but the input value "
            "has not been confirmed as a real identifier by the owner):",
        ]
        lines += [
            f"- {r.area.label} ({r.area.family}) — placeholder used solely to exercise "
            "the procedure path"
            for r in covered_placeholder
        ]

    lines += ["", "**Attempted but blocked** (dependency/permission gap, not a defect):"]
    if blocked:
        lines += [f"- {r.area.label} — {r.area.requirement}" for r in blocked]
    else:
        lines.append("- _None._")

    if failed:
        lines += ["", "**Ran but errored** (investigate):"]
        lines += [f"- {r.area.label} — {r.detail}" for r in failed]

    lines += ["", "**Not tested** — outside the verified scope of this run:"]
    if not_tested:
        lines += [f"- {r.area.label} — needs: {r.area.requirement}" for r in not_tested]
    else:
        lines.append("- _None; every configuration item was attempted._")

    lines += ["", "**Assumptions requiring procedure-owner confirmation:**"]
    if assumptions:
        lines += [f"- {item}" for item in assumptions]
    else:
        lines.append("- _None._")

    # Scope statement — uses the safer wording pattern recommended by CAB reviewer.
    confirmed_labels = ", ".join(r.area.label for r in covered_confirmed) or "no areas"
    placeholder_labels = ", ".join(r.area.label for r in covered_placeholder)
    remaining = [r.area.label for r in rows if r.symbol != "✅"]
    remaining_labels = ", ".join(remaining) if remaining else "none"

    lines += [
        "",
        "**Scope statement**",
        "",
        f"The campaign validated the simulation behaviour of: {confirmed_labels}.",
    ]
    if placeholder_labels:
        lines.append(
            f"Additional procedure capabilities were exercised with placeholder values "
            f"(pending owner confirmation of real identifiers): {placeholder_labels}."
        )
    lines.append(
        "Coverage of these capabilities is either: validated, exercised with "
        "placeholder values requiring confirmation, attempted and blocked by dependency, "
        "or requires clarification of accepted values."
    )
    if remaining_labels != "none":
        lines.append(
            f"The following remain outside this run's verified scope and must not be "
            f"read as validated: {remaining_labels}."
        )
    lines.append(
        "This report evidences procedure execution and behaviour; it does not by "
        "itself certify a live configuration change except where a row is explicitly "
        "marked `APPLIED`."
    )
    return lines


# ---------------------------------------------------------------------------
# Section E — rollback evidence (CAB Issue #6)
# ---------------------------------------------------------------------------


def _section_e(rollback_evidence: list) -> list[str]:
    """Dedicated rollback cycle evidence: before → change applied → restored."""
    lines = [
        "",
        "## Section E — Rollback evidence",
        "",
        "Proves the console can apply a live change and fully reverse it. Each test below ran "
        "in LIVE mode (committed), captured the changed state, then executed a compensating "
        "rollback, and finally verified the original value was restored.",
    ]
    if not rollback_evidence:
        lines += [
            "",
            "_No rollback evidence tests were run. Use the 'Rollback evidence test' button "
            "in the campaign or single-test UI to generate this section._",
        ]
        return lines

    for idx, ev in enumerate(rollback_evidence, start=1):
        proven = "PROVEN" if ev.full_cycle_proven else "NOT PROVEN"
        lines += [
            "",
            f"### E{idx}. {ev.test_key} — Rollback cycle: **{proven}**",
            "",
            f"- **Target**: `{ev.target}`",
            f"- **Environment**: {ev.environment}",
            "",
            "| Stage | Verified column value |",
            "| --- | --- |",
            f"| 1. Before (original) | {_cell(_clip(ev.state_before, 300))} |",
            f"| 2. After live apply | {_cell(_clip(ev.state_after_apply, 300))} |",
            f"| 3. After rollback | {_cell(_clip(ev.state_after_rollback, 300))} |",
            "",
            f"- **Change detected (step 1 ≠ step 2)**: {_yes_no(ev.change_detected)}",
            f"- **Rollback successful**: {_yes_no(ev.rollback_successful)}",
            f"- **Full cycle proven (step 1 == step 3)**: {_yes_no(ev.full_cycle_proven)}",
        ]
        if ev.full_cycle_proven:
            lines.append(
                "  The original value was restored exactly — the rollback mechanism is verified."
            )
        elif ev.error:
            lines.append(f"  **Note**: {ev.error}")
    return lines


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def cab_report(
    results: list[TestResult],
    meta: dict | None = None,
    rollback_evidence: list | None = None,
) -> str:
    """Render the full CAB report as Markdown from a list of results."""
    meta = meta or {}
    positives = [r for r in results if not r.is_negative]
    negatives = [r for r in results if r.is_negative]

    lines: list[str] = []
    lines += _header(meta, positives, negatives)
    lines += _procedure_version(meta)
    lines += _legend()
    lines += _section_a(positives)
    lines += _section_b(positives)
    lines += _section_c(negatives)
    lines += _section_d(positives, meta)
    lines += _section_e(rollback_evidence or [])
    lines += _integrity_footer(meta)
    lines += [
        "",
        "---",
        "",
        "_Generated by the DCC Enablement Configuration test console. Every figure above is "
        "derived from recorded run evidence; no result is hand-entered._",
    ]
    return "\n".join(lines)


def _integrity_footer(meta: dict) -> list[str]:
    """Append a report integrity hash so the content can be verified."""
    content_hash = meta.get("content_hash")
    if not content_hash:
        return []
    return [
        "",
        "## Report integrity",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Content SHA-256 | `{content_hash}` |",
        f"| Environment | {meta.get('environment', '?')} |",
        f"| Generated at | {meta.get('generated_at', '?')} |",
        "",
        "To verify this report was not modified, re-generate it from the same session data "
        "and confirm the SHA-256 digest matches. The signed PDF version embeds this hash in "
        "the document metadata for tamper detection.",
    ]


def cab_report_with_hash(
    results: list[TestResult],
    meta: dict | None = None,
    rollback_evidence: list | None = None,
) -> tuple[str, str]:
    """Generate the CAB report and return (report_markdown, content_sha256)."""
    import hashlib

    meta = meta or {}
    # First pass without hash to compute content
    report = cab_report(results, meta, rollback_evidence)
    content_hash = hashlib.sha256(report.encode("utf-8")).hexdigest()
    # Second pass with hash embedded
    meta["content_hash"] = content_hash
    report = cab_report(results, meta, rollback_evidence)
    return report, content_hash


__all__ = ["cab_report", "cab_report_with_hash"]
