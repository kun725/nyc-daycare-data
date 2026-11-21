"""Earlier years of a home-based provider's history must be captured.

2026-11-20: GetInspectionHistory/{id} with no year returns the current calendar
year only. The crawler read just that page, so 2,885 of 7,626 providers with
any visit held none before 2026. Provider 441669 is the worked example: the
default page lists 5 visits (all 2026); the 2025 page lists 5 more and the 2024
page 6 more, six of those eleven with violations found. Fixtures are those real
pages, saved 2026-11-20.

Everything here runs offline with the clock pinned to 2026-11-20.
"""
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_ocfs as fo  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
FID = "441669"


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def _setup(tmp_path, monkeypatch, cache=None, fail_years=(), blank_checklist=False):
    (tmp_path / "reg.json").write_text(
        json.dumps({"a": {"facility_id": FID, "program_type": "GFDC"}}), encoding="utf-8")
    (tmp_path / "cache.json").write_text(json.dumps(cache or {}), encoding="utf-8")
    monkeypatch.setattr(fo, "OUT", str(tmp_path / "reg.json"))
    monkeypatch.setattr(fo, "PROFILE_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(fo, "_today", lambda: dt.date(2026, 11, 20))
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)
        if "GetProgramInfo" in url:
            return "<td>Program Status:</td><td>Open</td><td>.</td>"
        if "GetInspectionHistory?" in url:
            year = int(url.rsplit("year=", 1)[1])
            if year in fail_years:
                raise OSError("timed out")
            name = f"ocfs_history_{FID}_{year}.html"
            return fixture(name) if os.path.exists(os.path.join(FIX, name)) else "<div>none</div>"
        if "GetInspectionHistory/" in url:
            return fixture(f"ocfs_history_{FID}_default.html")
        if "InspectionCheckList" in url:
            if blank_checklist:
                return "<div>nothing itemized</div>"
            return fixture(f"ocfs_checklist_{FID}_2025.html")
        if "GetComplaintHistory" in url:
            return "<div>This Program has no complaint history to view</div>"
        return "<div>Prices/Rates data is not available.</div>"
    monkeypatch.setattr(fo, "_get_page", fake_get)

    def entry():
        return json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))[FID]
    return calls, entry


def test_years_due_reads_a_past_year_once_and_the_year_just_ended_until_mid_february():
    nov = dt.date(2026, 11, 20)
    assert fo._years_due({}, nov) == [2025, 2024, 2023]
    assert fo._years_due({"hist_years": {"2025": "2026-11-20", "2024": "2026-11-20"}}, nov) == [2023]
    done = {"hist_years": {"2025": "2026-11-20", "2024": "2026-11-20", "2023": "2026-11-20"}}
    assert fo._years_due(done, nov) == []
    # January: 2026 has just ended and must be re-read. Reading it in January
    # does not settle it; reading it after Feb 15 does.
    assert fo._years_due(done, dt.date(2027, 1, 10)) == [2026]
    early = {"hist_years": dict(done["hist_years"], **{"2026": "2027-01-10"})}
    assert fo._years_due(early, dt.date(2027, 2, 1)) == [2026]
    late = {"hist_years": dict(done["hist_years"], **{"2026": "2027-02-20"})}
    assert fo._years_due(late, dt.date(2027, 3, 1)) == []


def test_the_default_page_alone_misses_2025_and_2024():
    default = fo._parse_history_v2(fixture(f"ocfs_history_{FID}_default.html"))
    assert {v["date"][:4] for v in default} == {"2026"} and len(default) == 5
    y25 = [v for v in fo._parse_history_v2(fixture(f"ocfs_history_{FID}_2025.html"))
           if v["date"] < "2026"]
    y24 = [v for v in fo._parse_history_v2(fixture(f"ocfs_history_{FID}_2024.html"))
           if v["date"] < "2026"]
    assert len(y25) == 5 and len(y24) == 6
    assert sum(v["found"] for v in y25 + y24) == 6


