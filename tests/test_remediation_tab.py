"""Render tests for the DCC Remediation tab and its one-by-one fixer (AppTest, no network).

Stand-ins for the Snowflake session and the SQL-Server connection answer the tab's
reads with frames shaped like the live objects, so the connected path — KPIs,
charts, filters, the selectable worklist and the fix panel — renders the way it
does in the console. The restore journal is isolated to a temp directory.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from dcc_console.database import ProcedureOutput
from dcc_console.remediation.mapping import TRACKED_CHECK_COLUMNS
from dcc_console.remediation.sf_connection import SnowflakeSettings


def _row(tid: str, **broken) -> dict:
    """One V_CURRENT_BROKEN row covering every Snowflake type the view returns."""
    return {
        "TERMINAL_IDENTIFIER": tid,
        "SNAPSHOT_DATE": date(2026, 9, 28),  # DATE
        "INSTANCE_IDENTIFIER": "I-" + tid,
        "LOCATION_NO": "L-" + tid,
        "LOCATION_NAME": "Shop 1",
        "MERCHANT_NAME": "Merchant",
        "COUNTRY_NAME": "Spain",
        "REGION": "EMEA",
        "TERMINAL_BRAND_NAME": "PAX",
        "TERMINAL_MODEL_NAME": "A920",
        "ACQUIRER_NAME": "Planet",
        "BANK_MERCHANT_ID": "BM-1",
        "FIRMWARE_VERSION": "1.2.3",
        "IS_DCC_BROKEN": True,  # BOOLEAN
        "SOURCE_LAST_ALTERED": datetime(2026, 9, 28, 5, 0),  # TIMESTAMP_NTZ (UTC)
        "CAPTURED_AT": datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc),  # TIMESTAMP_LTZ
        "LAST_FIX_AT_UTC": None,
        "FIXED_CHECKS": "[]",  # ARRAY arrives as JSON text
        "OPEN_FIXABLE_CHECKS": 1,
        "REMEDIATION_STATE": "ACTIONABLE",
        **{column: 0 for column in TRACKED_CHECK_COLUMNS},  # NUMBER(38,0)
        **broken,
    }


def _all_broken_frame() -> pd.DataFrame:
    everything = {column: 1 for column in TRACKED_CHECK_COLUMNS}
    return pd.DataFrame([_row("T-0001", **everything), _row("T-0002", **everything)])


class FakeSnowflake:
    """Duck-types SnowflakeConnection: canned reads, recorded writes."""

    def __init__(
        self,
        *,
        rows: list[dict] | None = None,
        initialised: bool = True,
        loaded: bool = True,
        fail_writes: bool = False,
        operator: bool = True,
        can_prod: bool = False,
    ) -> None:
        self.settings = SnowflakeSettings(account="acct", user="user", role="role", warehouse="wh")
        self.connection = object()  # non-None means "connected" to the tab
        self.frame = pd.DataFrame(rows) if rows is not None else _all_broken_frame()
        self.initialised = initialised
        self.loaded = loaded
        self.fail_writes = fail_writes
        self.operator = operator
        self.can_prod = can_prod
        self.reads: list[str] = []
        self.writes: list[tuple[str, list]] = []

    def query(self, sql: str, params=None) -> pd.DataFrame:
        self.reads.append(sql)
        params = list(params or [])
        if "INFORMATION_SCHEMA" in sql:
            full = self.initialised
            return pd.DataFrame(
                [
                    {
                        "OWN_TABLES": 2 * full,
                        "SHARED_TABLES": 3 * full,
                        "FIX_LOG_COLUMNS": 3 * full,
                        "OPERATOR_COLUMNS": int(full),
                        "VIEW_CURRENT": int(full),
                    }
                ]
            )
        if not self.initialised:
            raise RuntimeError("Object does not exist or not authorized.")
        if "V_REMEDIATION_KPIS" in sql:
            return pd.DataFrame(
                [
                    {
                        "BROKEN_ACTIONABLE": 12654 if self.loaded else 0,
                        "AWAITING_REFRESH": 0,
                        "TOTAL_LIVE_FIXES": 0,
                        "UNIQUE_TERMINALS_FIXED": 0,
                        "REHEARSAL_FIXES": 0,
                        "REGISTERED_FIXES": 0,
                        "LATEST_SNAPSHOT_DATE": date(2026, 9, 28) if self.loaded else None,
                    }
                ]
            )
        if "IS_OPERATOR" in sql:
            return pd.DataFrame(
                [
                    {
                        "USER_NAME": "ALIA",
                        "IS_OPERATOR": int(self.operator),
                        "CAN_PROD": int(self.can_prod),
                    }
                ]
            )
        if "SUM(" in sql:
            # SUM over zero rows is NULL in Snowflake, not 0.
            totals = {
                c: (i * 100 if self.loaded else None) for i, c in enumerate(TRACKED_CHECK_COLUMNS)
            }
            return pd.DataFrame([totals])
        if "AS CATEGORY" in sql:
            rows = [("EMEA", 9000), ("APAC", 3654)] if self.loaded else []
            return pd.DataFrame(rows, columns=["CATEGORY", "TERMINALS"])
        if "AS VALUE" in sql:
            return pd.DataFrame({"VALUE": ["A", "B"] if self.loaded else []})
        if "AS TEMPLATE" in sql:
            return pd.DataFrame({"TEMPLATE": ["Planet_PAX", "Six_PAX"], "TERMINALS": [95, 5]})
        if sql.startswith("SELECT OUTCOME, VERIFIED, APPLIED_AT"):
            return pd.DataFrame(columns=["OUTCOME", "VERIFIED", "APPLIED_AT"])
        if sql.startswith("SELECT APPLIED_AT, APPLIED_BY"):
            return pd.DataFrame()
        frame = self.frame if self.loaded else self.frame.iloc[0:0]
        if "V_CURRENT_BROKEN" in sql and "= %s AND" in sql:  # terminals one call covers
            column = sql.split("WHERE ", 1)[1].split(" = %s", 1)[0].strip()
            return frame[frame[column].astype(str) == str(params[0])]
        if "V_CURRENT_BROKEN WHERE TERMINAL_IDENTIFIER = %s" in sql:  # one terminal
            return frame[frame["TERMINAL_IDENTIFIER"] == params[0]]
        if sql.startswith("SELECT * FROM") and "V_CURRENT_BROKEN" in sql:
            return frame
        raise AssertionError(f"Unexpected SQL from the tab: {sql}")

    def execute(self, sql: str, params=None) -> int:
        self.writes.append((sql, list(params or [])))
        if self.fail_writes:
            raise RuntimeError("simulated write failure")
        return 0

    def close(self) -> None:
        self.connection = None

    def fix_log_rows(self) -> list[dict]:
        """Decode the multi-row fix-log INSERTs written so far."""
        rows = []
        for sql, params in self.writes:
            if "INSERT INTO" not in sql or "APP_FIX_LOG" not in sql:
                continue
            columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
            for start in range(0, len(params), len(columns)):
                rows.append(dict(zip(columns, params[start : start + len(columns)], strict=True)))
        return rows


class FakeSql:
    """The SQL-Server side for a Bit 2 (config download version) fix."""

    def __init__(self, version: str | None = "1") -> None:
        self.server, self.database = "uat-sql", "3CDB"
        self.connection = object()
        self.version = version
        self.calls: list[tuple[tuple, bool]] = []

    def scalar(self, sql: str, params=()):
        if "COUNT(*)" in sql:
            return 1
        if "configdownload_version" in sql:
            return self.version
        return None

    def query(self, sql: str, params=()):
        return pd.DataFrame([])

    def call_procedure(self, sql: str, params, rollback: bool) -> ProcedureOutput:
        self.calls.append((tuple(params), rollback))
        if not rollback and params[0] == 2:
            self.version = {"Standard": "1", "ECB DCC": "2"}[params[2]]
        return ProcedureOutput(grids=[], messages=[], return_code=0)

    def execute_write(self, sql: str, params=()) -> int:
        self.version = params[0]
        return 1

    def safe_rollback(self) -> None:
        pass


@pytest.fixture(autouse=True)
def journal(tmp_path, monkeypatch):
    import dcc_console.journal as journal_mod

    importlib.reload(journal_mod)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DIR", tmp_path)
    monkeypatch.setattr(journal_mod, "_JOURNAL_DB", tmp_path / "journal.db")
    instance = journal_mod.RestoreJournal()
    monkeypatch.setattr(journal_mod, "_journal", instance)
    yield instance
    instance.close()


def _console_script() -> None:
    """The tab as the console renders it: after the sidebar's SQL-Server controls."""
    import streamlit as st

    from dcc_console.remediation.tab import render_remediation

    # ui/sidebar.py always renders this unkeyed button before the tabs.
    st.sidebar.button("Disconnect", use_container_width=True)
    render_remediation(armed_live=st.session_state.get("_test_armed", False))


