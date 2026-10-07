"""Tests for the console's top-level layout routing (``app.prod_streamlined``).

On PROD the console hides the single-test (CAB report), Campaigns and Broken-Terminals
tabs and shows only the DCC Remediation experience; DEV/UAT keep the full tab strip.
"""

from __future__ import annotations

import pytest

from dcc_console.app import prod_streamlined


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("PROD", True),
        ("prod", True),
        ("UAT", False),
        ("DEV", False),
        ("", False),
        (None, False),
    ],
)
def test_prod_streamlined(env, expected):
    assert prod_streamlined(env) is expected
