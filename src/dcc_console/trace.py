"""Read the procedure's internal trace output via ``[db].[fnDisplayTrace]``.

The procedure under test depends on a trace function, but its signature is
not documented here.  Rather than guessing a call form, this module
*discovers* the signature from ``sys.parameters`` and
``OBJECTPROPERTY(..., 'IsTableFunction')`` at readiness time, builds the
matching call, and caches the result.

Why this matters for CAB: if the procedure surfaces validation failures
through its trace rather than by raising a SQL exception, then a negative
test that is genuinely rejected would otherwise be scored ``NOT-REJECTED``
purely because the harness was not reading the right place.

Every failure path records a *reason*, so the report can state that the
trace was attempted and why it was unavailable — never silently skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import TRACE_FUNCTION
from .database import DatabaseConnection

logger = logging.getLogger(__name__)

# Parameter names that plausibly accept the current session id.
_SPID_HINTS = ("spid", "session", "sessionid", "session_id", "connection")

# Maximum trace rows retained per call.
MAX_TRACE_ROWS = 200


@dataclass(frozen=True)
class TraceSignature:
    """What we discovered about the trace function."""

    exists: bool
    is_table_valued: bool | None
    parameters: tuple[dict, ...] = ()
    call_sql: str | None = None
    unavailable_reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.exists and self.call_sql is not None

    def as_dict(self) -> dict:
        return {
            "function": TRACE_FUNCTION,
            "exists": self.exists,
            "is_table_valued": self.is_table_valued,
            "parameter_count": len(self.parameters),
            "parameters": [p.get("parameter") for p in self.parameters],
            "call_sql": self.call_sql,
            "usable": self.usable,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class TraceOutput:
    """Trace rows captured for one procedure call."""

    rows: list[dict] = field(default_factory=list)
    status: str = "NOT_ATTEMPTED"
    reason: str | None = None

    @property
    def text(self) -> str:
        """Flatten every captured row into one searchable blob."""
        if not self.rows:
            return ""
        parts: list[str] = []
        for row in self.rows:
            parts.extend(str(value) for value in row.values() if value is not None)
        return " ".join(parts)

    def as_dict(self) -> dict:
        return {"rows": self.rows, "status": self.status, "reason": self.reason}


def discover_trace_signature(connection: DatabaseConnection) -> TraceSignature:
    """Inspect the trace function and build a call form for it."""
    meta_sql = (
        "SELECT "
        "  CASE WHEN OBJECT_ID(?) IS NULL THEN 0 ELSE 1 END AS fn_exists, "
        "  OBJECTPROPERTY(OBJECT_ID(?), 'IsTableFunction')  AS is_tvf, "
        "  OBJECTPROPERTY(OBJECT_ID(?), 'IsScalarFunction') AS is_scalar"
    )
    try:
        meta = connection.query(meta_sql, (TRACE_FUNCTION, TRACE_FUNCTION, TRACE_FUNCTION))
    except Exception as exc:
        return TraceSignature(
            exists=False,
            is_table_valued=None,
            unavailable_reason=f"Could not inspect {TRACE_FUNCTION}: {exc}",
        )

    if meta.empty or not bool(meta.iloc[0].get("fn_exists")):
        return TraceSignature(
            exists=False,
            is_table_valued=None,
            unavailable_reason=(
                f"{TRACE_FUNCTION} does not exist in this database, so procedure "
                "trace output cannot be read."
            ),
        )

    row = meta.iloc[0]
    is_tvf = bool(row.get("is_tvf"))
    is_scalar = bool(row.get("is_scalar"))

    param_sql = (
        "SELECT p.name AS parameter, TYPE_NAME(p.user_type_id) AS type_name, "
        "       p.max_length, p.parameter_id "
        "FROM sys.parameters AS p "
        "WHERE p.object_id = OBJECT_ID(?) AND p.is_output = 0 "
        "ORDER BY p.parameter_id"
    )
    try:
        params_frame = connection.query(param_sql, (TRACE_FUNCTION,))
    except Exception as exc:
        return TraceSignature(
            exists=True,
            is_table_valued=is_tvf,
            unavailable_reason=f"Could not read parameters of {TRACE_FUNCTION}: {exc}",
        )

    parameters = tuple(params_frame.to_dict("records")) if not params_frame.empty else ()
    args = _build_arguments(parameters)

    if args is None:
        return TraceSignature(
            exists=True,
            is_table_valued=is_tvf,
            parameters=parameters,
            unavailable_reason=(
                f"{TRACE_FUNCTION} takes parameters this harness cannot supply "
                f"automatically ({', '.join(str(p.get('parameter')) for p in parameters)}). "
                "Trace output is therefore not read. Provide the intended argument "
                "values to enable it."
            ),
        )

    arg_list = ", ".join(args)
    if is_tvf:
        call_sql = f"SELECT * FROM {TRACE_FUNCTION}({arg_list})"
    elif is_scalar:
        call_sql = f"SELECT {TRACE_FUNCTION}({arg_list}) AS trace_output"
    else:
        return TraceSignature(
            exists=True,
            is_table_valued=None,
            parameters=parameters,
            unavailable_reason=(
                f"{TRACE_FUNCTION} is neither a table-valued nor a scalar function "
                "according to OBJECTPROPERTY, so no call form could be built."
            ),
        )

    return TraceSignature(
        exists=True,
        is_table_valued=is_tvf,
        parameters=parameters,
        call_sql=call_sql,
    )


def _build_arguments(parameters: tuple[dict, ...]) -> list[str] | None:
    """Return SQL argument expressions, or ``None`` if we cannot supply them.

    Only parameters we can fill safely are accepted: a session-id-looking
    parameter receives ``@@SPID``; anything else is refused rather than
    guessed, because passing a wrong value could return another session's
    trace and corrupt the evidence.
    """
    if not parameters:
        return []

    args: list[str] = []
    for param in parameters:
        raw_name = str(param.get("parameter") or "").lstrip("@").lower()
        if any(hint in raw_name for hint in _SPID_HINTS):
            args.append("@@SPID")
        else:
            return None
    return args


def read_trace(
    connection: DatabaseConnection,
    signature: TraceSignature | None,
) -> TraceOutput:
    """Execute the discovered trace call and return its rows."""
    if signature is None:
        return TraceOutput(
            status="NOT_ATTEMPTED",
            reason="Trace signature was never discovered; re-run pre-flight readiness.",
        )
    if not signature.usable:
        return TraceOutput(status="UNAVAILABLE", reason=signature.unavailable_reason)

    try:
        frame = connection.query(signature.call_sql)
    except Exception as exc:
        return TraceOutput(
            status="ERRORED",
            reason=f"Trace read failed: {exc}",
        )

    if frame.empty:
        return TraceOutput(status="EMPTY", reason="The trace function returned no rows.")

    rows = frame.head(MAX_TRACE_ROWS).to_dict("records")
    truncated = len(frame) > MAX_TRACE_ROWS
    return TraceOutput(
        status="CAPTURED",
        rows=rows,
        reason=(
            f"{len(frame)} row(s) returned; first {MAX_TRACE_ROWS} retained."
            if truncated
            else None
        ),
    )


__all__ = [
    "TraceSignature",
    "TraceOutput",
    "discover_trace_signature",
    "read_trace",
    "MAX_TRACE_ROWS",
]
