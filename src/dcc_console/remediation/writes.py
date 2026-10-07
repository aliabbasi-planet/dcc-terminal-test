"""Shared Streamlit write helpers for the remediation fixer and batch runner.

Fix-log and durable-registry writes, the notice queue that survives ``st.rerun()``,
and the pending-write retry banner. Extracted from the one-by-one fix panel so the
batch runner records every outcome exactly the same way — same audit guarantees,
same crash-recovery handoff, same "never lose a row" retry queue.

Nothing here decides *what* to write (that stays in :mod:`.fixer`); it only takes the
records the caller built and persists them, queueing them for retry on failure.
"""

from __future__ import annotations

import streamlit as st

from ..execution import TestResult
from ..journal import get_journal
from . import RemediationObjects, fixlog, registry
from .sf_connection import SnowflakeConnection

# Session keys shared by every panel that writes (so one notice/retry queue serves all).
_PENDING_KEY = "rem_pending_logs"
_NOTICE_KEY = "rem_fix_notice"


def _state(key: str, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def notice(level: str, message: str) -> None:
    """Queue a message that survives ``st.rerun()`` (shown on the next render)."""
    _state(_NOTICE_KEY, []).append((level, message))


def show_notices() -> None:
    for level, message in st.session_state.pop(_NOTICE_KEY, []):
        getattr(st, level)(message)


def write_log(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    records: list[dict],
    *,
    summary: str,
    keep: TestResult | None = None,
) -> bool:
    """Write fix-log rows; on failure queue them for retry (never lose an audit row).

    ``keep`` is a verified live fix: once its rows are written it leaves crash recovery.
    """
    if not records:
        return True
    try:
        sql, params = fixlog.build_insert_many(records, objs)
    except ValueError as exc:  # a malformed record is a bug — surface it, don't queue it
        notice("error", f"Fix-log rows for {summary} were rejected: {exc}")
        return False
    try:
        conn.execute(sql, params)
    except Exception as exc:
        _state(_PENDING_KEY, []).append(
            {
                "sql": sql,
                "params": params,
                "summary": summary,
                "journal_id": keep.journal_id if keep is not None else None,
                "error": str(exc),
            }
        )
        notice(
            "error",
            f"Could not write the fix log for {summary}: {exc}. The rows are queued — use "
            "**Retry fix-log writes** at the top of the tab.",
        )
        return False
    if keep is not None and keep.journal_id is not None:
        get_journal().mark_kept(keep.journal_id)
    return True


def register_fix(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    records: list[dict],
    *,
    summary: str,
) -> None:
    """Upsert verified fixes into the durable registry (best-effort; never crashes).

    A failure here does not lose the audit trail (that is in APP_FIX_LOG) — it only
    means the worklist may re-show this terminal until the next daily load confirms it,
    so we surface a warning rather than blocking the operator.
    """
    if not records:
        return
    try:
        sql, params = registry.build_upsert(records, objs)
        conn.execute(sql, params)
    except Exception as exc:
        notice("warning", f"Could not register {summary} in the fix registry: {exc}")


def deregister_fix(
    conn: SnowflakeConnection,
    objs: RemediationObjects,
    *,
    environment: str,
    terminals: list[str],
    check_column: str,
    summary: str,
) -> None:
    """Delete registry rows for a confirmed rollback (best-effort; never crashes)."""
    try:
        sql, params = registry.build_delete_many(environment, terminals, check_column, objs)
        conn.execute(sql, params)
    except Exception as exc:
        notice("warning", f"Could not remove {summary} from the fix registry: {exc}")


def render_pending_logs(conn: SnowflakeConnection) -> None:
    """Banner + retry for fix-log rows that could not be written."""
    pending = st.session_state.get(_PENDING_KEY) or []
    if not pending:
        return
    st.error(
        f"**{len(pending)} fix-log write(s) are queued.** Until they are written, those "
        "attempts are missing from the shared audit trail, and a live fix among them stays "
        "listed in crash recovery."
    )
    if st.button("Retry fix-log writes", key="rem_retry_logs", type="primary"):
        remaining = []
        for item in pending:
            try:
                conn.execute(item["sql"], item["params"])
            except Exception as exc:
                remaining.append({**item, "error": str(exc)})
                continue
            if item.get("journal_id") is not None:
                get_journal().mark_kept(item["journal_id"])
        st.session_state[_PENDING_KEY] = remaining
        written = len(pending) - len(remaining)
        notice(
            "success" if not remaining else "warning",
            f"Wrote {written} queued fix-log write(s); {len(remaining)} still queued.",
        )
        st.rerun()
    with st.expander("Queued fix-log writes"):
        st.table([{"what": item["summary"], "last error": item["error"]} for item in pending])
