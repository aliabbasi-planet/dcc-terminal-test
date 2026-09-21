"""Tests for the trace model: fnDisplayTrace is a scalar PRINT formatter, so the
trace is the captured message stream (``cursor.messages``), not a queried table."""

from __future__ import annotations

import pandas as pd

from dcc_console.negatives import _looks_rejected
from dcc_console.trace import (
    MAX_TRACE_ROWS,
    TraceOutput,
    discover_trace_signature,
    trace_from_messages,
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
# signature discovery (documentation only — confirms scalar formatter)
# ---------------------------------------------------------------------------


def test_discovers_scalar_formatter():
    frame = pd.DataFrame([{"fn_exists": 1, "is_scalar": 1, "param_count": 2}])
    sig = discover_trace_signature(FakeConnection([frame]))
    assert sig.exists
    assert sig.is_scalar
    assert sig.parameter_count == 2
    assert sig.confirmed_print_formatter


def test_non_scalar_function_is_flagged():
    frame = pd.DataFrame([{"fn_exists": 1, "is_scalar": 0, "param_count": 2}])
    sig = discover_trace_signature(FakeConnection([frame]))
    assert sig.exists
    assert not sig.is_scalar
    assert not sig.confirmed_print_formatter
    assert "scalar" in sig.unavailable_reason


def test_missing_function_is_reported_not_raised():
    frame = pd.DataFrame([{"fn_exists": 0, "is_scalar": None, "param_count": None}])
    sig = discover_trace_signature(FakeConnection([frame]))
    assert not sig.exists
    assert not sig.confirmed_print_formatter
    assert "does not exist" in sig.unavailable_reason


def test_inspection_error_is_captured_as_reason():
    sig = discover_trace_signature(FakeConnection([RuntimeError("no permission")]))
    assert not sig.exists
    assert "no permission" in sig.unavailable_reason


def test_signature_as_dict_documents_capture_mechanism():
    frame = pd.DataFrame([{"fn_exists": 1, "is_scalar": 1, "param_count": 2}])
    sig = discover_trace_signature(FakeConnection([frame]))
    payload = sig.as_dict()
    assert payload["confirmed_print_formatter"] is True
    assert "cursor.messages" in payload["capture_mechanism"]


# ---------------------------------------------------------------------------
# trace_from_messages: the message stream IS the trace
# ---------------------------------------------------------------------------


def test_trace_from_messages_captures_each_line():
    out = trace_from_messages(["validating instance", "instance not found"])
    assert out.status == "CAPTURED"
    assert out.rows == [
        {"line": 1, "message": "validating instance"},
        {"line": 2, "message": "instance not found"},
    ]
    assert "instance not found" in out.text


def test_trace_from_messages_empty_stream():
    out = trace_from_messages([])
    assert out.status == "EMPTY"
    assert out.rows == []
    assert "no PRINT" in out.reason


def test_trace_from_messages_truncates_and_reports():
    out = trace_from_messages([f"line {i}" for i in range(MAX_TRACE_ROWS + 25)])
    assert out.status == "CAPTURED"
    assert len(out.rows) == MAX_TRACE_ROWS
    assert str(MAX_TRACE_ROWS + 25) in out.reason


def test_trace_output_text_is_empty_when_no_rows():
    assert TraceOutput().text == ""


def test_trace_output_text_joins_only_messages():
    out = TraceOutput(rows=[{"line": 1, "message": "hello"}], status="CAPTURED")
    # line numbers must not pollute the searchable blob
    assert out.text == "hello"


# ---------------------------------------------------------------------------
# the payoff: trace-only rejection is detected (trace == messages now)
# ---------------------------------------------------------------------------


def test_rejection_found_only_in_trace_is_detected():
    assert _looks_rejected([], [], "Instance identifier does not exist") is True


def test_no_rejection_anywhere_is_still_not_rejected():
    assert _looks_rejected([], [], "Applied configuration successfully") is False


def test_trace_text_is_optional_for_backwards_compatibility():
    assert _looks_rejected(["Error: invalid target"], []) is True
