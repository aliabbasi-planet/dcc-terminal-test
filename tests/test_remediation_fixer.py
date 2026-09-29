"""Unit tests for the one-by-one live fixer (``dcc_console.remediation.fixer``).

A small in-memory SQL Server stand-in models exactly what the fixer reads (the
catalogue's verify tables and the handler rows) and what the procedure changes, so
the full plan → pre-check → dry run → apply → verify → log sequence runs through the
real ``execution.run_test`` path with no network. The restore journal is isolated
to a temp directory.
"""

from __future__ import annotations

import importlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from dcc_console.database import ProcedureOutput
from dcc_console.execution import build_call
from dcc_console.remediation import RemediationObjects, fixer, fixlog, mapping

OBJS = RemediationObjects("DEV_CORE_AAB")

ROW = {
    "TERMINAL_IDENTIFIER": "T1",
    "INSTANCE_IDENTIFIER": "I1",
    "LOCATION_NO": "L1",
    "MERCHANT_NAME": "Shop",
    "COUNTRY_NAME": "Spain",
    "TERMINAL_BRAND_NAME": "PAX",
    "TERMINAL_MODEL_NAME": "A920",
    "ACQUIRER_NAME": "Planet",
    **{column: 0 for column in mapping.TRACKED_CHECK_COLUMNS},
}


def _row(**broken) -> dict:
    return {**ROW, **broken}


def _step(check: str, **row_overrides) -> fixer.FixStep:
    steps, _ = fixer.plan_for_row(_row(**{check: 1}, **row_overrides))
    return next(s for s in steps if s.check_column == check)


# --------------------------------------------------------------------------- #
# In-memory SQL Server
# --------------------------------------------------------------------------- #

_FLAG = re.compile(r'config_name="(?P<name>[^"]+)" config_value="(?P<value>[^"]*)"')


def _handler_xml(**flags: str) -> str:
    inner = "".join(
        f'<extra_config config_name="{k}" config_value="{v}"/>' for k, v in flags.items()
    )
    return f"<extra_config>{inner}</extra_config>"


class FakeSqlServer:
    def __init__(self, server: str = "uat-sql", database: str = "3CDB") -> None:
        self.server, self.database = server, database
        self.connection = object()
        self.terminals: dict[str, str | None] = {}
        self.locations: dict[str, str | None] = {}
        self.handlers: dict[str, list[dict]] = {}
        self.calls: list[tuple[tuple, bool]] = []
        self.fail_reads = False

    # -- reads -----------------------------------------------------------------
    def scalar(self, sql: str, params=()):
        if self.fail_reads:
            raise RuntimeError("read failed")
        key = params[0]
        if "COUNT(*)" in sql:
            assert "is_deleted" in sql  # deleted rows must not count as existing
            if "[emv_terminal]" in sql:
                return int(key in self.terminals)
            if "[ccc].[location]" in sql:
                return int(key in self.locations)
            if "[cccintegrang].[instance]" in sql:
                return int(key in self.handlers)
        if "configdownload_version" in sql:
            return self.terminals.get(key)
        if "extra_function" in sql:
            return self.locations.get(key)
        if "package_config" in sql:
            return "<pc/>"
        raise AssertionError(f"unexpected scalar: {sql}")

    def query(self, sql: str, params=()):
        column = "receipt_config" if "receipt_config" in sql else "extra_config"
        rows = [
            {"handler_name": h["handler_name"], column: h[column]}
            for h in self.handlers.get(params[0], [])
        ]
        return pd.DataFrame(rows, columns=["handler_name", column])

    # -- the procedure ---------------------------------------------------------------
    def call_procedure(self, sql: str, params, rollback: bool) -> ProcedureOutput:
        self.calls.append((tuple(params), rollback))
        if rollback:  # dry run: nothing persists
            return ProcedureOutput(grids=[], messages=["simulated"], return_code=0)
        bit = params[0]
        target = next(iter(json.loads(params[1])[0].values()))
        grids: list[pd.DataFrame] = []
        if bit == 2:
            self.terminals[target] = {"Standard": "1", "ECB DCC": "2"}[params[2]]
        elif bit == 1:
            assert params[3] == 1, "a fix must ADD the function"
            self.locations[target] = (self.locations[target] or "") + f'<fn name="{params[2]}"/>'
        elif bit == 8:
            for handler in self.handlers[target]:
                handler["extra_config"] = _FLAG.sub(
                    lambda m: (
                        f'config_name="{m["name"]}" config_value="true"'
                        if m["name"] == params[2]
                        else m[0]
                    ),
                    handler["extra_config"],
                )
            grids.append(pd.DataFrame([{"rollback_script": "UPDATE handler SET ... prior"}]))
        elif bit == 16:
            for handler in self.handlers[target]:
                handler["receipt_config"] = f'<receipt template="{params[2]}"/>'
            grids.append(pd.DataFrame([{"rollback_script": "UPDATE handler SET ... prior"}]))
        return ProcedureOutput(grids=grids, messages=[], return_code=0)

    def safe_rollback(self) -> None:
        pass