def test_crawl_fills_prior_years_once_with_the_state_status_words(tmp_path, monkeypatch, capsys):
    calls, entry = _setup(tmp_path, monkeypatch)
    assert fo.scrape_profiles(delay=0) == 1
    e = entry()
    ids = [i["id"] for i in e["inspections"]]
    assert len(ids) == len(set(ids)) == 16      # 5 + 5 + 6; the repeated newest visit once
    by_year = {}
    for i in e["inspections"]:
        by_year[i["date"][:4]] = by_year.get(i["date"][:4], 0) + 1
    assert by_year == {"2026": 5, "2025": 5, "2024": 6}
    assert e["hist_years"] == {"2025": "2026-11-20", "2024": "2026-11-20", "2023": "2026-11-20"}
    apr = next(i for i in e["inspections"] if i["date"] == "2025-04-02")
    assert apr["had_violation"] and len(apr["violations"]) >= 6
    assert {v["status"] for v in apr["violations"]} <= {"Corrected", "Corrected on-Site"}
    assert "1 of 1 providers hold all 3 prior years" in capsys.readouterr().out

    # Second crawl: nothing is due, so no per-year page is requested again.
    calls.clear()
    fo.scrape_profiles(delay=0)
    assert not [u for u in calls if "year=" in u]
    assert len(entry()["inspections"]) == 16


def test_an_old_visit_never_gets_a_derived_not_corrected(tmp_path, monkeypatch):
    """With no labeled status blocks, the grid fallback would mark a 2024
    citation Not Corrected and the site would call it open today."""
    _, entry = _setup(tmp_path, monkeypatch, blank_checklist=True)
    fo.scrape_profiles(delay=0)
    e = entry()
    old = [i for i in e["inspections"] if i["date"] < "2026" and i["had_violation"]]
    assert len(old) == 6
    assert all(i["violations"] == [] and i.get("not_itemized") for i in old)
    assert e["open_count"] == 0


def test_no_checklist_budget_leaves_the_year_due_and_the_next_crawl_fills_it(tmp_path, monkeypatch):
    _, entry = _setup(tmp_path, monkeypatch)
    fo.scrape_profiles(delay=0, checklist_budget=0)
    e = entry()
    assert sum(1 for i in e["inspections"]
               if i.get("details_pending") and i["date"] < "2026") == 6
    assert "2025" not in e["hist_years"] and "2024" not in e["hist_years"]
    assert e["hist_years"].get("2023") == "2026-11-20"      # nothing cited, so settled
    fo.scrape_profiles(delay=0)
    e = entry()
    assert len(e["inspections"]) == 16
    assert not [i for i in e["inspections"] if i.get("details_pending")]
    assert set(e["hist_years"]) == {"2025", "2024", "2023"}


def test_a_failed_year_page_does_not_fail_the_provider_and_stays_due(tmp_path, monkeypatch, capsys):
    _, entry = _setup(tmp_path, monkeypatch, fail_years=(2024,))
    assert fo.scrape_profiles(delay=0) == 1
    e = entry()
    assert "2024" not in e["hist_years"] and "2025" in e["hist_years"]
    assert {i["date"][:4] for i in e["inspections"]} == {"2026", "2025"}
    assert "2 year pages read, 1 failed" in capsys.readouterr().out


def test_mass_failure_of_year_pages_is_loud(tmp_path, monkeypatch, capsys):
    reg = {str(i): {"facility_id": str(1000 + i), "program_type": "FDC"} for i in range(8)}
    (tmp_path / "reg.json").write_text(json.dumps(reg), encoding="utf-8")
    (tmp_path / "cache.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fo, "OUT", str(tmp_path / "reg.json"))
    monkeypatch.setattr(fo, "PROFILE_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(fo, "_today", lambda: dt.date(2026, 11, 20))

    def fake_get(url, *a, **k):
        if "year=" in url:
            raise OSError("blocked")
        if "GetProgramInfo" in url:
            return "<td>Program Status:</td><td>Open</td><td>.</td>"
        return "<div>none</div>"
    monkeypatch.setattr(fo, "_get_page", fake_get)
    with pytest.raises(SystemExit) as ex:
        fo.scrape_profiles(delay=0)
    assert ex.value.code == 3
    assert "OCFS per-year history failing" in capsys.readouterr().out
