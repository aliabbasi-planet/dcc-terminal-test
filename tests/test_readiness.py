from __future__ import annotations

import pandas as pd

from dcc_console.readiness import (
    READINESS_CHECKS,
    all_passed,
    capture_procedure_version,
    grant_script,
    run_readiness,
)


class ReadinessConnection:
    database = "3CDB"

    def __init__(self, scalar_values=None, query_frame=None, query_error=None):
        self.scalar_values = list(scalar_values or [])
        self.query_frame = query_frame
        self.query_error = query_error
        self.scalar_calls = []

    def scalar(self, sql, params=()):
        self.scalar_calls.append((sql, params))
        return self.scalar_values.pop(0)

    def query(self, sql, params=()):
        if self.query_error:
            raise self.query_error
        return self.query_frame


def test_run_readiness_builds_login_specific_remedies():
    connection = ReadinessConnection([1, 1, 1, 1, 1])
    outcomes = run_readiness(connection, "svc-login")
    assert len(outcomes) == len(READINESS_CHECKS)
    assert all_passed(outcomes) is True
    assert all(outcome.passed for outcome in outcomes)
    assert connection.scalar_calls[0][1] == ("3CDB",)


def test_run_readiness_reports_probe_errors_and_failed_checks():
    connection = ReadinessConnection([1, 0, 0, 1, 0])
    outcomes = run_readiness(connection, "svc-login")
    assert all_passed(outcomes) is False
    assert outcomes[1].remedy == "Deploy the procedure to schema cccai."
    assert "GRANT EXECUTE" in outcomes[2].remedy
    assert "svc-login" in outcomes[2].remedy
    assert "GRANT EXECUTE" in outcomes[4].remedy

    failing = ReadinessConnection()
    failing.scalar = lambda sql, params=(): (_ for _ in ()).throw(RuntimeError("probe failed"))
    outcomes = run_readiness(failing, "login")
    assert all(not outcome.passed for outcome in outcomes)
    assert all("probe failed" in outcome.remedy for outcome in outcomes)


def test_grant_script_contains_only_failed_permission_remedies():
    outcomes = run_readiness(ReadinessConnection([1, 1, 0, 1, 0]), "svc")
    script = grant_script(outcomes, "3CDB", "UAT")
    assert "USE [3CDB];" in script
    assert script.count("GRANT EXECUTE") == 2

    clean = grant_script(run_readiness(ReadinessConnection([1] * 5), "svc"), "3CDB", "UAT")
    assert "No permission grants outstanding" in clean


def test_capture_procedure_version_hashes_definition():
    frame = pd.DataFrame([{
        "object_id": 42,
        "create_date": "2026-01-01",
        "modify_date": "2026-09-01",
        "definition": "CREATE PROCEDURE p AS SELECT 1",
        "definition_bytes": 60,
        "sha256_server_utf16": "server-hash",
        "can_view_definition": 1,
        "is_encrypted": 0,
    }])
    result = capture_procedure_version(ReadinessConnection(query_frame=frame))
    assert result["object_id"] == 42
    assert len(result["definition_sha256"]) == 64
    assert result["sha256_server_utf16"] == "server-hash"
    assert result["unavailable_reason"] is None


def test_capture_procedure_version_handles_missing_and_encrypted_definition():
    missing = capture_procedure_version(ReadinessConnection(query_frame=pd.DataFrame()))
    assert missing is None

    encrypted = pd.DataFrame([{
        "object_id": 42,
        "create_date": "created",
        "modify_date": "modified",
        "definition": None,
        "definition_bytes": None,
        "sha256_server_utf16": None,
        "can_view_definition": 1,
        "is_encrypted": 1,
    }])
    result = capture_procedure_version(ReadinessConnection(query_frame=encrypted))
    assert result["definition_sha256"] == "unavailable"
    assert "ENCRYPTION" in result["unavailable_reason"]


def test_capture_procedure_version_falls_back_to_minimal_query():
    class MinimalConnection(ReadinessConnection):
        def __init__(self):
            super().__init__(query_frame=pd.DataFrame([{
                "object_id": 7,
                "create_date": "created",
                "modify_date": "modified",
                "definition": "SELECT 1",
            }]))
            self.calls = 0

        def query(self, sql, params=()):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("unsupported function")
            return self.query_frame

    connection = MinimalConnection()
    result = capture_procedure_version(connection)
    assert result["object_id"] == 7
    assert result["sha256_server_utf16"] == "unavailable"
    assert connection.calls == 2
