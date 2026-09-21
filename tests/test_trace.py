"""Tests for the procedure trace reader and message accumulation."""

from __future__ import annotations

import pandas as pd
import pytest

from dcc_console.negatives import _looks_rejected
from dcc_console.trace import (
    TraceOutput,
    TraceSignature,
    _build_arguments,
    discover_trace_signature,
    read_trace,
)


class FakeConnection:
    """Minimal DatabaseConnection stand-in driven by queued responses."""

    def __init__(self, responses: list, server: str = "SRV", database: str = "DB") -> None:
        self._responses = list(responses)
        self.server = server
        self.database = database
        self.queries: list[tuple] = []

    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        self.queries.append((sql, params))
        if not self._responses:
            return pd.DataFrame()
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ---------------------------------------------------------------------------
# signature discovery
# ---------------------------------------------------------------------------


def test_discovers_table_valued_function_with_no_parameters():
    meta = pd.DataFrame([{"fn_exists": 1, "is_tvf": 1, "is_scalar": 0}])
    params = pd.DataFrame()
    sig = discover_trace_signature(FakeConnection([meta, params]))
    assert sig.exists
    assert sig.is_table_valued
    assert sig.usable
    assert sig.call_sql.startswith("SELECT * FROM ")
    assert "()" in sig.call_sql


def test_discovers_scalar_function_with_no_parameters():
    meta = pd.DataFrame([{"fn_exists": 1, "is_tvf": 0, "is_scalar": 1}])
    sig = discover_trace_signature(FakeConnection([meta, pd.DataFrame()]))
    assert sig.usable
    assert "AS trace_output" in sig.call_sql


def test_session_parameter_is_filled_with_spid():
    meta = pd.DataFrame([{"fn_exists": 1, "is_tvf": 1, "is_scalar": 0}])
    params = pd.DataFrame(
        [{"parameter": "@SPID", "type_name": "int", "max_length": 4, "parameter_id": 1}]
    )
    sig = discover_trace_signature(FakeConnection([meta, params]))
    assert sig.usable
    assert "@@SPID" in sig.call_sql


def test_unknown_parameter_is_refused_rather_than_guessed():
    meta = pd.DataFrame([{"fn_exists": 1, "is_tvf": 1, "is_scalar": 0}])
    params = pd.DataFrame(
        [{"parameter": "@TraceKey", "type_name": "varchar", "max_length": 50, "parameter_id": 1}]
    )
    sig = discover_trace_signature(FakeConnection([meta, params]))
    assert sig.exists
    assert not sig.usable
    assert "cannot supply" in sig.unavailable_reason
    assert "@TraceKey" in sig.unavailable_reason


def test_missing_function_is_reported_not_raised():
    meta = pd.DataFrame([{"fn_exists": 0, "is_tvf": None, "is_scalar": None}])
    sig = discover_trace_signature(FakeConnection([meta]))
    assert not sig.exists
    assert not sig.usable
    assert "does not exist" in sig.unavailable_reason


def test_inspection_error_is_captured_as_reason():
    sig = discover_trace_signature(FakeConnection([RuntimeError("no permission")]))
    assert not sig.usable
    assert "no permission" in sig.unavailable_reason


@pytest.mark.parametrize(
    "name",
    ["@spid", "@SessionId", "@session_id", "@ConnectionId"],
)
def test_spid_hints_are_recognised(name):
    assert _build_arguments(({"parameter": name},)) == ["@@SPID"]


def test_non_session_parameter_returns_none():
    assert _build_arguments(({"parameter": "@Foo"},)) is None


# ---------------------------------------------------------------------------
# reading the trace
# ---------------------------------------------------------------------------


def test_read_trace_captures_rows():
    sig = TraceSignature(exists=True, is_table_valued=True, call_sql="SELECT * FROM f()")
    frame = pd.DataFrame([{"msg": "Instance not found"}])
    out = read_trace(FakeConnection([frame]), sig)
    assert out.status == "CAPTURED"
    assert out.rows == [{"msg": "Instance not found"}]
    assert "Instance not found" in out.text


def test_read_trace_reports_empty_separately_from_unavailable():
    sig = TraceSignature(exists=True, is_table_valued=True, call_sql="SELECT * FROM f()")
    out = read_trace(FakeConnection([pd.DataFrame()]), sig)
    assert out.status == "EMPTY"
    assert out.rows == []


def test_read_trace_reports_unusable_signature():
    sig = TraceSignature(
        exists=True, is_table_valued=True, unavailable_reason="takes unknown parameters"
    )
    out = read_trace(FakeConnection([]), sig)
    assert out.status == "UNAVAILABLE"
    assert "unknown parameters" in out.reason


def test_read_trace_reports_none_signature_as_not_attempted():
    out = read_trace(FakeConnection([]), None)
    assert out.status == "NOT_ATTEMPTED"


def test_read_trace_captures_execution_error():
    sig = TraceSignature(exists=True, is_table_valued=True, call_sql="SELECT * FROM f()")
    out = read_trace(FakeConnection([RuntimeError("boom")]), sig)
    assert out.status == "ERRORED"
    assert "boom" in out.reason


def test_trace_output_text_is_empty_when_no_rows():
    assert TraceOutput().text == ""


# ---------------------------------------------------------------------------
# the payoff: trace-only rejection is detected
# ---------------------------------------------------------------------------


def test_rejection_found_only_in_trace_is_detected():
    """The core fix: without the trace this would score NOT-REJECTED."""
    assert _looks_rejected([], [], "Instance identifier does not exist") is True


def test_no_rejection_anywhere_is_still_not_rejected():
    assert _looks_rejected([], [], "Applied configuration successfully") is False


def test_trace_text_is_optional_for_backwards_compatibility():
    assert _looks_rejected(["Error: invalid target"], []) is True
