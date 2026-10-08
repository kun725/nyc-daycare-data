"""The per-run caps must survive a blank or malformed environment variable.

os.environ.get("INSPECTIONS_LIMIT", "900") returns "" when the variable is set
but empty (a workflow_dispatch input left blank does this), and `--limit ""`
makes the fetcher exit. Both sweeps are required=False, so the run stayed green
while inspections were silently skipped.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import capture  # noqa: E402


@pytest.mark.parametrize("value", ["", "   ", "abc", "0", "-5", "1.5"])
def test_blank_or_bad_value_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("INSPECTIONS_LIMIT", value)
    assert capture.limit_arg("INSPECTIONS_LIMIT") == "900"


def test_unset_falls_back_to_the_default(monkeypatch):
    monkeypatch.delenv("WATER_LEAD_LIMIT", raising=False)
    assert capture.limit_arg("WATER_LEAD_LIMIT") == "900"


def test_a_real_number_is_honoured(monkeypatch):
    monkeypatch.setenv("INSPECTIONS_LIMIT", " 450 ")
    assert capture.limit_arg("INSPECTIONS_LIMIT") == "450"


def test_capture_passes_the_helper_not_the_raw_environment():
    src = open(os.path.join(os.path.dirname(__file__), "..", "capture.py"), encoding="utf-8").read()
    assert 'limit_arg("INSPECTIONS_LIMIT")' in src and 'limit_arg("WATER_LEAD_LIMIT")' in src
    assert 'os.environ.get("INSPECTIONS_LIMIT"' not in src
