"""Parser regression tests against real saved pages (fixtures captured
2026-09-10, the night the OCFS app's second unannounced migration was found).

These pin the parsers to known-true values from two real providers:
  71348  — Straight A Group Family Day Care (long record, June violations)
  937816 — A & Z Best Kids LLC (licensed 2025, three clean-itemized visits)

If OCFS changes markup again, these fail in CI before a broken parser ships;
the live canaries (test_canaries.py) fail on the schedule when the routes
themselves move. Together: parse-drift caught at PR time, route-drift caught
within hours instead of via a Reddit commenter.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_ocfs as fo  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def test_info_flags_71348():
    out = fo._parse_info_v2(fixture("ocfs_info_71348.html"))
    assert out["qs_participant"] is False
    assert out["administers_medication"] is False
    assert out.get("program_status") == "Open"
    # entity-encoded in the markup; the parser must decode it
    assert out.get("email") == "JOANA315@optonline.net"
    assert out.get("contact_name") == "Joana Twum, On-Site Provider"


def test_info_937816_email():
    """The state-published contact email must be captured verbatim — it
    feeds the same page button the DOE-listed Pre-K emails do."""
    out = fo._parse_info_v2(fixture("ocfs_info_937816.html"))
    assert out.get("email") == "tea.maghlaferidze@gmail.com"
    assert out.get("program_status") == "Open"
    assert out.get("contact_name") == "Tea Maglaperidze, On-Site Provider"


def test_rates_937816_posted_schedule():
    """The per-day hours ship as JSON in a script tag (allHoursData) and
    render client-side — a text-level parse sees only empty table headers
    and once called this provider's posted schedule "empty". The parser
    must read the JSON."""
    out = fo._parse_hours_v2(fixture("ocfs_rates_937816.html"))
    assert out["traditional"] is True
    assert out["nontraditional"] is False
    assert out["rates_posted"] is False
    assert out["as_of"] == "2025-05-01"
    sched = out["schedule"]
    assert [r["day"] for r in sched] == [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    assert all(r["start"] == "08:00 AM" and r["end"] == "07:00 PM"
               and r["type"] == "STANDARD" for r in sched)


def test_history_71348_visits():
    visits = fo._parse_history_v2(fixture("ocfs_history_71348.html"))
    dates = [v["date"] for v in visits]
    assert "2026-08-10" in dates and "2026-06-16" in dates
    aug = next(v for v in visits if v["date"] == "2026-08-10")
    june = next(v for v in visits if v["date"] == "2026-06-16")
    assert aug["found"] is True and aug["type"] == "Documentation Review"
    assert june["found"] is True and june["type"] == "Annual Unannounced"
    assert june["id"] == "2026-I-NYCDOH-001687"
    assert all(v["chk_id"] for v in (aug, june))


def test_violation_blocks_isolate_june_and_carry_state_status():
    """The checklist page carries EVERY visit; a blended parse once pinned
    June's five noncompliant items on August. Segmentation must isolate,
    and the status must be the STATE'S published word (a positional parse
    once read the Corrected-on-Site? column as the compliance status and
    published "Not Corrected" where the state says "Corrected")."""
    page = fixture("ocfs_checklist_71348.html")
    june = fo._parse_violation_blocks(
        fo._checklist_segment(page, "2026-I-NYCDOH-001687"))
    assert sorted(c for c, _, _, _ in june) == [
        "416.15(c)(3)", "416.15(c)(4)", "416.15(c)(6)", "416.7(h)", "416.7(l)"]
    for _, _, word, onsite in june:
        assert word == "Corrected"      # the state's own status word
        assert onsite is False
    aug = fo._parse_violation_blocks(
        fo._checklist_segment(page, "2026-I-NYCDOH-069870"))
    assert aug == []  # this capture (opened from June) doesn't itemize August


def test_violation_blocks_carry_each_codes_own_rule_text():
    """Descriptions must be label-keyed to their own code — the off-by-one
    positional parse gave 416.7(l) the outdoor-play text of 416.7(h)."""
    page = fixture("ocfs_checklist_71348.html")
    by_code = {c: d for c, d, _, _ in fo._parse_violation_blocks(
        fo._checklist_segment(page, "2026-I-NYCDOH-001687"))}
    assert "outdoor play" in by_code["416.7(h)"].lower()
    assert "napping" in by_code["416.7(l)"].lower()
    assert "maintain on file" in by_code["416.15(c)(3)"].lower()


def test_checklist_grid_fallback_still_parses():
    """_parse_checklist_v2 is the liveness/fallback parse; it must keep
    returning items (the canary asserts the same against the live page)."""
    page = fixture("ocfs_checklist_71348.html")
    items = fo._parse_checklist_v2(
        fo._checklist_segment(page, "2026-I-NYCDOH-001687"))
    assert len(items) >= 5
    marks = {st for _, _, st, _ in items}
    assert marks <= {"Y", "N", "N/A", "N/O", "P/V"}


def test_history_937816_three_visits_one_unitemized():
    visits = fo._parse_history_v2(fixture("ocfs_history_937816.html"))
    assert len(visits) >= 3
    dates = {v["date"] for v in visits}
    assert {"2026-09-03", "2026-04-23", "2026-03-25"} <= dates
    mar = next(v for v in visits if v["date"] == "2026-03-25")
    assert mar["found"] is True


def test_slots_71348():
    """Consecutive table rows share a pipe; the old consuming regex dropped
    every second row — losing the served-age range the age filter needs."""
    out = fo._parse_slots_v2(fixture("ocfs_slots_71348.html"))
    assert out["total"] == 3
    assert out["capacity"] == 14
    assert out["as_of"] == "2026-04-19"
    assert [g["group"] for g in out["groups"]] == [
        "Under 2 years", "6 Weeks to 12 Years"]
    assert out["age_served"] == {"label": "6 Weeks to 12 Years",
                                 "min_months": 1, "max_months": 144}


def test_slots_937816_served_range():
    out = fo._parse_slots_v2(fixture("ocfs_slots_937816.html"))
    assert out["capacity"] == 16
    assert out["age_served"]["label"] == "6 Weeks to 12 Years"
    assert out["age_served"]["min_months"] < 12   # takes babies under 1


def test_hours_71348():
    out = fo._parse_hours_v2(fixture("ocfs_rates_71348.html"))
    assert out["traditional"] is True
    assert out["nontraditional"] is False
    assert out["rates_posted"] is False


def test_scrape_budget_and_refresh_window(tmp_path, capsys, monkeypatch):
    """The nightly crawl must (a) skip providers crawled inside the refresh
    window and (b) stop itself before the workflow's hard timeout — a job
    killed at the timeout commits nothing (the 2026-09-11 backfill burned
    five hours of crawling and threw it away)."""
    import datetime as dt
    import json as _json
    reg = {"a": {"facility_id": "111", "program_type": "GFDC"},
           "b": {"facility_id": "222", "program_type": "FDC"}}
    regp = tmp_path / "reg.json"
    regp.write_text(_json.dumps(reg), encoding="utf-8")
    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    cachep = tmp_path / "cache.json"
    cachep.write_text(_json.dumps({"111": {"fetched_at": fresh, "inspections": []}}),
                      encoding="utf-8")
    monkeypatch.setattr(fo, "OUT", str(regp))
    monkeypatch.setattr(fo, "PROFILE_CACHE", str(cachep))
    done = fo.scrape_profiles(refresh_days=14, time_budget_min=0)
    out = capsys.readouterr().out
    assert "due for crawl: 1 of 2" in out      # fresh provider not re-crawled
    assert "time budget (0m) reached" in out   # clean stop, queue announced
    assert done == 0                           # no network was touched


def test_scrape_pool_runs_workers_offline(tmp_path, monkeypatch):
    """The pooled crawl must produce one complete entry per provider with
    workers > 1 — main thread owns every cache write, the budget lock hands
    out checklist slots, and a snapshot can never catch an entry half-built."""
    import json as _json
    reg = {chr(97 + i): {"facility_id": str(100 + i), "program_type": "GFDC"}
           for i in range(6)}
    regp = tmp_path / "reg.json"
    regp.write_text(_json.dumps(reg), encoding="utf-8")
    cachep = tmp_path / "cache.json"
    cachep.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fo, "OUT", str(regp))
    monkeypatch.setattr(fo, "PROFILE_CACHE", str(cachep))

    def fake_get(url, *a, **k):
        if "GetProgramInfo" in url:
            return "<td>Program Status:</td><td>Open</td><td>.</td>"
        if "GetInspectionHistory" in url:
            return "<div>no visits listed</div>"
        if "GetComplaintHistory" in url:
            return "<div>This Program has no complaint history to view</div>"
        return "<div>Prices/Rates data is not available.</div>"
    monkeypatch.setattr(fo, "_get_page", fake_get)

    done = fo.scrape_profiles(delay=0, workers=3)
    assert done == 6
    saved = _json.loads(cachep.read_text(encoding="utf-8"))
    assert len(saved) == 6
    for e in saved.values():
        assert e.get("program_status") == "Open"
        assert e.get("fetched_at")


def test_scrape_serial_inline_path(tmp_path, monkeypatch):
    """workers=1 must use the inline path (never more than one in-flight
    request) and produce identical entries."""
    import json as _json
    reg = {chr(97 + i): {"facility_id": str(200 + i), "program_type": "FDC"}
           for i in range(3)}
    (tmp_path / "reg.json").write_text(_json.dumps(reg), encoding="utf-8")
    (tmp_path / "cache.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fo, "OUT", str(tmp_path / "reg.json"))
    monkeypatch.setattr(fo, "PROFILE_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(fo, "_get_page", lambda url, *a, **k:
        "<td>Program Status:</td><td>Open</td><td>.</td>" if "GetProgramInfo" in url
        else "<div>none</div>")
    assert fo.scrape_profiles(delay=0, workers=1) == 3


def test_scrape_degrades_to_serial_on_parallel_block(tmp_path, monkeypatch, capsys):
    """A WAF that blocks parallel datacenter traffic looks like near-100%
    early errors (2026-09-11: 0 ok / 456 errors at 4 workers, 754 / 0
    serial). The crawl must drop to one worker on that signature and let
    the serial phase carry the health verdict."""
    import json as _json
    reg = {f"k{i}": {"facility_id": str(300 + i), "program_type": "GFDC"}
           for i in range(24)}
    (tmp_path / "reg.json").write_text(_json.dumps(reg), encoding="utf-8")
    (tmp_path / "cache.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fo, "OUT", str(tmp_path / "reg.json"))
    monkeypatch.setattr(fo, "PROFILE_CACHE", str(tmp_path / "cache.json"))
    calls = {"n": 0}
    def flaky(url, *a, **k):
        if "GetProgramInfo" in url:
            calls["n"] += 1
            if calls["n"] <= 16:      # the parallel phase: everything blocked
                raise OSError("connection refused")
        return ("<td>Program Status:</td><td>Open</td><td>.</td>"
                if "GetProgramInfo" in url else "<div>none</div>")
    monkeypatch.setattr(fo, "_get_page", flaky)
    done = fo.scrape_profiles(delay=0, workers=4)
    out = capsys.readouterr().out
    assert "crawl degrading" in out           # the trip announced itself
    assert "dropping to a single worker" in out
    assert done == 8                          # serial phase finished the rest
