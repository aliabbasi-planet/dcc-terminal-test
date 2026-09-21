"""Interpret the procedure's internal trace output.

Corrected model (confirmed with the procedure owner):

    ``[db].[fnDisplayTrace]`` is a **scalar string formatter**. The stored
    procedure calls it *inside* ``PRINT`` statements, e.g.::

        PRINT db.fnDisplayTrace(GETDATE(), 'validating instance')

    So the trace "log" is SQL Server's **message / info stream**, NOT a table
    and NOT a queryable result set. pyodbc exposes that stream as
    ``cursor.messages``.

Therefore the trace is captured by draining ``cursor.messages`` during the
procedure call (see :meth:`DatabaseConnection.call_procedure`), and this
module simply *interprets* those captured messages — it does not issue any
separate query.  A prior version tried ``SELECT * FROM fnDisplayTrace(...)``;
that was wrong because the function needs two arguments and returns a
formatted string rather than a trace log.

``discover_trace_signature`` remains only to confirm, for the report, that
the function exists and is scalar — documenting the instrumentation model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import TRACE_FUNCTION
from .database import DatabaseConnection

logger = logging.getLogger(__name__)

MAX_TRACE_ROWS = 200


@dataclass(frozen=True)
class TraceSignature:
    """What we confirmed about the trace function, for the report."""

    exists: bool
    is_scalar: bool | None = None
    parameter_count: int | None = None
    unavailable_reason: str | None = None

    @property
    def confirmed_print_formatter(self) -> bool:
        """True when the function exists and is scalar (the PRINT-formatter model)."""
        return bool(self.exists) and bool(self.is_scalar)

    def as_dict(self) -> dict:
        return {
            "function": TRACE_FUNCTION,
            "exists": self.exists,
            "is_scalar": self.is_scalar,
            "parameter_count": self.parameter_count,
            "confirmed_print_formatter": self.confirmed_print_formatter,
            "capture_mechanism": (
                "cursor.messages (SQL Server PRINT / info stream)"
            ),
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class TraceOutput:
    """Trace lines captured from the PRINT message stream for one call."""

    rows: list[dict] = field(default_factory=list)
    status: str = "NOT_CAPTURED"
    reason: str | None = None

    @property
    def text(self) -> str:
        if not self.rows:
            return ""
        parts: list[str] = []
        for row in self.rows:
            if "message" in row:
                if row["message"] is not None:
                    parts.append(str(row["message"]))
            else:
                parts.extend(str(v) for v in row.values() if v is not None)
        return " ".join(parts)

    def as_dict(self) -> dict:
        return {"rows": self.rows, "status": self.status, "reason": self.reason}


def discover_trace_signature(connection: DatabaseConnection) -> TraceSignature:
    """Confirm the trace function exists and is a scalar formatter.

    This is documentation only — it does not affect how the trace is captured
    (that happens through the PRINT message stream during the call).
    """
    sql = (
        "SELECT "
        "  CASE WHEN OBJECT_ID(?) IS NULL THEN 0 ELSE 1 END AS fn_exists, "
        "  OBJECTPROPERTY(OBJECT_ID(?), 'IsScalarFunction') AS is_scalar, "
        "  (SELECT COUNT(*) FROM sys.parameters "
        "     WHERE object_id = OBJECT_ID(?) AND is_output = 0) AS param_count"
    )
    try:
        frame = connection.query(sql, (TRACE_FUNCTION, TRACE_FUNCTION, TRACE_FUNCTION))
    except Exception as exc:
        return TraceSignature(
            exists=False,
            unavailable_reason=f"Could not inspect {TRACE_FUNCTION}: {exc}",
        )

    if frame.empty or not bool(frame.iloc[0].get("fn_exists")):
        return TraceSignature(
            exists=False,
            unavailable_reason=(
                f"{TRACE_FUNCTION} does not exist in this database. The procedure's "
                "trace output would therefore not be formatted; check the deployment."
            ),
        )

    row = frame.iloc[0]
    is_scalar = bool(row.get("is_scalar"))
    param_count = int(row.get("param_count") or 0)
    reason = None
    if not is_scalar:
        reason = (
            f"{TRACE_FUNCTION} exists but is not reported as a scalar function. The "
            "expected model is a scalar formatter used inside PRINT; confirm with the "
            "procedure owner."
        )
    return TraceSignature(
        exists=True,
        is_scalar=is_scalar,
        parameter_count=param_count,
        unavailable_reason=reason,
    )


def trace_from_messages(messages: list[str]) -> TraceOutput:
    """Build a TraceOutput from the captured PRINT message stream.

    Every ``PRINT db.fnDisplayTrace(...)`` line arrives as one server message,
    so each message is one trace line.
    """
    if not messages:
        return TraceOutput(
            status="EMPTY",
            reason="The procedure emitted no PRINT/trace messages for this call.",
        )
    rows = [{"line": i + 1, "message": m} for i, m in enumerate(messages[:MAX_TRACE_ROWS])]
    reason = None
    if len(messages) > MAX_TRACE_ROWS:
        reason = f"{len(messages)} trace line(s); first {MAX_TRACE_ROWS} retained."
    return TraceOutput(status="CAPTURED", rows=rows, reason=reason)


__all__ = [
    "TraceSignature",
    "TraceOutput",
    "discover_trace_signature",
    "trace_from_messages",
    "MAX_TRACE_ROWS",
]
