"""Pre-flight readiness probes.

Mirrors Section 3 of `DccEnablementConfiguration_UatSimulation.sql`: confirm the
procedure and its trace dependency exist and that the connected login may
execute both, before any test is allowed to run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import PROCEDURE
from .database import DatabaseConnection


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    sql: str
    remedy_template: str


@dataclass(frozen=True)
class ReadinessOutcome:
    name: str
    passed: bool
    remedy: str


READINESS_CHECKS: tuple[ReadinessCheck, ...] = (
    ReadinessCheck(
        name="Connected to the expected database",
        sql="SELECT CASE WHEN DB_NAME() = ? THEN 1 ELSE 0 END",
        remedy_template="Point the connection at the intended database.",
    ),
    ReadinessCheck(
        name="Procedure cccai.spApplyDCCEnablementConfiguration exists",
        sql=(
            "SELECT CASE WHEN OBJECT_ID(N'[cccai].[spApplyDCCEnablementConfiguration]', N'P')"
            " IS NULL THEN 0 ELSE 1 END"
        ),
        remedy_template="Deploy the procedure to schema cccai.",
    ),
    ReadinessCheck(
        name="EXECUTE on cccai.spApplyDCCEnablementConfiguration",
        sql=(
            "SELECT ISNULL(HAS_PERMS_BY_NAME(N'cccai.spApplyDCCEnablementConfiguration',"
            " N'OBJECT', N'EXECUTE'), 0)"
        ),
        remedy_template=(
            "GRANT EXECUTE ON [cccai].[spApplyDCCEnablementConfiguration] TO [{login}];"
        ),
    ),
    ReadinessCheck(
        name="Function db.fnDisplayTrace exists",
        sql="SELECT CASE WHEN OBJECT_ID(N'[db].[fnDisplayTrace]') IS NULL THEN 0 ELSE 1 END",
        remedy_template="Deploy [db].[fnDisplayTrace]; the procedure depends on it.",
    ),
    ReadinessCheck(
        name="EXECUTE on db.fnDisplayTrace",
        sql="SELECT ISNULL(HAS_PERMS_BY_NAME(N'db.fnDisplayTrace', N'OBJECT', N'EXECUTE'), 0)",
        remedy_template="GRANT EXECUTE ON [db].[fnDisplayTrace] TO [{login}];",
    ),
)


def run_readiness(connection: DatabaseConnection, login: str) -> list[ReadinessOutcome]:
    outcomes: list[ReadinessOutcome] = []
    for check in READINESS_CHECKS:
        remedy = check.remedy_template.format(login=login or "<db-login>")
        params = (connection.database,) if "?" in check.sql else ()
        try:
            passed = bool(connection.scalar(check.sql, params))
        except Exception as exc:
            outcomes.append(ReadinessOutcome(check.name, False, f"{remedy}  (probe error: {exc})"))
        else:
            outcomes.append(ReadinessOutcome(check.name, passed, remedy))
    return outcomes


def all_passed(outcomes: list[ReadinessOutcome]) -> bool:
    return bool(outcomes) and all(outcome.passed for outcome in outcomes)


def grant_script(outcomes: list[ReadinessOutcome], database: str, environment: str) -> str:
    """Copy-pasteable script fixing every failed permission check."""
    statements = [
        outcome.remedy
        for outcome in outcomes
        if not outcome.passed and outcome.remedy.endswith(";")
    ]
    header = [f"-- Run as a DBA / sysadmin on {database} ({environment})", f"USE [{database}];"]
    return "\n".join(header + (statements or ["-- No permission grants outstanding."]))


def capture_procedure_version(connection: DatabaseConnection) -> dict | None:
    """Pin the exact procedure build under test (CAB issue #1).

    Returns object id, create/modify dates and a SHA-256 of the procedure
    definition, or ``None`` if the procedure or its definition cannot be read.
    """
    sql = (
        "SELECT o.object_id, o.create_date, o.modify_date, m.definition "
        "FROM sys.objects AS o "
        "JOIN sys.sql_modules AS m ON m.object_id = o.object_id "
        "WHERE o.object_id = OBJECT_ID(?, N'P')"
    )
    try:
        frame = connection.query(sql, (PROCEDURE,))
    except Exception:
        return None
    if frame.empty:
        return None

    row = frame.iloc[0]
    definition = row.get("definition")
    definition_sha256 = (
        hashlib.sha256(str(definition).encode("utf-8", "replace")).hexdigest()
        if definition is not None
        else "unavailable"
    )
    return {
        "object": PROCEDURE,
        "object_id": int(row["object_id"]) if row.get("object_id") is not None else None,
        "create_date": str(row.get("create_date")),
        "modify_date": str(row.get("modify_date")),
        "definition_sha256": definition_sha256,
    }
