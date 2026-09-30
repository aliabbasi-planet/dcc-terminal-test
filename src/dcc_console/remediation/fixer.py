"""One-by-one live fixing: plan → pre-check → dry run → apply → verify → log.

Pure orchestration around the console's hardened procedure path
(:func:`dcc_console.execution.run_test` / :func:`~dcc_console.execution.apply_rollback`).
Nothing here imports Streamlit; the SQL-Server connection is passed in, so every
rule is unit-testable with fakes.

Safety rules (decisions recorded 2026-09-29):

* **Live writes on DEV/UAT and — behind a stronger gate — PROD.** A PROD live fix
  additionally requires a PROD-approved operator (``CAN_PROD`` on the allowlist) and a
  typed change reference; enforced by :func:`live_gate` and again inside
  :func:`run_step`.
* **Pre-check before anything.** A live read of the target decides whether a fix
  is needed at all; an already-correct target is logged, never re-applied (a Bit 1
  ``Add`` on an existing node is not proven idempotent).
* **Dry run of the exact step first**, in the same environment, recently.
* **Verify by a fresh live read** after the call — the same pre-check logic.
* **Log every attempt** (one fix-log row per worklist terminal the call covers).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..catalog import TEST_CATALOG, TestDefinition
from ..execution import ORIGIN_REMEDIATION, TestResult, run_test
from ..journal import get_journal
from ..rollback import read_sp_flag_states, read_statement
from . import mapping
from .mapping import FlagFix

# Environments where a live fix may be applied. PROD is allowed only behind the extra
# gate in live_gate (a CAN_PROD operator + a typed change reference) and run_step.
LIVE_ENVIRONMENTS: frozenset[str] = frozenset({"DEV", "UAT", "PROD"})
# The production environment, which carries the extra approval requirements.
PROD_ENVIRONMENT = "PROD"
# A pre-check / dry run older than this must be repeated before Apply.
FRESHNESS_S = 15 * 60
# Bit 2 version codes, per the catalogue's documented mapping (1 Standard, 2 ECB DCC).
_CONFIG_DOWNLOAD_CODES = {"Standard": "1", "ECB DCC": "2"}
# Fix-log column widths that must not be exceeded.
_VALUE_SENT_MAX = 120
_STATE_MAX = 16000

# Pre-check verdicts.
NEEDS_FIX = "NEEDS_FIX"
ALREADY_OK = "ALREADY_OK"
NOT_FOUND = "NOT_FOUND"
UNKNOWN = "UNKNOWN"

# Row columns copied into every fix-log row for analytics.
_DIMENSIONS = (
    "BANK_MERCHANT_ID",
    "MERCHANT_NAME",
    "CUSTOMER_NAME",
    "COUNTRY_NAME",
    "REGION",
    "INDUSTRY_NAME",
    "ACQUIRER_NAME",
    "TERMINAL_BRAND_NAME",
    "TERMINAL_MODEL_NAME",
)
_ROW_IDENTIFIERS = ("INSTANCE_IDENTIFIER", "TERMINAL_IDENTIFIER", "LOCATION_NO")


# --------------------------------------------------------------------------- #
# Planning: which checks on a worklist row can be fixed, and how
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FixStep:
    """One procedure call that repairs one broken check."""

    check_column: str
    flag: FlagFix
    definition: TestDefinition
    target_kind: str  # the worklist column holding the identifier (mapping.INSTANCE, ...)
    target_identifier: str
    config_value: str | None  # None until the operator confirms a Bit 16 template
    worklist_target: str  # the row's own identifier (differs only for a substitute)
    resolved: bool = False  # already settled in this snapshot (awaiting refresh)

    @property
    def needs_template(self) -> bool:
        return self.definition.bit == 16

    @property
    def is_substitute(self) -> bool:
        return self.target_identifier != self.worklist_target

    @property
    def template(self) -> str | None:
        if self.needs_template and self.config_value:
            return self.config_value.partition(":")[2] or None
        return None

    def with_template(self, template: str) -> FixStep:
        name = (template or "").strip()
        if not self.needs_template:
            raise ValueError("Only the Bit 16 receipt-template fix takes a template.")
        if not name:
            raise ValueError("Choose a receipt template first.")
        return replace(self, config_value=f"Add:{name}")

    def with_target(self, identifier: str) -> FixStep:
        target = (identifier or "").strip()
        if not target:
            raise ValueError("Enter the substitute target identifier.")
        return replace(self, target_identifier=target)

    def signature(self, environment: str) -> tuple[str, str, str, str | None]:
        """What a dry run must have covered for Apply to be allowed."""
        return (environment.upper(), self.check_column, self.target_identifier, self.config_value)


@dataclass(frozen=True)
class NotFixable:
    check_column: str
    flag: FlagFix
    reason: str


def resolved_checks(row: dict) -> set[str]:
    """Checks already settled for this snapshot (``FIXED_CHECKS``; JSON text or a list)."""
    raw = row.get("FIXED_CHECKS")
    if raw is None:
        return set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return set()
    return {str(value) for value in raw} if isinstance(raw, list | tuple | set) else set()


def _is_broken(value: object) -> bool:
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def _resolve_value(flag: FlagFix, definition: TestDefinition) -> str | None:
    """The config value to send, validated against the catalogue. Raises if inconsistent."""
    bit = definition.bit
    if bit == 1:
        # The call sends "Add"; ANY other value becomes @add = 0, i.e. REMOVE (see
        # execution.build_call). The function itself is fixed by the catalogue entry.
        if definition.function_name != flag.fix_value:
            raise ValueError(
                f"Mapping says {flag.fix_value!r} but the catalogue entry adds "
                f"{definition.function_name!r}."
            )
        return "Add"
    if bit in (2, 8):
        options = definition.value_options or ()
        if flag.fix_value not in options:
            raise ValueError(f"{flag.fix_value!r} is not a valid value for Bit {bit}.")
        return flag.fix_value
    if bit == 16:
        return None  # chosen per terminal by the operator
    raise ValueError(f"Bit {bit} is not fixable from the worklist.")


def describe_checks(row: dict) -> str:
    """Readable summary of a row's broken checks for the worklist grid."""
    settled = resolved_checks(row)
    parts = []
    for flag in mapping.FLAG_FIXES:
        if not _is_broken(row.get(flag.check_column)):
            continue
        label = flag.flag_name if flag.fixable else f"{flag.flag_name} (manual)"
        if flag.check_column in settled:
            label += " (fixed)"
        parts.append(label)
    return " · ".join(parts)


