"""Crash recovery: restore a PENDING restore-journal entry safely.

Kept separate from the Streamlit UI so every guard is unit-testable. An entry is
only restored when the live connection is the exact target it was recorded
against (environment, server **and** database), it is still PENDING, and there is
something trustworthy to restore:

* ``COLUMN`` entries re-run the generic parameterised ``UPDATE`` with the captured
  pre-change value, then read it back to confirm.
* ``SP_SCRIPT`` entries run the procedure's own compensating statement(s) verbatim,
  with no bound parameters (the script already carries its values).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .catalog import TEST_CATALOG
from .journal import RESTORE_SP_SCRIPT, RestoreJournal
from .rollback import read_state, restore_via_scripts

logger = logging.getLogger(__name__)


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

    if current.get("restore_kind") == RESTORE_SP_SCRIPT:
        scripts = journal.procedure_scripts(current)
        outcome = restore_via_scripts(connection, scripts, trigger="recovery")
        if not outcome.ok:
            return RecoveryOutcome(False, f"Recovery failed for {label}: {outcome.error}")
        journal.mark_resolved(current["id"])
        return RecoveryOutcome(
            True, f"Restored {label} with the procedure's own script ({outcome.rows} row(s))."
        )

    if current.get("original_value") is None:
        # Writing NULL back would destroy the row's configuration rather than restore it.
        return RecoveryOutcome(
            False,
            f"No pre-change value was captured for {label}, so an automatic restore would "
            "write NULL. Restore it manually from a backup.",
        )
    try:
        connection.execute_write(
            current["restore_sql"], (current["original_value"], current["target"])
        )
    except Exception as exc:
        logger.error("Recovery failed for %s: %s", label, exc)
        return RecoveryOutcome(False, f"Recovery failed for {label}: {exc}")

    definition = TEST_CATALOG.get(current["test_key"])
    if definition is not None:
        restored = read_state(connection, definition, current["target"])
        if str(restored) != str(current["original_value"]):
            return RecoveryOutcome(
                False,
                f"Restore for {label} committed but the stored value still differs from the "
                "captured one — investigate before retrying.",
            )
    journal.mark_resolved(current["id"])
    return RecoveryOutcome(True, f"Restored {label}.")
