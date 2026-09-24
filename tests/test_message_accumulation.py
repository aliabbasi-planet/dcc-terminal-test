"""Tests for server-message accumulation across multiple result sets."""

from __future__ import annotations

import pandas as pd

from dcc_console.database import DatabaseConnection


class FakeCursor:
    """Cursor that emits messages per result set and clears them on nextset().

    This mimics the ODBC driver behaviour that made the original single read
    of ``cursor.messages`` lose output emitted between result sets.
    """

    def __init__(self, sets: list[dict]) -> None:
        self._sets = list(sets)
        self._index = 0
        self.messages: list[tuple] = []
        self.closed = False
        self._load_current()

    def _load_current(self) -> None:
        if self._index < len(self._sets):
            current = self._sets[self._index]
            # Driver clears previous messages when a new set becomes current.
            self.messages = [("01000", m) for m in current.get("messages", [])]
            self._rows = current.get("rows")
        else:
            self.messages = []
            self._rows = None

    @property
    def description(self):
        if self._rows is None:
            return None
        return [(col,) for col in self._rows[0]] if self._rows else None

    def execute(self, sql, params=()):
        return self

    def fetchall(self):
        if not self._rows:
            return []
        return [tuple(row.values()) for row in self._rows]

    def nextset(self):
        self._index += 1
        if self._index >= len(self._sets):
            self.messages = []
            return False
        self._load_current()
        return True

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _db(cursor: FakeCursor) -> DatabaseConnection:
    db = DatabaseConnection("SRV", "DB", "user", "pw")
    db.connection = FakeConnection(cursor)
    return db


def test_messages_from_every_result_set_are_retained():
    cursor = FakeCursor(
        [
            {"messages": ["first set message"], "rows": [{"a": 1}]},
            {"messages": ["second set message"], "rows": [{"b": 2}]},
            {"messages": ["third set message"], "rows": None},
        ]
    )
    out = _db(cursor).call_procedure("EXEC x", (), rollback=True)
    assert "first set message" in out.messages
    assert "second set message" in out.messages
    assert "third set message" in out.messages


def test_duplicate_messages_are_not_repeated():
    cursor = FakeCursor(
        [
            {"messages": ["same message"], "rows": None},
            {"messages": ["same message"], "rows": None},
        ]
    )
    out = _db(cursor).call_procedure("EXEC x", (), rollback=True)
    assert out.messages.count("same message") == 1


def test_grids_are_collected_for_each_described_set():
    cursor = FakeCursor(
        [
            {"messages": [], "rows": [{"a": 1}]},
            {"messages": [], "rows": [{"b": 2}]},
        ]
    )
    out = _db(cursor).call_procedure("EXEC x", (), rollback=True)
    assert len(out.grids) == 2
    assert all(isinstance(g, pd.DataFrame) for g in out.grids)


def test_simulation_rolls_back_and_live_commits():
    cursor = FakeCursor([{"messages": [], "rows": None}])
    db = _db(cursor)
    db.call_procedure("EXEC x", (), rollback=True)
    assert db.connection.rolled_back
    assert not db.connection.committed

    cursor2 = FakeCursor([{"messages": [], "rows": None}])
    db2 = _db(cursor2)
    db2.call_procedure("EXEC x", (), rollback=False)
    assert db2.connection.committed
    assert not db2.connection.rolled_back


def test_cursor_is_closed_even_on_success():
    cursor = FakeCursor([{"messages": [], "rows": None}])
    _db(cursor).call_procedure("EXEC x", (), rollback=True)
    assert cursor.closed
