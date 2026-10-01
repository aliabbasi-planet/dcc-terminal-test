from __future__ import annotations

import pytest

import dcc_console.database as database_module
from dcc_console.database import ConnectionError_, DatabaseConnection


class FakeCursor:
    def __init__(self, descriptions=None, rows=None, nextsets=None, messages=None, rowcount=1):
        self.description = descriptions
        self.rows = list(rows or [])
        self.nextsets = list(nextsets or [])
        self.messages = list(messages or [])
        self.rowcount = rowcount
        self.executed = []
        self.closed = False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def nextset(self):
        if not self.nextsets:
            return False
        state = self.nextsets.pop(0)
        self.description, self.rows, self.messages = state
        return True

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def connected(db_cursor):
    database = DatabaseConnection(" server ", " db ", " user ", " secret")
    database.connection = FakeConnection(db_cursor)
    return database


def test_connection_string_and_missing_connection():
    database = DatabaseConnection("server", "db", "user", "password")
    assert "Driver={Driver 18}" in database._connection_string("Driver 18", True)
    assert "Encrypt=yes" in database._connection_string("Driver 18", True)
    assert "Encrypt=yes" not in database._connection_string("Driver 18", False)
    with pytest.raises(ConnectionError_):
        database.query("SELECT 1")


def test_connect_rejects_missing_credentials():
    database = DatabaseConnection("server", "db", "", "password")
    connected, message = database.connect()
    assert connected is False
    assert "Enter server" in message


def test_connect_reports_missing_driver(monkeypatch):
    monkeypatch.setattr(database_module.pyodbc, "drivers", lambda: [])
    database = DatabaseConnection("server", "db", "user", "password")
    connected, message = database.connect()
    assert connected is False
    assert "Installed ODBC drivers: none" in message


def test_connect_tries_encryption_then_plain_connection(monkeypatch):
    attempts = []

    class Connected:
        autocommit = True

    def connect(connection_string, timeout):
        attempts.append(connection_string)
        if "Encrypt=yes" in connection_string:
            raise RuntimeError("TLS unavailable")
        return Connected()

    monkeypatch.setattr(
        database_module.pyodbc,
        "drivers",
        lambda: ["ODBC Driver 18 for SQL Server"],
    )
    monkeypatch.setattr(database_module.pyodbc, "connect", connect)
    database = DatabaseConnection("server", "db", "user", "password")
    connected, message = database.connect()
    assert connected is True
    assert database.driver == "ODBC Driver 18 for SQL Server"
    assert database.connection.autocommit is False
    assert len(attempts) == 2
    assert "Encrypt=yes" in attempts[0]
    assert "Encrypt=yes" not in attempts[1]
    assert "Connected with" in message


def test_connect_reports_last_driver_error(monkeypatch):
    monkeypatch.setattr(
        database_module.pyodbc,
        "drivers",
        lambda: ["ODBC Driver 18 for SQL Server"],
    )
    monkeypatch.setattr(
        database_module.pyodbc,
        "connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("connection failed")),
    )
    database = DatabaseConnection("server", "db", "user", "password")
    connected, message = database.connect()
    assert connected is False
    assert "connection failed" in message


def test_query_and_scalar_close_cursor():
    query_cursor = FakeCursor(descriptions=[("id",), ("name",)], rows=[(1, "one")])
    database = connected(query_cursor)
    frame = database.query("SELECT", (1,))
    assert frame.to_dict("records") == [{"id": 1, "name": "one"}]
    assert query_cursor.closed is True

    scalar_cursor = FakeCursor(descriptions=[("value",)], rows=[("answer",)])
    database = connected(scalar_cursor)
    assert database.scalar("SELECT", ()) == "answer"
    assert scalar_cursor.closed is True


def test_execute_write_commits_and_rolls_back_on_error():
    cursor = FakeCursor(rowcount=3)
    database = connected(cursor)
    assert database.execute_write("UPDATE", (1,)) == 3
    assert database.connection.commits == 1

    class FailingCursor(FakeCursor):
        def execute(self, sql, params=()):
            raise RuntimeError("write failed")

    failing = connected(FailingCursor())
    with pytest.raises(RuntimeError, match="write failed"):
        failing.execute_write("UPDATE")
    assert failing.connection.rollbacks == 1


def test_execute_batch_skips_blank_statements_and_commits():
    cursor = FakeCursor(rowcount=2)
    database = connected(cursor)
    assert database.execute_batch(["", "UPDATE a", "  ", "UPDATE b"]) == 4
    assert [sql for sql, _ in cursor.executed] == ["UPDATE a", "UPDATE b"]
    assert database.connection.commits == 1


def test_execute_batch_rolls_back_on_error():
    class FailingCursor(FakeCursor):
        def execute(self, sql, params=()):
            if sql == "bad":
                raise RuntimeError("batch failed")
            super().execute(sql, params)

    database = connected(FailingCursor(rowcount=1))
    with pytest.raises(RuntimeError, match="batch failed"):
        database.execute_batch(["good", "bad"])
    assert database.connection.rollbacks == 1


def test_call_procedure_collects_grids_messages_and_return_code():
    cursor = FakeCursor(
        descriptions=[("preview",)],
        rows=[("yes",)],
        messages=[("S", "first")],
        nextsets=[
            ([("dcc_return_code",)], [(0,)], [("S", "first"), ("S", "second")]),
            (None, [], []),
        ],
    )
    database = connected(cursor)
    output = database.call_procedure("EXEC", (1,), rollback=False)
    assert len(output.grids) == 1
    assert output.grids[0].to_dict("records") == [{"preview": "yes"}]
    assert output.return_code == 0
    assert output.messages == ["first", "second"]
    assert database.connection.commits == 1


def test_call_procedure_rolls_back_and_ignores_non_integer_return_code():
    cursor = FakeCursor(
        descriptions=[("dcc_return_code",)],
        rows=[("not-an-int",)],
        nextsets=[(None, [], [])],
    )
    database = connected(cursor)
    output = database.call_procedure("EXEC", (), rollback=True)
    assert output.grids == []
    assert output.return_code is None
    assert database.connection.rollbacks == 1


def test_safe_rollback_swallows_driver_errors_and_close_clears_connection():
    database = connected(FakeCursor())
    database.connection = None
    database.safe_rollback()

    database = connected(FakeCursor())
    database.close()
    assert database.connection is None
