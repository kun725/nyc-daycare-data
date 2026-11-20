"""New roster centers must get a record in THIS repo, before the inspection sweep.

On 2026-11-19 the private build failed its freshness gate on a center whose
inspections were last fetched on Sept 22. The cause was structural: the step
that creates a record for a center new to the DOHMH roster ran only in the
private build. This repo, which does the scraping, never held those files, so
43 listed centers never had their inspection history fetched.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_inspections  # noqa: E402
import reconcile_active  # noqa: E402

BUILD = os.path.join(os.path.dirname(__file__), "..")


def test_capture_reconciles_before_the_inspection_sweep():
    src = open(os.path.join(BUILD, "capture.py"), encoding="utf-8").read()
    mint = src.index('"reconcile_active.py"')
    assert src.index('"fetch_active.py"') < mint < src.index('"fetch_inspections.py"')


def test_a_roster_center_with_no_file_gets_a_stub_the_sweep_will_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(reconcile_active, "FAC_DIR", str(tmp_path))
    roster = [{"dcid": "DC1", "program_name": "HAVE IT", "address": "1 MAIN STREET", "borough": "QUEENS"},
              {"dcid": "DC2", "program_name": "NEW ONE LLC", "address": "2 MAIN STREET", "borough": "QUEENS",
               "latitude": "40.7", "longitude": "-73.9"}]
    created = reconcile_active.add_missing(roster, {"DC1"}, report_only=False)
    assert created == 1
    files = os.listdir(tmp_path)
    assert files == ["chil-new-one-llc-2-main-street.json"]
    stub = json.load(open(tmp_path / files[0], encoding="utf-8"))
    assert stub["dcid"] == "DC2" and stub["dcid_source"] == "active_roster_add_missing"
    assert fetch_inspections.needs_refresh(stub, False, "2026-01-01")


def test_same_name_and_address_gets_a_distinct_file(tmp_path, monkeypatch):
    monkeypatch.setattr(reconcile_active, "FAC_DIR", str(tmp_path))
    roster = [{"dcid": "DC7", "program_name": "TWIN", "address": "5 A STREET", "borough": "BRONX"},
              {"dcid": "DC8", "program_name": "TWIN", "address": "5 A STREET", "borough": "BRONX"}]
    assert reconcile_active.add_missing(roster, set(), report_only=False) == 2
    assert sorted(os.listdir(tmp_path)) == ["chil-twin-5-a-street-dc8.json", "chil-twin-5-a-street.json"]


def test_never_fetched_centers_jump_the_queue():
    targets = [("a", {"portal_inspections_fetched": "2026-11-01"}),
               ("b", {}),
               ("c", {"portal_inspections_fetched": "2026-10-01"}),
               ("d", {"portal_inspections_fetched": ""})]
    assert [p for p, _ in fetch_inspections.never_fetched_first(targets)] == ["b", "d", "a", "c"]