def _render(fake: FakeSnowflake, **state) -> AppTest:
    at = AppTest.from_function(_console_script, default_timeout=30)
    at.session_state["rem_sf_conn"] = fake
    at.session_state["results"] = []
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def _problems(at: AppTest) -> list[str]:
    return [e.value for e in at.exception] + [e.value for e in at.error]


def _bit2_row(tid: str = "T-9") -> dict:
    return _row(tid, CONFIGDOWNLOAD_VERSION_CHECK_C=1)


def _fixer_state(env: str, sql: FakeSql, tid: str = "T-9", armed: bool = False) -> dict:
    return {
        "connection": sql,
        "connected_env": env,
        "connected_login": "svc",
        "rem_selected_terminal": tid,
        "_test_armed": armed,
    }


def _reviewed_checkbox(at: AppTest):
    return next(c for c in at.checkbox if c.label.startswith("I have reviewed this exact EXEC"))


# --------------------------------------------------------------------------- #
# the tab
# --------------------------------------------------------------------------- #


def test_connected_tab_renders_beside_sidebar_disconnect():
    at = _render(FakeSnowflake())

    assert _problems(at) == []
    assert at.button(key="rem_sf_disconnect").label == "Disconnect"
    assert at.metric[0].value == "12,654"
    assert len(at.dataframe) == 1
    assert any("Select a terminal" in info.value for info in at.info)


