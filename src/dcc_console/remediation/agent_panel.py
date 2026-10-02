"""Reusable DCC maintenance Cortex Agent chat panel (scoped or account-wide).

One widget rendered on every remediation page. Given a terminal ``row`` it scopes the
question to that terminal (the Single-fix page); with no row it answers account-wide
(the Overview and Batch pages). Read-only Q&A over the same maintenance semantic view
the worklist is built from, called with the operator's own SSO session (see
:mod:`.agent`).

Input is a text box + button (not ``st.chat_input``) on purpose: ``st.chat_input`` may
not be placed inside ``st.tabs`` / ``st.expander``, and the remediation tab is embedded
in both. History is rendered with ``st.chat_message`` for a modern chat look.
"""

from __future__ import annotations

import streamlit as st

from . import agent
from .sf_connection import SnowflakeConnection

_AGENT_CHAT_KEY = "rem_agent_chat"

# Presets for a terminal-scoped chat (Single fix).
TERMINAL_PRESETS = {
    "Health summary": "Give a health summary and list which DCC checks pass or fail.",
    "Failing checks": "Which DCC checks are failing and why? Distinguish CRITICAL from Supporting.",
    "Remediation steps": "How do I fix the failing checks? Give only the relevant steps.",
}

# Presets for an account-wide chat (Overview / Batch).
ACCOUNT_PRESETS = {
    "Top broken checks": "Which DCC checks fail on the most terminals right now? Give counts.",
    "Broken by region": "How many terminals are broken on DCC enablement, broken down by region?",
    "Fixable vs manual": "Which failing DCC checks can be fixed automatically and which need "
    "manual follow-up?",
}


def _state(key: str, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def _ask(conn: SnowflakeConnection, *, context_key: str, row: dict | None, question: str) -> None:
    """Call the agent and append the exchange to this context's chat history."""
    chat = _state(_AGENT_CHAT_KEY, {})
    history = chat.get(context_key, [])
    prior = [t for t in history if t.get("role") in ("user", "assistant")]
    with st.spinner("Asking the DCC maintenance agent…"):
        answer = agent.ask(conn, question, row=row, history=prior)
    history.append({"role": "user", "text": question})
    reply = answer.text if answer.ok else f"⚠️ {answer.error}"
    history.append({"role": "assistant", "text": reply, "sql": answer.sql})
    chat[context_key] = history


def render_agent_panel(
    conn: SnowflakeConnection,
    *,
    context_key: str,
    title: str,
    caption: str,
    row: dict | None = None,
    presets: dict[str, str] | None = None,
    expanded: bool = False,
) -> None:
    """Preset buttons + a free-text chat with the DCC maintenance agent.

    ``context_key`` keys the per-context history (a terminal id when scoped, or a page
    label like ``"overview"`` when account-wide), so each page keeps its own thread and
    the widget keys never collide.
    """
    presets = presets if presets is not None else (TERMINAL_PRESETS if row else ACCOUNT_PRESETS)
    with st.expander(title, expanded=expanded):
        st.caption(caption)
        cols = st.columns(len(presets))
        for col, (label, prompt) in zip(cols, presets.items(), strict=True):
            if col.button(
                label, key=f"rem_agent_preset_{context_key}_{label}", use_container_width=True
            ):
                _ask(conn, context_key=context_key, row=row, question=prompt)
                st.rerun()
        typed = st.text_input("Ask something else", key=f"rem_agent_q_{context_key}")
        if st.button(
            "Ask", key=f"rem_agent_ask_{context_key}", disabled=not typed.strip()
        ):
            _ask(conn, context_key=context_key, row=row, question=typed.strip())
            st.rerun()
        history = st.session_state.get(_AGENT_CHAT_KEY, {}).get(context_key, [])
        for turn in history:
            with st.chat_message("user" if turn["role"] == "user" else "assistant"):
                st.markdown(turn["text"])
                for statement in turn.get("sql") or []:
                    st.code(statement, language="sql")
        if history and st.button("Clear conversation", key=f"rem_agent_clear_{context_key}"):
            st.session_state[_AGENT_CHAT_KEY].pop(context_key, None)
            st.rerun()


def render_terminal_agent(conn: SnowflakeConnection, row: dict) -> None:
    """Terminal-scoped agent chat for the Single-fix page."""
    tid = str(row.get("TERMINAL_IDENTIFIER"))
    render_agent_panel(
        conn,
        context_key=tid,
        title=f"Ask the DCC maintenance agent about {tid}",
        caption=(
            "Cortex agent `DCC_TERMINAL_MAINTENANCE_AGENT`, scoped to this terminal. Read-only "
            "and answered from the same maintenance data as the worklist — use it to sanity-check "
            "a terminal before you change it."
        ),
        row=row,
        presets=TERMINAL_PRESETS,
    )


def render_account_agent(
    conn: SnowflakeConnection, *, context_key: str, expanded: bool = False
) -> None:
    """Account-wide agent chat for the Overview / Batch pages (no terminal scope)."""
    render_agent_panel(
        conn,
        context_key=context_key,
        title="Ask the DCC maintenance agent",
        caption=(
            "Cortex agent `DCC_TERMINAL_MAINTENANCE_AGENT`, read-only over the same maintenance "
            "data as the worklist. Ask about the fleet — broken counts, breakdowns, what's "
            "fixable — before you act."
        ),
        row=None,
        presets=ACCOUNT_PRESETS,
        expanded=expanded,
    )