@pytest.fixture
def journal(tmp_path, monkeypatch):
    import dcc_console.journal as journal_mod

    importlib.reload(journal_mod)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DIR", tmp_path)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DB", tmp_path / "journal.db")
    instance = journal_mod.RestoreJournal()
    monkeypatch.setattr(journal_mod, "_journal", instance)
    yield instance
    instance.close()


# --------------------------------------------------------------------------- #
# Planning and value rules
# --------------------------------------------------------------------------- #


def test_plan_splits_fixable_from_not_fixable_with_reasons():
    row = _row(
        HANDLER_DCCENABLEAUTH_CHECK_O=1,
        DCCXPRESSCO_CHECK_O=1,
        PRINTOUTTYPETEMPLATEDCC_CHECK_C=1,
        CONFIGDOWNLOAD_VERSION_CHECK_C=1,
        FIRMWARE_VERSION_CHECK=1,
        DCCMERCHANT_NO_CHECK_O=1,
    )
    steps, blocked = fixer.plan_for_row(row)
    by_check = {s.check_column: s for s in steps}
    assert by_check["HANDLER_DCCENABLEAUTH_CHECK_O"].config_value == "dccEnableAuth"
    assert by_check["HANDLER_DCCENABLEAUTH_CHECK_O"].target_identifier == "I1"
    assert by_check["DCCXPRESSCO_CHECK_O"].target_identifier == "L1"
    assert by_check["CONFIGDOWNLOAD_VERSION_CHECK_C"].config_value == "ECB DCC"
    assert by_check["CONFIGDOWNLOAD_VERSION_CHECK_C"].target_identifier == "T1"
    assert by_check["PRINTOUTTYPETEMPLATEDCC_CHECK_C"].config_value is None  # operator picks
    assert {b.check_column for b in blocked} == {"FIRMWARE_VERSION_CHECK", "DCCMERCHANT_NO_CHECK_O"}
    assert all(b.reason for b in blocked)


def test_every_fixable_check_plans_a_consistent_step():
    for flag in mapping.FLAG_FIXES:
        if not flag.fixable:
            continue
        step = _step(flag.check_column)
        assert step.definition.bit == flag.fix_bit
        assert step.target_identifier == ROW[flag.target_id_kind]


@pytest.mark.parametrize(
    "check", ["DCCXPRESSCO_CHECK_O", "DCCXPRESSCODT_CHECK_O", "DCCXPRESSCOFALLBACK_CHECK_O"]
)
def test_bit1_fix_always_adds_never_removes(check):
    """build_call maps anything but "Add" to @add = 0 — i.e. REMOVE the function."""
    step = _step(check)
    assert step.config_value == "Add"
    _, params, _ = build_call(step.definition, step.target_identifier, step.config_value, True)
    assert params[2] == step.flag.fix_value  # the function being fixed
    assert params[3] == 1  # @add


def test_missing_target_identifier_is_not_fixable():
    _, blocked = fixer.plan_for_row(_row(HANDLER_DCCENABLE_CHECK_O=1, INSTANCE_IDENTIFIER=None))
    assert blocked and "INSTANCE_IDENTIFIER" in blocked[0].reason


def test_resolved_checks_come_from_fixed_checks_json():
    steps, _ = fixer.plan_for_row(
        _row(
            HANDLER_DCCENABLE_CHECK_O=1,
            DCCXPRESSCO_CHECK_O=1,
            FIXED_CHECKS='[\n  "HANDLER_DCCENABLE_CHECK_O"\n]',
        )
    )
    resolved = {s.check_column: s.resolved for s in steps}
    assert resolved == {"HANDLER_DCCENABLE_CHECK_O": True, "DCCXPRESSCO_CHECK_O": False}