def test_empty_snapshot_renders_without_errors():
    at = _render(FakeSnowflake(loaded=False))

    assert _problems(at) == []
    assert any("No terminals match" in info.value for info in at.info)


def test_uninitialised_schema_only_offers_initialise():
    fake = FakeSnowflake(initialised=False)
    at = _render(fake)

    assert _problems(at) == []
    assert any("needs initialising" in warning.value for warning in at.warning)
    assert at.button(key="rem_init").label == "Initialise / verify my schema"
    assert all("INFORMATION_SCHEMA" in sql for sql in fake.reads)


@pytest.mark.parametrize(
    ("button_key", "failure_text", "success_text"),
    [
        ("rem_refresh", "Refresh failed", "Snapshot refreshed."),
        ("rem_init", "Initialise failed", "is ready."),
    ],
)
def test_action_outcome_is_visible_after_click(button_key, failure_text, success_text):
    failing = _render(FakeSnowflake(fail_writes=True))
    failing.button(key=button_key).click().run()

    assert [e.value for e in failing.exception] == []
    assert any(failure_text in e.value for e in failing.error)

    fake = FakeSnowflake()
    ok = _render(fake)
    ok.button(key=button_key).click().run()

    assert _problems(ok) == []
    assert fake.writes
    assert any(success_text in s.value for s in ok.success)


def test_initialise_creates_the_shared_log_before_the_views():
    fake = FakeSnowflake()
    at = _render(fake)
    at.button(key="rem_init").click().run()
    statements = [sql for sql, _ in fake.writes]
    log_at = next(
        i for i, s in enumerate(statements) if "TABLE IF NOT EXISTS" in s and "APP_FIX_LOG" in s
    )
    view_at = next(i for i, s in enumerate(statements) if "V_CURRENT_BROKEN AS" in s)
    assert log_at < view_at
    assert any("FIX_OPERATORS" in s for s in statements)


# --------------------------------------------------------------------------- #
# the fixer
# --------------------------------------------------------------------------- #


def test_prod_pre_check_and_dry_run_work_but_apply_needs_approval_and_change_ref():
    snow = FakeSnowflake(rows=[_bit2_row()], can_prod=False)
    sql = FakeSql()
    at = _render(snow, **_fixer_state("PROD", sql, armed=True))

    assert _problems(at) == []
    assert any("PRODUCTION" in w.value for w in at.warning)  # the stern PROD banner
    assert at.button(key="rem_apply_T-9").disabled

    at.button(key="rem_pre_T-9").click().run()
    at.button(key="rem_dry_T-9").click().run()

    assert _problems(at) == []
    # PROD is enabled now, but this operator is not CAN_PROD and gave no change ref.
    assert at.button(key="rem_apply_T-9").disabled
    reasons = " ".join(m.value for m in at.markdown)
    assert "PROD-approved operator" in reasons and "change/CAB reference" in reasons
    assert [rollback for _, rollback in sql.calls] == [True]  # one dry run, rolled back
    logged = snow.fix_log_rows()
    assert [(r["MODE"], r["OUTCOME"], r["ENVIRONMENT"]) for r in logged] == [
        ("SIMULATION", "SIMULATED", "PROD")
    ]
    assert sql.version == "1"


