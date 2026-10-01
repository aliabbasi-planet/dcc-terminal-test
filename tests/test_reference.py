from __future__ import annotations

import pandas as pd

from dcc_console.reference import load_instances, load_locations, load_terminals


class ReferenceConnection:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def query(self, sql, params=()):
        self.calls.append((sql, params))
        return self.frame.copy()


def test_load_instances_and_locations_forward_queries():
    instances = ReferenceConnection(pd.DataFrame([{"instance_identifier": "I1"}]))
    assert load_instances(instances).to_dict("records") == [{"instance_identifier": "I1"}]
    assert instances.calls[0][1] == (500,)

    locations = ReferenceConnection(pd.DataFrame([{"location_no": "L1"}]))
    assert load_locations(locations).to_dict("records") == [{"location_no": "L1"}]
    assert locations.calls[0][1] == (500,)


def test_load_terminals_applies_filters_and_adds_version_description():
    frame = pd.DataFrame([
        {"terminal_identifier": "T1", "configdownload_version": 1},
        {"terminal_identifier": "T2", "configdownload_version": 2},
    ])
    connection = ReferenceConnection(frame)
    result = load_terminals(connection, only_online=True, only_unlocked=True, limit=25)
    assert list(result["configdownload_version_desc"]) == ["1 (Standard)", "2 (ECB DCC)"]
    assert connection.calls[0][1] == (25, 1, 1)


def test_load_terminals_handles_missing_version_column():
    connection = ReferenceConnection(pd.DataFrame([{"terminal_identifier": "T1"}]))
    result = load_terminals(connection, only_online=False, only_unlocked=False, limit=10)
    assert "configdownload_version_desc" not in result.columns
    assert connection.calls[0][1] == (10, 0, 0)