def plan_for_row(row: dict) -> tuple[list[FixStep], list[NotFixable]]:
    """Split a worklist row's broken checks into fix steps and not-fixable findings."""
    settled = resolved_checks(row)
    steps: list[FixStep] = []
    blocked: list[NotFixable] = []
    for flag in mapping.FLAG_FIXES:
        if not _is_broken(row.get(flag.check_column)):
            continue
        if not flag.fixable or flag.catalog_key is None or flag.target_id_kind is None:
            blocked.append(
                NotFixable(flag.check_column, flag, flag.notes or "Not fixable by this procedure.")
            )
            continue
        target = str(row.get(flag.target_id_kind) or "").strip()
        if not target:
            blocked.append(
                NotFixable(flag.check_column, flag, f"The row has no {flag.target_id_kind}.")
            )
            continue
        definition = TEST_CATALOG[flag.catalog_key]
        try:
            value = _resolve_value(flag, definition)
        except ValueError as exc:
            blocked.append(NotFixable(flag.check_column, flag, f"Mapping error: {exc}"))
            continue
        steps.append(
            FixStep(
                check_column=flag.check_column,
                flag=flag,
                definition=definition,
                target_kind=flag.target_id_kind,
                target_identifier=target,
                config_value=value,
                worklist_target=target,
                resolved=flag.check_column in settled,
            )
        )
    return steps, blocked


# --------------------------------------------------------------------------- #
# Live pre-check (also the post-apply verification)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Precheck:
    status: str  # NEEDS_FIX | ALREADY_OK | NOT_FOUND | UNKNOWN
    current: str | None  # what the live target holds now (display / audit)
    detail: str
    signature: tuple
    checked_at: float
    states: tuple[dict, ...] = ()
    # True when ALREADY_OK means the maintenance check itself is satisfied (Bit 8 flag
    # true on every handler, Bit 2 on ECB DCC). For Bit 1 it only means the node is
    # present and for Bit 16 only that the *chosen* template is set, so those never
    # settle the worklist row on their own.
    authoritative: bool = False

    @property
    def fresh(self) -> bool:
        return time.time() - self.checked_at <= FRESHNESS_S