def test_prod_apply_with_approval_and_change_ref_verifies_and_registers(journal):
    snow = FakeSnowflake(rows=[_bit2_row()], can_prod=True)
    sql = FakeSql()
    at = _render(snow, **_fixer_state("PROD", sql, armed=True))

    at.text_input(key="rem_change_ref_T-9").set_value("CHG0043215").run()
    at.button(key="rem_pre_T-9").click().run()
    at.button(key="rem_dry_T-9").click().run()
    assert _problems(at) == []
    _reviewed_checkbox(at).check().run()
    assert not at.button(key="rem_apply_T-9").disabled
    at.button(key="rem_apply_T-9").click().run()

    assert _problems(at) == []
    assert sql.version == "2"
    live = [r for r in snow.fix_log_rows() if r["MODE"] == "LIVE"]
    assert [(r["OUTCOME"], r["VERIFIED"], r["ENVIRONMENT"]) for r in live] == [
        ("APPLIED", True, "PROD")
    ]
    assert live[0]["CHANGE_REF"] == "CHG0043215"
    # The verified PROD fix is registered in the durable registry.
    assert any("DCC_FIX_REGISTRY" in s for s, _ in snow.writes)
    assert any("verified" in s.value for s in at.success)


def test_uat_operator_full_cycle_logs_a_verified_fix_and_keeps_it(journal):
    snow = FakeSnowflake(rows=[_bit2_row()])
    sql = FakeSql()
    at = _render(snow, **_fixer_state("UAT", sql, armed=True))

    at.button(key="rem_pre_T-9").click().run()
    at.button(key="rem_dry_T-9").click().run()
    assert _problems(at) == []
    assert at.button(key="rem_apply_T-9").disabled  # not until the EXEC is reviewed
    assert any("@display_config" in code.value for code in at.code)

    _reviewed_checkbox(at).check().run()
    assert not at.button(key="rem_apply_T-9").disabled
    at.button(key="rem_apply_T-9").click().run()

    assert _problems(at) == []
    assert sql.version == "2"
    assert [rollback for _, rollback in sql.calls] == [True, False]
    live = [r for r in snow.fix_log_rows() if r["MODE"] == "LIVE"]
    assert [(r["OUTCOME"], r["VERIFIED"], r["ENVIRONMENT"]) for r in live] == [
        ("APPLIED", True, "UAT")
    ]
    assert live[0]["VALUE_SENT"] == "ECB DCC" and live[0]["SQL_LOGIN"] == "svc"
    assert journal.pending_entries() == []  # a verified, logged fix leaves crash recovery
    assert any("verified" in s.value for s in at.success)
    assert at.session_state["results"][0].origin == "remediation"


def test_non_operator_cannot_apply_even_when_armed():
    snow = FakeSnowflake(rows=[_bit2_row()], operator=False)
    at = _render(snow, **_fixer_state("UAT", FakeSql(), armed=True))
    at.button(key="rem_pre_T-9").click().run()
    at.button(key="rem_dry_T-9").click().run()

    assert _problems(at) == []
    assert at.button(key="rem_apply_T-9").disabled
    reasons = " ".join(m.value for m in at.markdown)
    assert "operator list" in reasons


def test_bit16_needs_a_confirmed_template_before_anything_runs():
    row = _row("T-16", PRINTOUTTYPETEMPLATEDCC_CHECK_C=1)
    at = _render(FakeSnowflake(rows=[row]), **_fixer_state("UAT", FakeSql(), tid="T-16"))

    assert _problems(at) == []
    assert any("Planet_PAX" in s.value for s in at.success)  # 95% of peers → strong
    assert at.button(key="rem_pre_T-16").disabled
    assert at.selectbox(key="rem_tpl_T-16").value == "Planet_PAX"

    next(c for c in at.checkbox if c.label.startswith("I confirm")).check().run()
    assert not at.button(key="rem_pre_T-16").disabled


def test_failed_fix_log_write_is_queued_and_retried():
    snow = FakeSnowflake(rows=[_bit2_row()], fail_writes=True)
    at = _render(snow, **_fixer_state("PROD", FakeSql()))
    at.button(key="rem_dry_T-9").click().run()

    assert [e.value for e in at.exception] == []
    assert any("queued" in e.value for e in at.error)
    assert len(at.session_state["rem_pending_logs"]) == 1

    snow.fail_writes = False
    at.button(key="rem_retry_logs").click().run()
    assert at.session_state["rem_pending_logs"] == []
    assert [r["OUTCOME"] for r in snow.fix_log_rows()] == ["SIMULATED", "SIMULATED"]
