"""Unit tests for the DCC maintenance agent client (``remediation.agent``).

No network: the pure helpers are tested directly, and :func:`agent.ask` is exercised
against a fake ``requests.post`` and a fake Snowflake connection.
"""

from __future__ import annotations

import pytest

from dcc_console.remediation import DCC_AGENT_FQN, agent


def test_agent_path_from_fqn_and_rejects_bad_identifiers():
    assert agent.agent_path(DCC_AGENT_FQN) == (
        "/api/v2/databases/PROD_PRESENTATION/schemas/CORTEX/agents/"
        "DCC_TERMINAL_MAINTENANCE_AGENT:run"
    )
    for bad in ("only.two", "a.b.c.d", "a.b.c;drop", "PROD..NAME"):
        with pytest.raises(ValueError):
            agent.agent_path(bad)


def test_scoped_question_prepends_identifiers():
    row = {"TERMINAL_IDENTIFIER": "00081922", "INSTANCE_IDENTIFIER": "I000014957"}
    scoped = agent.scoped_question(row, "is it healthy?")
    assert scoped == "For terminal 00081922 / instance I000014957: is it healthy?"
    assert agent.scoped_question(None, " hi ") == "hi"
    assert agent.scoped_question({}, "hi") == "hi"


def test_build_payload_includes_history_then_question():
    payload = agent.build_payload(
        "now what?",
        history=[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
    )
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert payload["messages"][-1]["content"][0]["text"] == "now what?"


def _sse(*lines: str) -> list[bytes]:
    return [line.encode("utf-8") for line in lines]


def test_parse_sse_accumulates_text_deltas():
    answer = agent.parse_sse(
        _sse(
            "event: response.text.delta",
            'data: {"text": "Hello "}',
            "",
            "event: response.text.delta",
            'data: {"text": "world"}',
            "",
            "data: [DONE]",
        )
    )
    assert answer.ok and answer.text == "Hello world" and answer.error is None


def test_parse_sse_full_text_without_deltas():
    answer = agent.parse_sse(_sse("event: response.text", 'data: {"text": "All at once"}'))
    assert answer.text == "All at once"


def test_parse_sse_captures_sql_and_surfaces_errors():
    with_sql = agent.parse_sse(
        _sse('data: {"text": "see below", "sql": "SELECT 1"}')
    )
    assert with_sql.sql == ["SELECT 1"]
    err = agent.parse_sse(_sse("event: error", 'data: {"error": {"message": "no access"}}'))
    assert err.error == "no access" and not err.ok
    empty = agent.parse_sse(_sse("data: not-json", ""))
    assert empty.error  # no text at all is reported as an error


class _FakeResponse:
    def __init__(self, status_code=200, lines=None, text=""):
        self.status_code = status_code
        self._lines = lines or []
        self.text = text

    def iter_lines(self):
        yield from self._lines


class _FakeRest:
    token = "session-token"


class _FakeRaw:
    host = "acct.snowflakecomputing.com"
    rest = _FakeRest()


class _FakeConn:
    connection = _FakeRaw()


def test_ask_posts_with_session_token_and_parses(monkeypatch):
    import requests

    captured = {}

    def fake_post(url, headers=None, json=None, stream=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "healthy"}')
        )

    monkeypatch.setattr(requests, "post", fake_post)
    answer = agent.ask(_FakeConn(), "status?", row={"TERMINAL_IDENTIFIER": "T1"})
    assert answer.ok and answer.text == "healthy"
    assert captured["url"].endswith(":run") and "PROD_PRESENTATION" in captured["url"]
    assert 'Snowflake Token="session-token"' in captured["headers"]["Authorization"]
    assert captured["json"]["messages"][-1]["content"][0]["text"].startswith("For terminal T1:")


def test_ask_without_a_live_session_is_an_error():
    class NoSession:
        connection = None

    answer = agent.ask(NoSession(), "hi")
    assert not answer.ok and "Connect to Snowflake" in answer.error


def test_ask_maps_403_and_404_to_friendly_errors(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(status_code=404))
    assert "not found" in agent.ask(_FakeConn(), "hi").error.lower()
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(status_code=403))
    assert "not allowed" in agent.ask(_FakeConn(), "hi").error.lower()


def test_ask_rejects_empty_question():
    assert agent.ask(_FakeConn(), "   ").error
