"""Crash recovery: restore a PENDING restore-journal entry safely.

Kept separate from the Streamlit UI so every guard is unit-testable. An entry is
only restored when the live connection is the exact target it was recorded
against (environment, server **and** database), it is still PENDING, and there is
something trustworthy to restore:

Only structured ``SP_JSON`` restore points may be executed. Legacy SQL entries
remain pending for explicit operator review, never parsed or executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .journal import RESTORE_SP_JSON, RestoreJournal
from .rollback import restore_via_payload


@dataclass(frozen=True)
class RecoveryOutcome:
    ok: bool
    message: str


def _same(left: object, right: object) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def recover_entry(
    entry: dict,
    connection,
    connected_env: str,
    journal: RestoreJournal,
) -> RecoveryOutcome:
    """Restore one journal entry, or explain why it was refused."""
    label = f"{entry.get('target')} ({entry.get('test_key')})"
    if connection is None or getattr(connection, "connection", None) is None:
        return RecoveryOutcome(
            False,
            f"Connect to {entry['environment']} ({entry['server']}/{entry['database_name']}) "
            "first, then retry the recovery.",
        )
    if not _same(connected_env, entry["environment"]):
        return RecoveryOutcome(
            False,
            f"You are connected to {connected_env or 'nothing'} but {label} was changed in "
            f"{entry['environment']}. Switch environments first.",
        )
    if not (
        _same(getattr(connection, "server", ""), entry["server"])
        and _same(getattr(connection, "database", ""), entry["database_name"])
    ):
        return RecoveryOutcome(
            False,
            f"{label} was changed on {entry['server']}/{entry['database_name']}, but you are "
            f"connected to {connection.server}/{connection.database}. Restoring here would "
            "write the old value to the wrong database — connect to the recorded one.",
        )

    current = journal.get_entry(entry["id"])
    if current is None or current["status"] != "PENDING":
        status = current["status"] if current else "missing"
        return RecoveryOutcome(False, f"{label} is no longer pending ({status}); nothing to do.")

    if current.get("restore_kind") != RESTORE_SP_JSON:
        return RecoveryOutcome(
            False,
            f"Legacy restore point for {label}: no structured JSON original values were "
            "captured. SQL/UPDATE execution is disabled; ask the DBA to review this entry "
            "(including any NULL values).",
        )
    try:
        plan = json.loads(current["restore_sql"])
        if not isinstance(plan, dict) or any(
            plan.get(k) != current[v]
            for k, v in (("target", "target"), ("test_key", "test_key"), ("bit", "bit"))
        ):
            return RecoveryOutcome(
                False, f"Restore payload does not match journal entry for {label}."
            )
    except (ValueError, TypeError) as exc:
        return RecoveryOutcome(False, f"Invalid recovery JSON for {label}: {exc}")
    outcome = restore_via_payload(connection, plan, trigger="recovery")
    if not outcome.ok:
        return RecoveryOutcome(False, f"Recovery failed for {label}: {outcome.error}")
    journal.mark_resolved(current["id"])
    return RecoveryOutcome(
        True, f"Restored and verified {label} through the SP ({outcome.rows} record(s))."
    )
