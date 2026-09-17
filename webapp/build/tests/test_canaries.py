"""Live source canaries — the scheduled slice of the test suite that talks to
the real sources. One known-good record per source; failures mean a source
moved, renamed, or went dark, and they surface within hours instead of via a
missing-facility report a month later (2026-09-10: the county-name mismatch
and the OCFS route move were both invisible to every existing check).

Run with:  pytest -m canary          (the scheduled workflow job)
Excluded:  pytest -m "not canary"    (the default PR job — offline, fast)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_ocfs as fo  # noqa: E402

pytestmark = pytest.mark.canary

UA = {"User-Agent": "NYCDaycareCheck-canary/1.0 (+https://nycdaycarecheck.com)"}


def _get(url, timeout=45, tries=3):
    """5xx and timeouts retry (Socrata flaps 503s with valid payloads in
    between — observed 2026-09-11); 4xx fails immediately, because a 404
    is a moved route and exactly the alarm these probes exist to raise."""
    import time
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except urllib.error.URLError as e:
            last = e
        if attempt < tries - 1:
            time.sleep(20)
    raise last


def _soda(url, **params):
    return json.loads(_get(url + "?" + urllib.parse.urlencode(params)))


BOROUGHS = ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island")


def test_ocfs_registry_shape():
    """cb42-qumz: NYC rows exist under the jurisdiction code, all boroughs."""
    rows = _soda("https://data.ny.gov/resource/cb42-qumz.json",
                 **{"$select": "county,count(*)", "$group": "county",
                    "$where": "region_code='NYCDOH'"})
    counts = {}
    for r in rows:
        b = fo._BORO_OF.get((r.get("county") or "").strip())
        if b:
            counts[b] = counts.get(b, 0) + int(r["count"])
    assert sum(counts.values()) >= 8000, counts
    for b in BOROUGHS:
        assert counts.get(b, 0) >= 100, f"{b} missing from OCFS registry: {counts}"


def test_dohmh_roster_shape():
    rows = _soda("https://data.cityofnewyork.us/resource/gy3q-4tzp.json",
                 **{"$select": "borough,count(*)", "$group": "borough"})
    total = sum(int(r["count"]) for r in rows)
    assert total >= 2400, rows


def test_ocfs_profile_app_alive():
    """The consumer app's three core routes parse for a known provider —
    this is the canary that would have caught both route migrations."""
    info = fo._parse_info_v2(_get(fo.INFO_TMPL.format("71348")))
    assert info.get("program_status"), info
    visits = fo._parse_history_v2(_get(fo.HIST_TMPL.format("71348")))
    assert len(visits) >= 3 and all(v["date"] for v in visits)
    with_chk = [v for v in visits if v["chk_id"]]
    assert with_chk, "no checklist links found — markup changed"
    # Walk visits newest-first. Some visits legitimately carry no items —
    # the state's own page says "Checklist items were not found for this
    # inspection" — and a just-posted inspection usually has none yet. So
    # asserting on whichever visit happens to be newest fires a false alarm
    # the morning after any inspection posts (it did, 2026-09-17). Liveness
    # is: the route answers, and SOME visit here still itemizes.
    best = 0
    for v in with_chk[:5]:
        page = _get(fo.CHK_TMPL.format(v["chk_id"], "71348"))
        if "Checklist items were not found" in page:
            continue          # an expected, explicit answer from the source
        seg = fo._checklist_segment(page, v["id"])
        best = max(best, len(fo._parse_checklist_v2(seg)))
        if best >= 5:
            break
    assert best >= 5, "no visit on this provider itemized — checklist parse collapsed"
    # A cited visit should yield labeled blocks carrying the state's own
    # status word. Same caveat as above: the state marks some cited visits
    # "violations found" without publishing the items, so require blocks
    # from SOME cited visit rather than from whichever is newest.
    blocks = []
    for v in [x for x in with_chk if x["found"]][:4]:
        page = _get(fo.CHK_TMPL.format(v["chk_id"], "71348"))
        if "Checklist items were not found" in page:
            continue
        blocks = fo._parse_violation_blocks(fo._checklist_segment(page, v["id"]))
        if blocks:
            break
    if blocks:
        assert all(w or o is not None for _, _, w, o in blocks), blocks


def test_headstart_dataset_reachable():
    req = urllib.request.Request(fo.__dict__.get("S3_JSON")
                                 or "https://s3foa.s3.us-east-1.amazonaws.com/HS_Service_Locations.json",
                                 headers=UA, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        head = r.read(200000).decode("utf-8", "replace")
    assert '"state"' in head and '"city"' in head


def test_myschools_reachable():
    body = _get("https://www.myschools.nyc/en/api/v2/schools/process/2/?limit=1")
    assert "results" in body or "count" in body


# Every link methodology's "Check everything yourself" section publishes,
# minus hosts that block automated requests (ocfs.ny.gov and headstart.gov
# refuse non-browser clients; their liveness is covered by the hs.ocfs and
# Head Start S3 probes above).
RESOURCE_LINKS = [
    "https://a816-healthpsi.nyc.gov/ChildCare/",
    "https://data.cityofnewyork.us/d/gy3q-4tzp",
    "https://www.nyc.gov/assets/doh/downloads/pdf/about/healthcode/health-code-article47.pdf",
    "https://data.ny.gov/d/cb42-qumz",
    "https://data.cityofnewyork.us/d/wvxf-dwi5",
    "https://data.cityofnewyork.us/d/3h2n-5cm9",
    "https://data.cityofnewyork.us/d/eabe-havv",
    "https://data.cityofnewyork.us/d/erm2-nwe9",
    "https://data.cityofnewyork.us/d/p937-wjvj",
    "https://www.myschools.nyc",
    "https://data.nysed.gov",
    "https://qualitystarsny.org",
    "https://openfoil.ny.gov",
    "https://data.ny.gov/d/i9wp-a4ja",
]


def test_published_resource_links_resolve():
    """The "check everything yourself" links on the methodology page, probed
    so a moved rulebook PDF or a renamed dataset page cannot rot silently on
    the page whose whole job is proving our sources."""
    dead = []
    for url in RESOURCE_LINKS:
        try:
            _get(url, timeout=30, tries=2)
        except Exception as e:
            dead.append(f"{url} -> {type(e).__name__}: {e}")
    assert not dead, dead
