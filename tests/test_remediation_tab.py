"""Render tests for the DCC Remediation tab (Streamlit AppTest, no network).

A stand-in for :class:`SnowflakeConnection` answers the tab's reads with frames
shaped like the live ``DCC_REMEDIATION`` objects (same column names and types),
so the connected path — KPIs, charts, filters, worklist grid — renders the way it
does in the console.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from dcc_console.remediation.mapping import TRACKED_CHECK_COLUMNS
from dcc_console.remediation.sf_connection import SnowflakeSettings


def _worklist_frame() -> pd.DataFrame:
    """Two V_CURRENT_BROKEN rows covering every Snowflake type the view returns."""
    row = {
        "SNAPSHOT_DATE": date(2026, 9, 28),  # DATE
        "TERMINAL_IDENTIFIER": "T-0001",
        "LOCATION_NAME": "Shop 1",
        "COUNTRY_NAME": "Spain",
        "REGION": "EMEA",
        "TERMINAL_BRAND_NAME": "Brand A",
        "ACQUIRER_NAME": "Acquirer A",
        "BANK_MERCHANT_ID": "BM-1",
        "FIRMWARE_VERSION": "1.2.3",
        "IS_DCC_BROKEN": True,  # BOOLEAN
        "SOURCE_LAST_ALTERED": datetime(2026, 9, 28, 6, 0),  # TIMESTAMP_NTZ
        "CAPTURED_AT": datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc),  # TIMESTAMP_LTZ
        "LAST_FIX_AT": None,  # TIMESTAMP_LTZ, NULL until a live fix is logged
        "REMEDIATION_STATE": "ACTIONABLE",
        **{column: 1 for column in TRACKED_CHECK_COLUMNS},  # NUMBER(38,0)
    }
    fixed = {
        **row,
        "TERMINAL_IDENTIFIER": "T-0002",
        "LAST_FIX_AT": datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc),
    }
    return pd.DataFrame([row, fixed])


class _FakeSnowflake:
    """Duck-types SnowflakeConnection: canned reads, recorded writes."""

    def __init__(
        self, *, initialised: bool = True, loaded: bool = True, fail_writes: bool = False
    ) -> None:
        self.settings = SnowflakeSettings(account="acct", user="user", role="role", warehouse="wh")
        self.connection = object()  # non-None means "connected" to the tab
        self.initialised = initialised
        self.loaded = loaded
        self.fail_writes = fail_writes
        self.reads: list[str] = []
        self.writes: list[str] = []

    def query(self, sql: str, params=None) -> pd.DataFrame:
        self.reads.append(sql)
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return pd.DataFrame({"N": [3 if self.initialised else 0]})
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
                        "LATEST_SNAPSHOT_DATE": date(2026, 9, 28) if self.loaded else None,
                    }
                ]
            )
        if "SUM(" in sql:
            # SUM over zero rows is NULL in Snowflake, not 0.
            totals = {
                column: (index * 100 if self.loaded else None)
                for index, column in enumerate(TRACKED_CHECK_COLUMNS)
            }
            return pd.DataFrame([totals])
        if "AS CATEGORY" in sql:
            rows = [("EMEA", 9000), ("APAC", 3654)] if self.loaded else []
            return pd.DataFrame(rows, columns=["CATEGORY", "TERMINALS"])
        if "AS VALUE" in sql:
            return pd.DataFrame({"VALUE": ["A", "B"] if self.loaded else []})
        if sql.startswith("SELECT * FROM") and "V_CURRENT_BROKEN" in sql:
            frame = _worklist_frame()
            return frame if self.loaded else frame.iloc[0:0]
        raise AssertionError(f"Unexpected SQL from the tab: {sql}")

    def execute(self, sql: str, params=None) -> int:
        self.writes.append(sql)
        if self.fail_writes:
            raise RuntimeError("simulated write failure")
        return 0

    def close(self) -> None:
        self.connection = None


def _console_script() -> None:
    """The tab as the console renders it: after the sidebar's SQL-Server controls."""
    import streamlit as st

    from dcc_console.remediation.tab import render_remediation

    # ui/sidebar.py always renders this unkeyed button before the tabs.
    st.sidebar.button("Disconnect", use_container_width=True)
    render_remediation()


def _render(fake: _FakeSnowflake) -> AppTest:
    at = AppTest.from_function(_console_script, default_timeout=30)
    at.session_state["rem_sf_conn"] = fake
    return at.run()


def _problems(at: AppTest) -> list[str]:
    return [e.value for e in at.exception] + [e.value for e in at.error]


def test_connected_tab_renders_beside_sidebar_disconnect():
    at = _render(_FakeSnowflake())

    assert _problems(at) == []
    assert at.button(key="rem_sf_disconnect").label == "Disconnect"
    assert at.metric[0].value == "12,654"
    assert len(at.dataframe) == 1


def test_empty_snapshot_renders_without_errors():
    at = _render(_FakeSnowflake(loaded=False))

    assert _problems(at) == []
    assert any("No terminals match" in info.value for info in at.info)


def test_uninitialised_schema_only_offers_initialise():
    fake = _FakeSnowflake(initialised=False)
    at = _render(fake)

    assert _problems(at) == []
    assert any("not fully initialised" in warning.value for warning in at.warning)
    assert at.button(key="rem_init").label == "Initialise / verify my schema"
    assert all("INFORMATION_SCHEMA.TABLES" in sql for sql in fake.reads)


@pytest.mark.parametrize(
    ("button_key", "failure_text", "success_text"),
    [
        ("rem_refresh", "Refresh failed", "Snapshot refreshed."),
        ("rem_init", "Initialise failed", "is ready."),
    ],
)
def test_action_outcome_is_visible_after_click(button_key, failure_text, success_text):
    failing = _render(_FakeSnowflake(fail_writes=True))
    failing.button(key=button_key).click().run()

    assert [e.value for e in failing.exception] == []
    assert any(failure_text in e.value for e in failing.error)

    fake = _FakeSnowflake()
    ok = _render(fake)
    ok.button(key=button_key).click().run()

    assert _problems(ok) == []
    assert fake.writes
    assert any(success_text in s.value for s in ok.success)
