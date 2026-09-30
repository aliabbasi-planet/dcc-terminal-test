"""Client for the account's DCC maintenance Cortex Agent (read-only Q&A).

Lets an operator ask the ``DCC_TERMINAL_MAINTENANCE_AGENT`` about a terminal *before*
changing it — "is this terminal healthy?", "which checks fail?", "what's the fix?" —
without leaving the remediation tab. The agent is Cortex-Analyst-backed over the same
maintenance semantic view the worklist is built from, so its answers line up with the
snapshot.

Design:

* The pure helpers (:func:`agent_path`, :func:`build_payload`, :func:`scoped_question`,
  :func:`parse_sse`) import nothing heavy and are unit-tested with fakes.
* :func:`ask` is the only part that touches the network. It reuses the operator's own
  SSO session token from the live Snowflake connector — no new secret, no new login —
  and calls the Cortex Agent ``:run`` REST endpoint, streaming the SSE reply.
* Every failure is returned as an :class:`AgentAnswer` with ``error`` set; the caller
  renders it as a caption and never crashes the tab.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import DCC_AGENT_FQN, validate_identifier

# The agent's own budget is ~60s; give the HTTP call headroom over that.
_TIMEOUT_S = 90


@dataclass
class AgentAnswer:
    """The assembled answer from one agent turn (or an error)."""

    text: str = ""
    sql: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


def agent_path(agent_fqn: str = DCC_AGENT_FQN) -> str:
    """REST path for the ``:run`` endpoint of a ``DATABASE.SCHEMA.NAME`` agent.

    Each identifier is validated as a bare identifier (the same guard the rest of the
    package uses), so the FQN cannot inject path segments.
    """
    parts = agent_fqn.split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected DATABASE.SCHEMA.NAME agent, got {agent_fqn!r}")
    database, schema, name = (validate_identifier(p, "agent identifier") for p in parts)
    return f"/api/v2/databases/{database}/schemas/{schema}/agents/{name}:run"


def scoped_question(row: dict | None, question: str) -> str:
    """Prepend the selected terminal's identifiers so the agent scopes its answer.

    Uses only the identifiers (not the whole row) so the prompt stays small and the
    agent looks the terminal up itself in the semantic view.
    """
    text = (question or "").strip()
    if not row:
        return text
    bits = []
    for label, column in (("terminal", "TERMINAL_IDENTIFIER"), ("instance", "INSTANCE_IDENTIFIER")):
        value = row.get(column)
        if value:
            bits.append(f"{label} {value}")
    if not bits:
        return text
    return f"For {' / '.join(bits)}: {text}"


def build_payload(question: str, history: list[dict] | None = None) -> dict:
    """The ``:run`` request body: prior turns (if any) then the new user question.

    ``history`` items are ``{"role": "user"|"assistant", "text": "..."}``.
    """
    messages: list[dict] = []
    for turn in history or []:
        role = turn.get("role")
        text = (turn.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": [{"type": "text", "text": text}]})
    messages.append({"role": "user", "content": [{"type": "text", "text": question}]})
    return {"messages": messages}


def _collect_text(payload: dict, answer: AgentAnswer, *, is_delta: bool) -> None:
    """Pull answer text and any generated SQL out of one SSE ``data`` object."""
    text = payload.get("text")
    if isinstance(text, str) and text:
        # Deltas concatenate; a non-delta full-text event replaces (avoids duplication).
        answer.text = (answer.text + text) if is_delta else text
    # The Analyst tool surfaces its SQL under a few shapes across versions; capture any.
    for key in ("sql", "statement"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and value not in answer.sql:
            answer.sql.append(value.strip())
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                _collect_text(item, answer, is_delta=is_delta)


def parse_sse(lines) -> AgentAnswer:
    """Assemble an :class:`AgentAnswer` from Server-Sent-Event lines.

    Robust to versions that stream ``response.text.delta`` events and to those that send
    a single ``response.text``; if the stream carries an ``error`` event its message is
    surfaced. Non-JSON ``data`` payloads (e.g. ``[DONE]``) are ignored.
    """
    answer = AgentAnswer()
    event = ""
    saw_delta = False
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\r\n")
        if not line:
            event = ""
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        err_obj = payload.get("error")
        if event == "error" or isinstance(err_obj, dict):
            if isinstance(err_obj, dict):
                answer.error = str(err_obj.get("message") or err_obj)
            else:
                answer.error = str(err_obj or payload)
            continue
        is_delta = event.endswith(".delta")
        if is_delta:
            saw_delta = True
        # Once we've seen deltas, ignore full-text snapshots so we don't double the text.
        if not (saw_delta and not is_delta):
            _collect_text(payload, answer, is_delta=is_delta)
    if not answer.text.strip() and answer.error is None:
        answer.error = "The agent returned no text."
    return answer


def ask(
    conn,
    question: str,
    *,
    row: dict | None = None,
    history: list[dict] | None = None,
    agent_fqn: str = DCC_AGENT_FQN,
) -> AgentAnswer:
    """Ask the agent one question over the operator's live Snowflake session.

    ``conn`` is a :class:`~dcc_console.remediation.sf_connection.SnowflakeConnection`.
    Never raises: any transport / auth / permission problem comes back as ``error``.
    """
    text = (question or "").strip()
    if not text:
        return AgentAnswer(error="Type a question for the agent.")
    raw = getattr(conn, "connection", None)
    host = getattr(raw, "host", None)
    rest = getattr(raw, "rest", None)
    token = getattr(rest, "token", None) if rest is not None else None
    if not raw or not host or not token:
        return AgentAnswer(error="Connect to Snowflake before asking the agent.")
    try:
        import requests
    except Exception as exc:  # pragma: no cover - requests is a declared dependency
        return AgentAnswer(error=f"The `requests` package is unavailable: {exc}")

    url = f"https://{host}{agent_path(agent_fqn)}"
    headers = {
        "Authorization": f'Snowflake Token="{token}"',
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = build_payload(scoped_question(row, text), history)
    try:
        response = requests.post(
            url, headers=headers, json=payload, stream=True, timeout=_TIMEOUT_S
        )
    except Exception as exc:
        return AgentAnswer(error=f"Could not reach the agent: {exc}")
    if response.status_code == 404:
        return AgentAnswer(
            error=(
                f"Agent {agent_fqn} was not found, or your role cannot see it. Ask the owner "
                "for USAGE on the agent and its semantic view."
            )
        )
    if response.status_code in (401, 403):
        return AgentAnswer(
            error=(
                "Your Snowflake role is not allowed to run this agent. Ask the owner for "
                "USAGE on the agent and its Cortex Analyst semantic view."
            )
        )
    if response.status_code >= 400:
        detail = (response.text or "").strip()
        return AgentAnswer(error=f"The agent call failed ({response.status_code}). {detail[:500]}")
    try:
        return parse_sse(response.iter_lines())
    except Exception as exc:
        return AgentAnswer(error=f"Could not read the agent's reply: {exc}")