def test_template_and_substitute_change_the_signature():
    step = _step("PRINTOUTTYPETEMPLATEDCC_CHECK_C")
    with pytest.raises(ValueError):
        step.with_template("  ")
    chosen = step.with_template(" Planet_PAX ")
    assert chosen.config_value == "Add:Planet_PAX" and chosen.template == "Planet_PAX"
    moved = chosen.with_target("I_UAT")
    assert moved.is_substitute and not chosen.is_substitute
    assert len({s.signature("UAT") for s in (step, chosen, moved)}) == 3
    with pytest.raises(ValueError):
        _step("HANDLER_DCCENABLE_CHECK_O").with_template("x")


# --------------------------------------------------------------------------- #
# Live pre-check
# --------------------------------------------------------------------------- #


def test_bit8_precheck():
    step = _step("HANDLER_DCCENABLEAUTH_CHECK_O")
    sql = FakeSqlServer()
    sql.handlers["I1"] = [
        {"handler_name": "h1", "extra_config": _handler_xml(dccEnableAuth="true")},
        {"handler_name": "h2", "extra_config": _handler_xml(dccEnableAuth="false")},
    ]
    assert fixer.precheck(sql, step, "UAT").status == fixer.NEEDS_FIX
    sql.handlers["I1"][1]["extra_config"] = _handler_xml(dccEnableAuth="True")
    ok = fixer.precheck(sql, step, "UAT")
    assert ok.status == fixer.ALREADY_OK and ok.authoritative
    sql.handlers["I1"] = []
    assert fixer.precheck(sql, step, "UAT").status == fixer.NOT_FOUND


def test_bit1_precheck_matches_the_whole_function_name():
    step = _step("DCCXPRESSCO_CHECK_O")
    sql = FakeSqlServer()
    sql.locations["L1"] = '<fn name="DCCXpressCODT"/><fn name="DCCXpressCOFallback"/>'
    assert fixer.precheck(sql, step, "UAT").status == fixer.NEEDS_FIX  # CODT is not CO
    sql.locations["L1"] += '<fn name="DCCXpressCO"/>'
    ok = fixer.precheck(sql, step, "UAT")
    assert ok.status == fixer.ALREADY_OK and not ok.authoritative
    sql.locations["L1"] = None  # no extra functions at all
    assert fixer.precheck(sql, step, "UAT").status == fixer.NEEDS_FIX


def test_bit2_precheck_uses_the_version_code():
    step = _step("CONFIGDOWNLOAD_VERSION_CHECK_C")
    sql = FakeSqlServer()
    for code, status in (("1", fixer.NEEDS_FIX), (None, fixer.NEEDS_FIX), ("2", fixer.ALREADY_OK)):
        sql.terminals["T1"] = code
        assert fixer.precheck(sql, step, "UAT").status == status
    assert fixer.precheck(sql, step, "UAT").authoritative


def test_bit16_precheck_needs_a_template_and_matches_whole_names():
    step = _step("PRINTOUTTYPETEMPLATEDCC_CHECK_C")
    sql = FakeSqlServer()
    sql.handlers["I1"] = [{"handler_name": "h1", "receipt_config": '<r template="Planet_PAX"/>'}]
    assert fixer.precheck(sql, step, "UAT").status == fixer.UNKNOWN
    assert fixer.precheck(sql, step.with_template("Planet"), "UAT").status == fixer.NEEDS_FIX
    ok = fixer.precheck(sql, step.with_template("Planet_PAX"), "UAT")
    assert ok.status == fixer.ALREADY_OK and not ok.authoritative


def test_precheck_missing_target_and_read_errors():
    step = _step("CONFIGDOWNLOAD_VERSION_CHECK_C")
    sql = FakeSqlServer()
    assert fixer.precheck(sql, step, "UAT").status == fixer.NOT_FOUND
    sql.fail_reads = True
    assert fixer.precheck(sql, step, "UAT").status == fixer.UNKNOWN


# --------------------------------------------------------------------------- #
# The Apply-live gate
# --------------------------------------------------------------------------- #


def _ready(step, env="UAT", **overrides):
    pre = fixer.Precheck(fixer.NEEDS_FIX, "1", "", step.signature(env), time.time())
    dry = fixer.DryRun(step.signature(env), "PASS", time.time())
    kwargs = dict(
        step=step, environment=env, is_operator=True, armed_live=True, pre=pre, dry_run=dry
    )
    kwargs.update(overrides)
    return fixer.live_gate(**kwargs)


def test_gate_allows_a_fully_prepared_uat_fix():
    gate = _ready(_step("CONFIGDOWNLOAD_VERSION_CHECK_C"))
    assert gate.allowed, gate.reasons


def test_gate_blocks_prod_even_when_everything_else_is_ready():
    gate = _ready(_step("CONFIGDOWNLOAD_VERSION_CHECK_C"), env="PROD")
    assert not gate.allowed
    assert any("not enabled on PROD" in r for r in gate.reasons)


