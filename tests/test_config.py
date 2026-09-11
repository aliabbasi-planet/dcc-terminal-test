"""Environment configuration safety checks."""

from dcc_console.config import ENVIRONMENTS


def test_dev_uat_and_prod_are_available():
    assert set(ENVIRONMENTS) == {"DEV", "UAT", "PROD"}


def test_prod_requires_explicit_confirmation_for_live_runs():
    assert ENVIRONMENTS["PROD"].requires_typed_confirmation is True