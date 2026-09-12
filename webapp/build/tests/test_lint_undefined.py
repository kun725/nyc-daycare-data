"""Undefined-name lint over every build module.

Born 2026-09-12: the crawler-v2 rewrite dropped mint_missing_homebased and
its helpers while replacing an adjacent block. Python compiles a call to a
missing module-level name just fine, so the registry fetch raised NameError
at RUNTIME — and because that pipeline step was not marked required, every
run printed the traceback and went green with stale registry data for 41
hours. pyflakes catches the whole class at PR time.
"""
import glob
import os
import subprocess
import sys

import pytest

BUILD = os.path.join(os.path.dirname(__file__), "..")


def _modules():
    return sorted(glob.glob(os.path.join(BUILD, "*.py")))


def test_no_undefined_names():
    pytest.importorskip("pyflakes")
    out = subprocess.run([sys.executable, "-m", "pyflakes"] + _modules(),
                         capture_output=True, text=True).stdout
    fatal = [l for l in out.splitlines()
             if "undefined name" in l or "used prior to global declaration" in l]
    assert not fatal, "undefined names in build modules:\n" + "\n".join(fatal)


def test_pipeline_entrypoints_exist():
    """The names refresh.py/capture.py call by module path must exist — a
    second net that works even where pyflakes is unavailable."""
    sys.path.insert(0, os.path.abspath(BUILD))
    import fetch_ocfs
    for name in ("fetch_all", "mint_missing_homebased", "scrape_profiles",
                 "_parse_info_v2", "_parse_history_v2", "_parse_violation_blocks",
                 "_parse_slots_v2", "_parse_hours_v2", "_checklist_segment"):
        assert hasattr(fetch_ocfs, name), f"fetch_ocfs.{name} is missing"