def test_gate_reasons():
    step = _step("CONFIGDOWNLOAD_VERSION_CHECK_C")
    sig = step.signature("UAT")
    stale = time.time() - fixer.FRESHNESS_S - 1
    cases = {
        "operator list": dict(is_operator=False),
        "Mode to Live": dict(armed_live=False),
        "Run the live pre-check": dict(pre=None),
        "older than 15 minutes": dict(pre=fixer.Precheck(fixer.NEEDS_FIX, "1", "", sig, stale)),
        "already correct": dict(pre=fixer.Precheck(fixer.ALREADY_OK, "2", "", sig, time.time())),
        "could not confirm": dict(pre=fixer.Precheck(fixer.UNKNOWN, None, "", sig, time.time())),
        "Run a dry run": dict(dry_run=None),
        "did not pass": dict(dry_run=fixer.DryRun(sig, "FAIL", time.time())),
    }
    for fragment, override in cases.items():
        gate = _ready(step, **override)
        assert not gate.allowed and any(fragment in r for r in gate.reasons), fragment


def test_gate_requires_a_dry_run_of_the_exact_value():
    step = _step("PRINTOUTTYPETEMPLATEDCC_CHECK_C").with_template("Planet_PAX")
    dry_on_other = fixer.DryRun(step.with_template("Six_PAX").signature("UAT"), "PASS", time.time())
    assert not _ready(step, dry_run=dry_on_other).allowed
    assert not _ready(_step("PRINTOUTTYPETEMPLATEDCC_CHECK_C")).allowed  # no template yet


def test_gate_refix_guard_respects_snapshot_time_and_rollbacks():
    step = _step("CONFIGDOWNLOAD_VERSION_CHECK_C")
    loaded = datetime(2026, 9, 29, 5, 0)  # snapshot source load (UTC wall-clock)
    after = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
    applied = {"OUTCOME": "APPLIED", "VERIFIED": True, "APPLIED_AT": after}
    assert not _ready(step, last_live=applied, source_last_altered=loaded).allowed
    rolled_back = {**applied, "OUTCOME": "ROLLED_BACK"}
    assert _ready(step, last_live=rolled_back, source_last_altered=loaded).allowed
    before_load = {**applied, "APPLIED_AT": after - timedelta(hours=6)}
    assert _ready(step, last_live=before_load, source_last_altered=loaded).allowed
    unverified = {**applied, "VERIFIED": False}
    assert _ready(step, last_live=unverified, source_last_altered=loaded).allowed


def test_run_step_refuses_prod_writes_without_the_gate():
    step = _step("CONFIGDOWNLOAD_VERSION_CHECK_C")
    sql = FakeSqlServer()
    sql.terminals["T1"] = "1"
    with pytest.raises(PermissionError):
        fixer.run_step(
            sql, step, simulation=False, environment="PROD", login="svc", correlation_id="c"
        )
    assert sql.calls == []
    with pytest.raises(ValueError):
        fixer.run_step(
            sql,
            _step("PRINTOUTTYPETEMPLATEDCC_CHECK_C"),
            simulation=True,
            environment="UAT",
            login="svc",
            correlation_id="c",
        )


def test_substitutes_are_never_allowed_on_prod():
    assert fixer.substitute_allowed("uat") and not fixer.substitute_allowed("PROD")


# --------------------------------------------------------------------------- #
# End to end: pre-check -> dry run -> apply -> verify -> log -> keep
# --------------------------------------------------------------------------- #


