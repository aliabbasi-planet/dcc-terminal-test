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


def test_agent_path_uses_environment_components_and_requires_all_three(monkeypatch):
    monkeypatch.setenv("CORTEX_AGENT_DATABASE", "CUSTOM_DB")
    monkeypatch.setenv("CORTEX_AGENT_SCHEMA", "CUSTOM_SCHEMA")
    monkeypatch.setenv("CORTEX_AGENT_NAME", "CUSTOM_AGENT")
    assert agent.agent_path() == (
        "/api/v2/databases/CUSTOM_DB/schemas/CUSTOM_SCHEMA/agents/CUSTOM_AGENT:run"
    )

    monkeypatch.delenv("CORTEX_AGENT_SCHEMA")
    with pytest.raises(ValueError, match="Set CORTEX_AGENT_DATABASE"):
        agent.agent_path()


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
    assert "warehouse" not in payload
    with_warehouse = agent.build_payload("hello", warehouse="  DATA_SCIENCE  ")
    assert with_warehouse["warehouse"] == "DATA_SCIENCE"


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

    class settings:
        warehouse = "SESSION_WH"


def test_ask_posts_with_session_token_and_parses(monkeypatch):
    import requests

    captured = {}
    for name in (
        "SNOWFLAKE_ACCOUNT_URL",
        "CORTEX_AGENT_DATABASE",
        "CORTEX_AGENT_SCHEMA",
        "CORTEX_AGENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    def fake_post(url, headers=None, json=None, stream=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "healthy"}')
        )

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("CORTEX_AGENT_WAREHOUSE", "AGENT_WH")
    answer = agent.ask(_FakeConn(), "status?", row={"TERMINAL_IDENTIFIER": "T1"})
    assert answer.ok and answer.text == "healthy"
    assert captured["url"].endswith(":run") and "PROD_PRESENTATION" in captured["url"]
    assert 'Snowflake Token="session-token"' in captured["headers"]["Authorization"]
    assert captured["json"]["messages"][-1]["content"][0]["text"].startswith("For terminal T1:")
    assert captured["headers"]["X-Snowflake-Warehouse"] == "AGENT_WH"
    assert captured["json"]["warehouse"] == "AGENT_WH"


def test_ask_uses_configured_agent_and_matching_account_url(monkeypatch):
    import requests

    captured = {}
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT_URL", "https://acct.snowflakecomputing.com/")
    monkeypatch.setenv("CORTEX_AGENT_DATABASE", "CUSTOM_DB")
    monkeypatch.setenv("CORTEX_AGENT_SCHEMA", "CUSTOM_SCHEMA")
    monkeypatch.setenv("CORTEX_AGENT_NAME", "CUSTOM_AGENT")
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: captured.update(url=url, **kwargs) or _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "ok"}')
        ),
    )

    answer = agent.ask(_FakeConn(), "status?")

    assert answer.ok
    assert captured["url"] == (
        "https://acct.snowflakecomputing.com/api/v2/databases/CUSTOM_DB/"
        "schemas/CUSTOM_SCHEMA/agents/CUSTOM_AGENT:run"
    )


def test_ask_uses_explicit_snowflake_account_url_alias(monkeypatch):
    import requests

    captured = {}
    monkeypatch.setenv(
        "SNOWFLAKE_ACCOUNT_URL", "https://ha17763.eu-west-1.snowflakecomputing.com/"
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: captured.update(url=url, **kwargs) or _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "ok"}')
        ),
    )

    answer = agent.ask(_FakeConn(), "status?")

    assert answer.ok
    assert captured["url"].startswith(
        "https://ha17763.eu-west-1.snowflakecomputing.com/api/v2/"
    )


def test_ask_rejects_non_snowflake_account_url(monkeypatch):
    import requests

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT_URL", "https://attacker.example.com")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: pytest.fail(
            "Must not send the session token to a non-Snowflake host"
        ),
    )

    answer = agent.ask(_FakeConn(), "status?")

    assert "Snowflake account URL" in answer.error


def test_cortex_client_uses_external_browser_session_without_pat(monkeypatch):
    import requests

    captured = {}
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: captured.update(kwargs) or _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "ok"}')
        ),
    )

    answer = agent.CortexClient(_FakeConn(), warehouse="CLIENT_WH").ask("status?")

    assert answer.ok
    assert captured["headers"]["Authorization"] == 'Snowflake Token="session-token"'
    assert captured["headers"]["X-Snowflake-Warehouse"] == "CLIENT_WH"
    assert captured["json"]["warehouse"] == "CLIENT_WH"


def test_ask_uses_session_warehouse_when_no_agent_override(monkeypatch):
    import requests

    captured = {}
    monkeypatch.delenv("CORTEX_AGENT_WAREHOUSE", raising=False)
    monkeypatch.delenv("SNOWFLAKE_WAREHOUSE", raising=False)
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: captured.update(kwargs) or _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "ok"}')
        ),
    )

    answer = agent.ask(_FakeConn(), "status?")

    assert answer.ok
    assert captured["headers"]["X-Snowflake-Warehouse"] == "SESSION_WH"
    assert captured["json"]["warehouse"] == "SESSION_WH"


def test_ask_uses_snowflake_warehouse_environment_fallback(monkeypatch):
    import requests

    captured = {}

    class ConnectionWithoutWarehouse:
        connection = _FakeRaw()
        settings = object()

    monkeypatch.delenv("CORTEX_AGENT_WAREHOUSE", raising=False)
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "ENV_WH")
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: captured.update(kwargs) or _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "ok"}')
        ),
    )

    answer = agent.ask(ConnectionWithoutWarehouse(), "status?")

    assert answer.ok
    assert captured["headers"]["X-Snowflake-Warehouse"] == "ENV_WH"
    assert captured["json"]["warehouse"] == "ENV_WH"


def test_snowflake_warehouse_environment_overrides_active_connection(monkeypatch):
    import requests

    captured = {}
    monkeypatch.delenv("CORTEX_AGENT_WAREHOUSE", raising=False)
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "ENV_WH")
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, **kwargs: captured.update(kwargs) or _FakeResponse(
            lines=_sse("event: response.text.delta", 'data: {"text": "ok"}')
        ),
    )

    answer = agent.ask(_FakeConn(), "status?")

    assert answer.ok
    assert captured["headers"]["X-Snowflake-Warehouse"] == "ENV_WH"
    assert captured["json"]["warehouse"] == "ENV_WH"


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