def _token(name: str) -> re.Pattern:
    """Whole-name match: DCCXpressCO must not match inside DCCXpressCODT."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _exists(connection, definition: TestDefinition, identifier: str) -> bool:
    verify = definition.verify
    # Table and key are catalogue constants; the identifier is a bound parameter.
    # Every catalogue table carries is_deleted (as reference.py / broken.py rely on).
    sql = (
        f"SELECT COUNT(*) FROM {verify.table} "  # noqa: S608
        f"WHERE CONVERT(nvarchar(50), {verify.key}) = ? AND ISNULL(is_deleted, 0) = 0"
    )
    return int(connection.scalar(sql, (identifier,)) or 0) > 0


def _read_value(connection, definition: TestDefinition, identifier: str) -> tuple[bool, object]:
    """``(ok, value)`` — unlike ``read_state``, a NULL column is not a read failure."""
    try:
        return True, connection.scalar(read_statement(definition), (identifier,))
    except Exception:
        return False, None


def _clip(value: object, limit: int = 400) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _opt(value: object) -> str | None:
    """Stringify a value for a nullable text column, preserving NULL."""
    return None if value is None else str(value)


def precheck(connection, step: FixStep, environment: str) -> Precheck:
    """Read the live target and decide whether the fix is still needed."""
    signature = step.signature(environment)
    now = time.time()

    def verdict(status, current, detail, states=(), authoritative=False) -> Precheck:
        return Precheck(
            status, _clip(current), detail, signature, now, tuple(states), authoritative
        )

    definition = step.definition
    target = step.target_identifier
    try:
        found = _exists(connection, definition, target)
    except Exception as exc:  # a read problem must never look like "needs fix"
        return verdict(UNKNOWN, None, f"Could not read the target: {exc}")
    if not found:
        return verdict(
            NOT_FOUND, None, f"{step.target_kind} {target} does not exist in {environment}."
        )

    bit = definition.bit
    if bit == 8:
        flag_name = step.config_value
        states = read_sp_flag_states(connection, target, flag_name, column="extra_config")
        if not states:
            return verdict(
                NOT_FOUND,
                None,
                f"Instance {target} has no DCC handler of the types the procedure edits.",
            )
        current = "; ".join(f"{s.get('handler_name')}={s.get('flag_value')}" for s in states)
        if all(str(s.get("flag_value")).strip().lower() == "true" for s in states):
            return verdict(
                ALREADY_OK,
                current,
                f"{flag_name} is already true on every handler.",
                states,
                authoritative=True,
            )
        return verdict(NEEDS_FIX, current, f"{flag_name} is not true on every handler.", states)

    if bit == 16:
        states = read_sp_flag_states(connection, target, None, column="receipt_config")
        if not states:
            return verdict(
                NOT_FOUND,
                None,
                f"Instance {target} has no DCC handler of the types the procedure edits.",
            )
        documents = [str(s.get("flag_value") or "") for s in states]
        current = " | ".join(_clip(d, 200) or "" for d in documents)
        template = step.template
        if template is None:
            return verdict(UNKNOWN, current, "Choose the receipt template to compare.", states)
        if all(_token(template).search(d) for d in documents):
            return verdict(
                ALREADY_OK,
                current,
                f"Every handler already uses {template}. If the maintenance check still "
                "flags it, the template itself may be the wrong one — investigate.",
                states,
            )
        return verdict(NEEDS_FIX, current, f"Not every handler uses {template}.", states)

    if bit == 1:
        function = definition.function_name or ""
        ok, document = _read_value(connection, definition, target)
        if not ok:
            return verdict(UNKNOWN, None, "Could not read the location's extra_function.")
        if document is not None and _token(function).search(str(document)):
            return verdict(
                ALREADY_OK,
                document,
                f"{function} is already on the location, so Add would change nothing. If "
                "the maintenance check still flags it, the node may be misconfigured — "
                "investigate manually.",
            )
        return verdict(NEEDS_FIX, document, f"{function} is missing from the location.")

    if bit == 2:
        wanted = _CONFIG_DOWNLOAD_CODES.get(step.config_value or "")
        ok, code = _read_value(connection, definition, target)
        if not ok or wanted is None:
            return verdict(UNKNOWN, code, "Could not read the terminal's config download version.")
        if code is not None and str(code).strip() == wanted:
            return verdict(
                ALREADY_OK,
                code,
                f"Already on {step.config_value} (code {wanted}).",
                authoritative=True,
            )
        return verdict(NEEDS_FIX, code, f"Code {code}; needs {step.config_value} (code {wanted}).")

    return verdict(UNKNOWN, None, f"No pre-check for Bit {bit}.")


# --------------------------------------------------------------------------- #
# Dry run / apply
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DryRun:
    signature: tuple
    status: str  # TestResult.status of the simulation
    at: float

    @property
    def fresh(self) -> bool:
        return time.time() - self.at <= FRESHNESS_S


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def run_step(
    connection,
    step: FixStep,
    *,
    simulation: bool,
    environment: str,
    login: str,
    correlation_id: str,
    change_ref: str | None = None,
) -> TestResult:
    """Run the step through the console's hardened procedure path.

    Independent backstops even if a caller skipped :func:`live_gate`: refuses a live
    call outside :data:`LIVE_ENVIRONMENTS`, and refuses a live PROD call that carries no
    change reference — the second, independent block on unapproved PROD writes.
    """
    if step.config_value is None:
        raise ValueError("Choose and confirm the receipt template before running this fix.")
    env = environment.upper()
    if not simulation and env not in LIVE_ENVIRONMENTS:
        raise PermissionError(f"Live fixes are not enabled on {environment}.")
    if not simulation and env == PROD_ENVIRONMENT and not (change_ref or "").strip():
        raise PermissionError("A change reference is required for a live PROD fix.")
    result = run_test(
        connection,
        step.definition,
        step.target_identifier,
        step.config_value,
        simulation=simulation,
        environment=environment,
        login=login,
        campaign_id=correlation_id,
    )
    result.origin = ORIGIN_REMEDIATION
    return result


# --------------------------------------------------------------------------- #
# The Apply-live gate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Gate:
    allowed: bool
    reasons: tuple[str, ...]


def _utc_naive(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value  # snapshot times are stored as UTC wall-clock
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def live_gate(
    *,
    step: FixStep,
    environment: str,
    is_operator: bool,
    armed_live: bool,
    pre: Precheck | None,
    dry_run: DryRun | None,
    last_live: dict | None = None,
    source_last_altered: object = None,
    can_prod: bool = False,
    change_ref: str | None = None,
) -> Gate:
    """Every reason Apply live is refused right now (empty = allowed).

    ``last_live`` is the latest live APPLIED / ROLLED_BACK fix-log row for this
    terminal + check in this environment (see ``fixlog.build_last_live_outcome_query``):
    a verified fix since the snapshot blocks a re-fix; a later rollback re-opens it.

    On PROD two more approvals are required on top of the DEV/UAT rules: the operator
    must be PROD-approved (``can_prod``) and a change reference must be supplied.
    """
    reasons: list[str] = []
    env = environment.upper()
    signature = step.signature(environment)
    if env not in LIVE_ENVIRONMENTS:
        reasons.append(
            f"Live fixes are not enabled on {env or 'this environment'} in this phase — "
            "run the pre-check and dry run only."
        )
    if env == PROD_ENVIRONMENT:
        if not can_prod:
            reasons.append(
                "PROD live fixes need a PROD-approved operator (CAN_PROD in FIX_OPERATORS)."
            )
        if not (change_ref or "").strip():
            reasons.append("Enter the change/CAB reference authorising this PROD fix.")
    if not is_operator:
        reasons.append("You are not on the live-fix operator list (FIX_OPERATORS).")
    if not armed_live:
        reasons.append("Switch the console Mode to Live and type the environment name to confirm.")
    if step.config_value is None:
        reasons.append("Choose and confirm the receipt template first.")
    if pre is None or pre.signature != signature:
        reasons.append("Run the live pre-check for this exact target and value.")
    elif not pre.fresh:
        reasons.append("The pre-check is older than 15 minutes — run it again.")
    elif pre.status == ALREADY_OK:
        reasons.append("The target is already correct — nothing to apply.")
    elif pre.status != NEEDS_FIX:
        reasons.append(f"The pre-check could not confirm a fix is needed ({pre.status}).")
    if dry_run is None or dry_run.signature != signature:
        reasons.append("Run a dry run of this exact fix first.")
    elif dry_run.status != "PASS":
        reasons.append(f"The last dry run did not pass ({dry_run.status}).")
    elif not dry_run.fresh:
        reasons.append("The dry run is older than 15 minutes — run it again.")
    if last_live and last_live.get("OUTCOME") == "APPLIED" and last_live.get("VERIFIED"):
        fixed_at = _utc_naive(last_live.get("APPLIED_AT"))
        loaded_at = _utc_naive(source_last_altered)
        if fixed_at is not None and (loaded_at is None or fixed_at > loaded_at):
            reasons.append(f"Already fixed live in {env} since this snapshot was built.")
    return Gate(not reasons, tuple(reasons))


def substitute_allowed(environment: str) -> bool:
    """Rehearsing on a substitute target is only ever allowed off PROD."""
    env = environment.upper()
    return env in LIVE_ENVIRONMENTS and env != PROD_ENVIRONMENT


# --------------------------------------------------------------------------- #
# Outcomes: what each attempt means for the fix log
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Outcome:
    outcome: str  # an fixlog.VALID_OUTCOMES value
    mode: str  # LIVE | SIMULATION
    verified: bool | None
    error: str | None = None


def precheck_outcome(pre: Precheck) -> Outcome | None:
    """Pre-checks are logged only when they settle something (else nothing to record)."""
    if pre.status == ALREADY_OK:
        # VERIFIED only when the read proves the maintenance check itself is satisfied,
        # so a Bit 1 / Bit 16 "already set" never hides a terminal from the worklist.
        return Outcome("SKIPPED_ALREADY_OK", "LIVE", pre.authoritative, None)
    if pre.status == NOT_FOUND:
        return Outcome("NOT_FOUND", "LIVE", None, pre.detail)
    return None


def dry_run_outcome(result: TestResult) -> Outcome:
    if result.status == "PASS":
        return Outcome("SIMULATED", "SIMULATION", None, None)
    return Outcome("FAILED", "SIMULATION", None, result.error or f"Dry run {result.status}")


def live_outcome(result: TestResult, post: Precheck | None) -> Outcome:
    """A committed call is APPLIED; VERIFIED only if a fresh live read confirms the fix."""
    if result.error:
        return Outcome("FAILED", "LIVE", False, result.error)
    verified = post is not None and post.status == ALREADY_OK
    error = None
    if not verified:
        detail = post.detail if post is not None else "no verification read"
        error = f"Verification did not confirm the fix: {detail}"
    return Outcome("APPLIED", "LIVE", verified, error)


def rollback_outcome(ok: bool, error: str | None, reverted: bool | None = None) -> Outcome:
    """Outcome of a rollback. ``reverted`` is a fresh read confirming the undo.

    VERIFIED means "confirmed reverted by a fresh SQL-Server read", not merely that the
    compensating script ran. A rollback whose reversion cannot be confirmed is still
    ROLLED_BACK but unverified, and carries a warning (the registry row is kept until a
    read confirms the check is open again).
    """
    if not ok:
        return Outcome("FAILED", "LIVE", False, f"Rollback failed: {error}")
    if reverted is False:
        return Outcome(
            "ROLLED_BACK",
            "LIVE",
            False,
            "The rollback ran but a fresh read did not confirm the change was reverted.",
        )
    return Outcome("ROLLED_BACK", "LIVE", reverted, None)


def rollback_confirmed(post: Precheck | None) -> bool:
    """True when a fresh read shows the change undone (the check is broken again)."""
    return post is not None and post.status == NEEDS_FIX


def should_register(outcome: Outcome) -> bool:
    """Whether a resolving outcome is verified enough to record in the fix registry.

    A committed fix confirmed by a fresh read, or a live pre-check that authoritatively
    proved the check already satisfied — both mean the check is genuinely resolved.
    """
    return outcome.outcome in ("APPLIED", "SKIPPED_ALREADY_OK") and bool(outcome.verified)


def registry_resolution(outcome: Outcome) -> str:
    """The registry ``RESOLUTION`` for a resolving outcome (APPLIED vs ALREADY_OK)."""
    return "APPLIED" if outcome.outcome == "APPLIED" else "ALREADY_OK"


def keep_fix(result: TestResult) -> None:
    """Take a verified, logged live fix out of crash recovery (see journal.mark_kept)."""
    if result.journal_id is not None:
        get_journal().mark_kept(result.journal_id)


# --------------------------------------------------------------------------- #
# Fix-log rows
# --------------------------------------------------------------------------- #


def _states_text(states: list[dict] | tuple[dict, ...], fallback: object) -> str | None:
    if states:
        return _clip(json.dumps(list(states), default=str), _STATE_MAX)
    return _clip(fallback, _STATE_MAX)


def log_records(
    *,
    step: FixStep,
    covered_rows: list[dict],
    outcome: str,
    mode: str,
    environment: str,
    server: str,
    database_name: str,
    operator: str,
    sql_login: str,
    correlation_id: str,
    verified: bool | None,
    result: TestResult | None = None,
    pre: Precheck | None = None,
    post: Precheck | None = None,
    error: str | None = None,
    notes: str | None = None,
    change_ref: str | None = None,
) -> list[dict]:
    """One ``APP_FIX_LOG`` record per worklist terminal the call covers."""
    if result is not None:
        before = _states_text(result.sp_prior_states, result.state_before)
        after = _states_text(result.sp_after_states, result.state_after)
        rollback = _clip("\n".join(result.sp_rollback_scripts), _STATE_MAX) or None
    else:
        before, after, rollback = None, None, None
    if pre is not None:
        before = before or _states_text(pre.states, pre.current)
    if post is not None:
        after = _states_text(post.states, post.current) or after
    if step.is_substitute:
        substitute_note = (
            f"Rehearsal on substitute {step.target_kind} {step.target_identifier} "
            f"(worklist target {step.worklist_target})."
        )
        notes = f"{substitute_note} {notes}".strip() if notes else substitute_note
    change = (change_ref or "").strip() or None
    records = []
    for row in covered_rows:
        record = {
            "CORRELATION_ID": correlation_id,
            "APPLIED_BY": operator,
            "SQL_LOGIN": sql_login or None,
            "MODE": mode,
            "ENVIRONMENT": environment.upper(),
            "SERVER": server,
            "DATABASE_NAME": database_name,
            "FIX_BIT": step.definition.bit,
            "CHECK_COLUMN": step.check_column,
            "FLAG_NAME": step.flag.flag_name,
            "TARGET_ID_KIND": step.target_kind,
            "TARGET_IDENTIFIER": step.target_identifier,
            "VALUE_SENT": _clip(step.config_value, _VALUE_SENT_MAX),
            "STATE_BEFORE": before,
            "STATE_AFTER": after,
            "VERIFIED": verified,
            "ROLLBACK_SCRIPT": rollback,
            "OUTCOME": outcome,
            "ERROR": _clip(error, _STATE_MAX),
            "NOTES": _clip(notes, 1000),
            "CHANGE_REF": _clip(change, 120),
        }
        for column in _ROW_IDENTIFIERS + _DIMENSIONS:
            value = row.get(column)
            record[column] = None if value is None else str(value)
        records.append(record)
    return records


def registry_records(
    *,
    step: FixStep,
    covered_rows: list[dict],
    environment: str,
    operator: str,
    correlation_id: str,
    resolution: str,
    change_ref: str | None = None,
    result: TestResult | None = None,
) -> list[dict]:
    """One ``DCC_FIX_REGISTRY`` upsert record per worklist terminal the call covers.

    Stores the procedure's own compensating script (when the bit is SP-managed) so a
    verified PROD fix can still be rolled back later, even from a fresh session.
    """
    rollback_script = None
    if result is not None and getattr(result, "sp_rollback_scripts", None):
        rollback_script = _clip("\n".join(result.sp_rollback_scripts), _STATE_MAX)
    change = (change_ref or "").strip() or None
    records = []
    for row in covered_rows:
        records.append(
            {
                "ENVIRONMENT": environment.upper(),
                "TERMINAL_IDENTIFIER": str(row.get("TERMINAL_IDENTIFIER")),
                "CHECK_COLUMN": step.check_column,
                "INSTANCE_IDENTIFIER": _opt(row.get("INSTANCE_IDENTIFIER")),
                "LOCATION_NO": _opt(row.get("LOCATION_NO")),
                "FIX_BIT": step.definition.bit,
                "FLAG_NAME": step.flag.flag_name,
                "VALUE_SENT": _clip(step.config_value, _VALUE_SENT_MAX),
                "TARGET_ID_KIND": step.target_kind,
                "TARGET_IDENTIFIER": step.target_identifier,
                "CORRELATION_ID": correlation_id,
                "CHANGE_REF": _clip(change, 120),
                "RESOLUTION": resolution,
                "SP_ROLLBACK_SCRIPT": rollback_script,
                "FIXED_BY": operator,
            }
        )
    return records