def test_bit2_full_cycle_on_uat(journal):
    step = _step("CONFIGDOWNLOAD_VERSION_CHECK_C")
    sql = FakeSqlServer()
    sql.terminals["T1"] = "1"

    pre = fixer.precheck(sql, step, "UAT")
    assert pre.status == fixer.NEEDS_FIX

    dry = fixer.run_step(
        sql, step, simulation=True, environment="UAT", login="svc", correlation_id="c1"
    )
    assert dry.status == "PASS" and sql.terminals["T1"] == "1"  # nothing persisted
    assert fixer.dry_run_outcome(dry).outcome == "SIMULATED"

    gate = _ready(
        step, pre=pre, dry_run=fixer.DryRun(step.signature("UAT"), dry.status, time.time())
    )
    assert gate.allowed, gate.reasons

    live = fixer.run_step(
        sql, step, simulation=False, environment="UAT", login="svc", correlation_id="c1"
    )
    assert sql.terminals["T1"] == "2"
    assert [rollback for _, rollback in sql.calls] == [True, False]
    assert live.journal_id is not None
    assert journal.get_entry(live.journal_id)["status"] == "PENDING"

    post = fixer.precheck(sql, step, "UAT")
    outcome = fixer.live_outcome(live, post)
    assert (outcome.outcome, outcome.verified) == ("APPLIED", True)

    covered = [_row(CONFIGDOWNLOAD_VERSION_CHECK_C=1)]
    records = fixer.log_records(
        step=step,
        covered_rows=covered,
        outcome=outcome.outcome,
        mode=outcome.mode,
        environment="uat",
        server=sql.server,
        database_name=sql.database,
        operator="ALIA",
        sql_login="svc",
        correlation_id="c1",
        verified=outcome.verified,
        result=live,
        pre=pre,
        post=post,
    )
    assert len(records) == 1
    record = records[0]
    assert record["ENVIRONMENT"] == "UAT" and record["VALUE_SENT"] == "ECB DCC"
    assert record["STATE_BEFORE"] == "1" and record["STATE_AFTER"] == "2"
    fixlog.build_insert(record, OBJS)  # every key is an allowed, well-formed column

    fixer.keep_fix(live)
    assert journal.pending_entries() == []  # a kept fix never reaches crash recovery


def test_bit8_live_fix_verifies_every_handler(journal):
    step = _step("HANDLER_DCCENABLEAUTH_CHECK_O")
    sql = FakeSqlServer()
    sql.handlers["I1"] = [
        {
            "handler_name": "h1",
            "extra_config": _handler_xml(dccEnable="true", dccEnableAuth="false"),
        },
    ]
    live = fixer.run_step(
        sql, step, simulation=False, environment="UAT", login="svc", correlation_id="c2"
    )
    assert live.sp_rollback_scripts  # the procedure's own undo was captured
    post = fixer.precheck(sql, step, "UAT")
    assert fixer.live_outcome(live, post).verified is True
    entry = journal.get_entry(live.journal_id)
    assert entry["restore_kind"] == "SP_SCRIPT"  # crash recovery would run the SP's script


def test_live_outcome_and_precheck_outcome_rules():
    step = _step("CONFIGDOWNLOAD_VERSION_CHECK_C")
    sig = step.signature("PROD")
    not_fixed = fixer.Precheck(fixer.NEEDS_FIX, "1", "still 1", sig, time.time())

    class R:
        error = None

    outcome = fixer.live_outcome(R(), not_fixed)
    assert (outcome.outcome, outcome.verified) == ("APPLIED", False) and "still 1" in outcome.error
    R.error = "boom"
    assert fixer.live_outcome(R(), None).outcome == "FAILED"

    exact = fixer.Precheck(fixer.ALREADY_OK, "2", "", sig, time.time(), authoritative=True)
    loose = fixer.Precheck(fixer.ALREADY_OK, "<x/>", "", sig, time.time())
    assert fixer.precheck_outcome(exact).verified is True
    assert fixer.precheck_outcome(loose).verified is False  # never hides the terminal
    assert fixer.precheck_outcome(not_fixed) is None
    missing = fixer.Precheck(fixer.NOT_FOUND, None, "gone", sig, time.time())
    assert fixer.precheck_outcome(missing).outcome == "NOT_FOUND"
    assert fixer.rollback_outcome(True, None).outcome == "ROLLED_BACK"
    assert fixer.rollback_outcome(False, "x").outcome == "FAILED"


def test_log_records_cover_every_terminal_and_note_substitutes():
    step = _step("HANDLER_DCCENABLE_CHECK_O").with_target("I_UAT")
    covered = [_row(TERMINAL_IDENTIFIER="T1"), _row(TERMINAL_IDENTIFIER="T2")]
    records = fixer.log_records(
        step=step,
        covered_rows=covered,
        outcome="SIMULATED",
        mode="SIMULATION",
        environment="UAT",
        server="s",
        database_name="d",
        operator="ALIA",
        sql_login="",
        correlation_id="c3",
        verified=None,
    )
    assert [r["TERMINAL_IDENTIFIER"] for r in records] == ["T1", "T2"]
    assert all(r["TARGET_IDENTIFIER"] == "I_UAT" for r in records)
    assert all("substitute" in r["NOTES"] and "I1" in r["NOTES"] for r in records)
    assert records[0]["SQL_LOGIN"] is None and records[0]["MERCHANT_NAME"] == "Shop"
    for record in records:
        fixlog.build_insert(record, OBJS)
