from __future__ import annotations

import pandas as pd

from dcc_console.broken import (
    _extract_xml_flag,
    _is_flag_broken,
    broken_summary,
    compute_broken_flags,
    find_broken_instances,
    find_broken_locations,
    find_broken_terminals,
)


class BrokenConnection:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []

    def query(self, sql, params=()):
        self.calls.append((sql, params))
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return frame.copy()


def test_xml_and_scalar_flag_parsing():
    xml = '<dccEnable>true</dccEnable><dccEnableAuth> false </dccEnableAuth>'
    assert _extract_xml_flag(xml, "dccEnable") == "true"
    assert _extract_xml_flag(xml, "dccEnableAuth") == "false"
    assert _extract_xml_flag(None, "dccEnable") is None
    assert _extract_xml_flag(xml, "missing") is None
    assert _is_flag_broken(None) is True
    assert _is_flag_broken("false") is True
    assert _is_flag_broken(0) is True
    assert _is_flag_broken("true") is False
    assert _is_flag_broken(1) is False


def test_find_broken_instances_and_locations_enrich_xml_flags():
    instances = BrokenConnection([pd.DataFrame([{
        "instance_identifier": "I1",
        "package_config_text": (
            "<dccEnable>true</dccEnable><dccEnableAuth>false</dccEnableAuth>"
        ),
    }])])
    result = find_broken_instances(instances, limit=10)
    assert result.loc[0, "handler_dccEnable"] == "true"
    assert result.loc[0, "handler_dccEnableAuth"] == "false"
    assert instances.calls[0][1] == (10,)

    locations = BrokenConnection([pd.DataFrame([
        {"location_no": "L1", "extra_function_text": "DCCXpressCO"},
        {"location_no": "L2", "extra_function_text": None},
    ])])
    result = find_broken_locations(locations, limit=5)
    assert list(result["location_DCCXpressCO"]) == ["true", "false"]
    assert locations.calls[0][1] == (5,)


def test_reference_queries_return_empty_frames_unchanged():
    empty = pd.DataFrame()
    assert find_broken_instances(BrokenConnection([empty])).empty
    assert find_broken_locations(BrokenConnection([empty])).empty
    assert find_broken_terminals(BrokenConnection([empty])).empty


def test_find_broken_terminals_enriches_instances_and_survives_join_failure():
    terminals = pd.DataFrame([{
        "terminal_identifier": "T1",
        "extra_function_text": "DCCXpressCO",
    }])
    instances = pd.DataFrame([{
        "instance_identifier": "T1",
        "package_config_text": "<dccEnable>true</dccEnable>",
    }])
    connection = BrokenConnection([terminals, instances])
    result = find_broken_terminals(connection, only_active=False, limit=20)
    assert result.loc[0, "location_DCCXpressCO"] == "true"
    assert result.loc[0, "handler_dccEnable"] == "true"
    assert connection.calls[0][1] == (20, 0)

    failing = BrokenConnection([terminals, RuntimeError("instance query failed")])
    result = find_broken_terminals(failing)
    assert result.loc[0, "location_DCCXpressCO"] == "true"
    assert "handler_dccEnable" not in result.columns


def test_compute_broken_flags_and_summary():
    frame = pd.DataFrame([
        {
            "handler_dccEnable": "true",
            "handler_dccEnableAuth": "false",
            "location_DCCXpressCO": "true",
        },
        {
            "handler_dccEnable": None,
            "handler_dccEnableAuth": "true",
            "location_DCCXpressCO": "false",
        },
    ])
    result = compute_broken_flags(frame)
    assert list(result["broken_flag_count"]) == [1, 2]
    summary = broken_summary(result)
    assert summary["total_terminals"] == 2
    assert summary["broken_terminals"] == 2
    assert summary["healthy_terminals"] == 0
    assert summary["by_flag"]["dccEnableAuth"] == 1


def test_empty_broken_summary_and_frame_without_known_flags():
    assert broken_summary(pd.DataFrame()) == {
        "total_terminals": 0,
        "broken_terminals": 0,
        "healthy_terminals": 0,
        "by_flag": {},
    }
    result = compute_broken_flags(pd.DataFrame([{"other": "value"}]))
    assert result.loc[0, "broken_flag_count"] == 0
