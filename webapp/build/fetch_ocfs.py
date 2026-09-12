#!/usr/bin/env python3
"""
NYC Daycare Check — OCFS home-based registry fetcher (Phase F1).

Pulls the NYS OCFS "Child Care Regulated Programs" dataset (data.ny.gov,
`cb42-qumz`) via the Socrata SODA API for the five NYC counties. This dataset
is FREE, has no key requirement (an app token only raises rate limits), and is
updated daily.

Why this exists: our home-based (OCFS) tier already carries registry basics
(name, capacity, coords, status), but it lacks the per-facility OCFS PROFILE
URL — `hs.ocfs.ny.gov/dcfs/Profile/Index/{facility_id}` — which is the only
place individual inspection/violation history is published. This script
captures that facility_id + profile URL per provider and writes a sidecar map:

    webapp/data/processed/ocfs_profiles.json   { "<license/key>": {facility_id, profile_url, ...} }

build_data.py / the Phase-F2 profile scraper consume that map. We do NOT fake
inspection data here — F1 only enriches the registry and unlocks F2.

Usage:
    python webapp/build/fetch_ocfs.py            # fetch NYC registry -> sidecar
    python webapp/build/fetch_ocfs.py --offline  # report from existing sidecar only

Legality/etiquette: official open-data API, public data; we identify a UA and
page politely. (Profile scraping itself lives in F2, rate-limited.)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import datetime as _dt
import re as _re
import html as _html
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "ocfs_profiles.json"))

SODA = "https://data.ny.gov/resource/cb42-qumz.json"
# BOTH county-name schemes, because this dataset uses BOROUGH names
# (Brooklyn / Manhattan / Staten Island) where most NY datasets use legal
# county names (Kings / New York / Richmond). The original list used legal
# names only; Bronx and Queens are spelled the same in both schemes, so the
# query silently returned two boroughs for the site's entire life and every
# downstream count looked plausible (found 2026-09-10 via a launch-thread
# missing-facility report). Querying all eight names is harmless (a name
# matches in whichever scheme the state uses) and survives a future rename.
NYC_COUNTIES = ["Bronx", "Brooklyn", "Kings", "Manhattan", "New York",
                "Queens", "Richmond", "Staten Island"]
# Normalized borough for the coverage check below.
_BORO_OF = {"Bronx": "Bronx", "Brooklyn": "Brooklyn", "Kings": "Brooklyn",
            "Manhattan": "Manhattan", "New York": "Manhattan",
            "Queens": "Queens", "Richmond": "Staten Island",
            "Staten Island": "Staten Island"}
# 2026-07: OCFS's redesign moved profiles from /dcfs/Profile/Index/{id} (now
# 404) to /DCFS1/Profile/Index/{id}. The new route is a plain un-gated GET —
# the reCAPTCHA on the redesigned app covers only its search-form POSTs, which
# this pipeline never touches (facility ids come from the cb42-qumz roster).
PROFILE_TMPL = "https://hs.ocfs.ny.gov/dcfs/Search/GetProgramInfo/{}"
# The profile page's "24-month" history quietly OMITS rows (verified: facility
# 621637 was missing a violations-found Sep 2024 visit and a Mar 2025 visit
# that the search surface's per-year endpoint returns). So the per-year
# endpoint is the authoritative filler: fetch each of the last 4 calendar
# years and merge, keeping the profile's rows where they overlap (only those
# carry itemized violation detail). Final history is capped at 36 months so
# the site's "last 3 years" framing is backed by exactly that window.
YEAR_TMPL = "https://hs.ocfs.ny.gov/dcfs/Search/GetInspectionHistory?id={}&year={}"
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")  # optional, raises rate limit
WORKERS = int(os.environ.get("OCFS_WORKERS", "8"))  # concurrent profile fetches


def soda_get(where, limit=1000, offset=0):
    params = {"$where": where, "$limit": limit, "$offset": offset,
              "$order": "facility_id"}
    url = SODA + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA}
    if APP_TOKEN:
        headers["X-App-Token"] = APP_TOKEN
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def key_for(row):
    """Stable join key to match against our processed OCFS records."""
    lic = (row.get("facility_id") or "").strip()
    return lic or (row.get("facility_name", "") + "|" + row.get("zip_code", "")).lower()


def fetch_all():
    # Primary filter is the JURISDICTION marker, not county names: NYC rows
    # carry region_code NYCDOH (verified equal to the 8-name county union,
    # 8,575 = 8,575, 2026-09-10). County names stay only as the fallback arm
    # so a change to either field alone cannot starve the pull again.
    where = "region_code='NYCDOH' OR county in({})".format(
        ",".join("'%s'" % c for c in NYC_COUNTIES))
    out, offset = {}, 0
    while True:
        # A page failure USED to break the loop and write whatever had been
        # collected: one Socrata flake produced a 4,000-row "registry" out of
        # 8,569 (seen 2026-09-12). Retry, then refuse to write a partial pull
        # — a truncated registry silently un-publishes real providers.
        rows = None
        for attempt in range(4):
            try:
                rows = soda_get(where, 1000, offset)
                break
            except Exception as e:
                print(f"  ! fetch failed at offset {offset} (attempt {attempt + 1}): {e}")
                time.sleep(5 * (attempt + 1))
        if rows is None:
            print(f"::error title=OCFS registry pull truncated::Failed at offset "
                  f"{offset} after 4 attempts; refusing to write a partial registry.")
            sys.exit(4)
        if not rows:
            break
        for row in rows:
            fid = (row.get("facility_id") or "").strip()
            if not fid:
                continue
            out[key_for(row)] = {
                "facility_id": fid,
                "profile_url": PROFILE_TMPL.format(fid),
                "program_type": row.get("program_type"),
                "status": row.get("facility_status"),
                "county": row.get("county"),
                "name": row.get("facility_name"),
                "zip": row.get("zip_code"),
                # Kept since 2026-09-11 so the registry can MINT raw facility
                # records for providers licensed after the original seed (the
                # seed was frozen; see mint_missing_homebased below).
                "address": " ".join(x for x in (
                    (row.get("street_number") or "").strip(),
                    (row.get("street_name") or "").strip()) if x) or None,
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "phone": (row.get("phone_number") or "").strip() or None,
                "total_capacity": row.get("total_capacity"),
                # Parent-relevant registry depth (surfaced on home-based pages):
                # how long the provider has operated, license validity window,
                # and the per-age capacity mix behind the single total.
                "opened_date": (row.get("facility_opened_date") or "")[:10] or None,
                "license_issue_date": (row.get("license_issue_date") or "")[:10] or None,
                "license_expiration_date": (row.get("license_expiration_date") or "")[:10] or None,
                "capacity_mix": {k: int(v) for k, v in {
                    "infant": row.get("infant_capacity"),
                    "toddler": row.get("toddler_capacity"),
                    "preschool": row.get("preschool_capacity"),
                    "school_age": row.get("school_age_capacity"),
                }.items() if v and str(v).isdigit() and int(v) > 0} or None,
            }
        offset += len(rows)
        print(f"  …{offset} NYC programs")
        time.sleep(1.0)  # polite
        if len(rows) < 1000:
            break
    # Coverage gate: every borough must be represented, loudly. This exact
    # failure (a county-name mismatch starving three boroughs) ran silent
    # from the initial commit because nothing ever compared what we RECEIVED
    # against the city's real shape. Absence has no row to audit, so the
    # check has to live here, at the point of receipt.
    by_boro = {}
    for rec in out.values():
        b = _BORO_OF.get((rec.get("county") or "").strip(), "?")
        by_boro[b] = by_boro.get(b, 0) + 1
    print("  registry by borough:", by_boro)
    missing = [b for b in ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island")
               if by_boro.get(b, 0) < 25]
    if missing:
        print(f"::warning title=OCFS registry borough gap::No/low home-based rows for: {', '.join(missing)} — "
              "the upstream county naming may have changed again. Investigate before trusting home-based coverage.")
    return out


PROFILE_CACHE = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "ocfs_violations.json"))


# ── Crawler v2 (2026-09-10): OCFS moved apps AGAIN — /DCFS1/ now 404s for
# every provider (histories silently frozen since ~Aug 12 until the L4 age
# gate existed to notice). The live app is server-rendered under
# /dcfs/Search/: GetProgramInfo/{id} (status + QUALITYstars + medication
# flags), GetInspectionHistory/{id} (every visit: date, type, violations
# found, inspection id, checklist link), InspectionCheckList?inspectionId=
# (per-visit item-level detail: reg code, requirement text, compliance
# Y/N/N-O/N-A/P-V, corrected on-site), GetComplaintHistory/{id}.
# The new app does NOT publish per-violation corrected status or an
# uncorrected-total, so v2 DERIVES openness and says so: a violation is
# open when its code's latest appearance is Noncompliant without on-site
# correction, or Prior-Violation in the latest visit. Cached pre-Aug-12
# entries keep their state-published statuses; only new visits append.
def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

INFO_TMPL = "https://hs.ocfs.ny.gov/dcfs/Search/GetProgramInfo/{}"
HIST_TMPL = "https://hs.ocfs.ny.gov/dcfs/Search/GetInspectionHistory/{}"
CHK_TMPL = "https://hs.ocfs.ny.gov/dcfs/search/InspectionCheckList?inspectionId={}&facilityId={}"
CMP_TMPL = "https://hs.ocfs.ny.gov/dcfs/Search/GetComplaintHistory/{}"

def _get_page(url, timeout=25, attempts=3):
    last = None
    for a in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError:
            raise                       # 404/403 are real answers, not flakes
        except Exception as e:
            last = e
            time.sleep(2 * (a + 1))
    raise last

def _piped(html_text):
    t = _re.sub(r"<script.*?</script>", " ", html_text, flags=_re.S)
    t = _re.sub(r"<[^>]+>", "|", t)
    t = _html.unescape(t)
    return _re.sub(r"[\s|]*\|[\s|]*", "|", t)

def _parse_info_v2(page):
    p = _piped(page)
    def flag(label):
        m = _re.search(_re.escape(label) + r"\|?:?\|(Yes|No)", p, _re.I)
        return (m.group(1).lower() == "yes") if m else None
    out = {"qs_participant": flag("QUALITYstarsNY Participant"),
           "administers_medication": flag("Administers Medication"),
           "removed_from_referral": flag("Removed from Referral List")}
    m = _re.search(r"Program Status:\|([A-Za-z ]{2,30})\|", p)
    if m: out["program_status"] = m.group(1).strip()
    # The state-published contact email for the licensed business (the same
    # basis as the DOE-listed Pre-K emails). Absent when the provider lists
    # none.
    m = _re.search(r"Email:\|([^|]+@[^|]+?)\|", p)
    if m: out["email"] = m.group(1).strip()
    m = _re.search(r"Contact Name:\|([^|]{3,80}?)\|", p)
    if m: out["contact_name"] = _re.sub(r"\s{2,}", " ", m.group(1).strip())
    return out

def _parse_history_v2(page):
    """Visit list from GetInspectionHistory: [{date,type,id,found,chk_id}].
    Each visit's checklist LINK precedes its field block, but a naive
    split-after-link put the NEXT link inside the current segment and paired
    every visit with its neighbor's checklist (caught by the fixture tests:
    "August's" checklist contained June's items). Pair by position instead:
    link k opens segment k; fields inside segment k belong with link k."""
    links = [(m.start(), m.group(1))
             for m in _re.finditer(r'inspectionId=(\d+)', page)]
    visits = []
    for k, (pos, chk_id) in enumerate(links):
        end = links[k + 1][0] if k + 1 < len(links) else len(page)
        p = _piped(page[pos:end])
        d = _re.search(r"Inspection Date:\|?(\d{2}/\d{2}/\d{4})", p)
        if not d:
            continue
        f = _re.search(r"Violations found:\|?\s*(Yes|No)", p, _re.I)
        ty = _re.search(r"Inspection Type:\|([^|]{3,60})\|", p)
        iid = _re.search(r"Inspection ID:\|?\|?([0-9A-Z-]{6,40})\|", p)
        mm, dd, yy = d.group(1).split("/")
        visits.append({"date": f"{yy}-{mm}-{dd}",
                       "type": (ty.group(1).strip() if ty else ""),
                       "id": (iid.group(1) if iid else ""),
                       "found": bool(f and f.group(1).lower() == "yes"),
                       "chk_id": chk_id})
    return visits

_CODE_RE = _re.compile(r"^\d{3}\.\d+")
def _checklist_segment(page, want_id):
    """The checklist page carries EVERY visit's checklist; slice out the one
    whose Inspection ID matches, so items are never attributed across visits
    (a blended parse pinned June's items on August in testing)."""
    if not want_id:
        return page
    marks = [m.start() for m in _re.finditer(r"Inspection ID:", page)]
    for idx, start in enumerate(marks):
        end = marks[idx + 1] if idx + 1 < len(marks) else len(page)
        if want_id in page[start:start + 400]:
            return page[start:end]
    return ""

_CHK_LABELS = ("Date Cited", "Regulation", "Regulation Description",
               "Compliance Status", "Corrected on-Site?", "Corrected on-Site")

def _parse_violation_blocks(seg):
    """The authoritative violations parse. Each cited violation appears on the
    checklist page as a labeled block:
      Regulation | <code> | Regulation Description | <rule text>
      | Compliance Status | <the state's own word, e.g. Corrected/Not Corrected>
      | Corrected on-Site? | <Y/N>
    Returns [(code, desc, state_status_word, corrected_onsite)]. The same data
    also renders as an unlabeled table above the blocks; only the blocks are
    read (label-keyed, so a column reorder cannot silently shift fields \u2014 an
    earlier positional parse published our derived "Not Corrected" where the
    state's own word was "Corrected")."""
    toks = [t.strip() for t in _piped(seg).split("|")]
    out, i = [], 0
    while i < len(toks) - 1:
        if toks[i] == "Regulation" and _CODE_RE.match(toks[i + 1]) and len(toks[i + 1]) < 40:
            code, desc, word, onsite = toks[i + 1], "", "", None
            j = i + 2
            while j < len(toks) - 1 and toks[j] not in ("Regulation", "Date Cited"):
                nxt = toks[j + 1]
                if toks[j] == "Regulation Description" and nxt not in _CHK_LABELS:
                    desc = nxt
                elif toks[j] == "Compliance Status" and nxt not in _CHK_LABELS:
                    word = nxt
                elif toks[j].startswith("Corrected on-Site") and nxt not in _CHK_LABELS:
                    onsite = nxt.upper() in ("Y", "YES")
                j += 1
            out.append((code, desc, word, onsite))
            i = j
        else:
            i += 1
    return out

def _parse_checklist_v2(page):
    """Fallback/liveness parse of the full checklist markup (the compliance
    grid plus the violations table) -> [(code, text, mark, corrected_onsite)].
    Grid rows put the requirement text BEFORE the code; table rows put it
    after \u2014 so try forward first, then back, skipping every label cell.
    Violations themselves come from _parse_violation_blocks; this exists so
    the canary can prove the page still parses and as a last-resort source
    when a segment carries no labeled blocks."""
    p = _piped(page)
    toks = [t.strip() for t in p.split("|")]
    items = []
    def _is_text(t):
        return (len(t) > 15 and not _CODE_RE.match(t) and t not in _CHK_LABELS
                and not t.startswith("Y-Compliant"))
    for i, t in enumerate(toks):
        if _CODE_RE.match(t) and len(t) < 40:
            status, corrected, status_at = "", None, None
            for j in range(i + 1, min(i + 6, len(toks))):
                tj = toks[j].upper().replace("\u2011", "-")
                if tj in ("Y", "N", "N/A", "N/O", "P/V"):
                    status, status_at = tj, j
                    for k in range(j + 1, min(j + 4, len(toks))):
                        if toks[k] in ("Yes", "No"):
                            corrected = toks[k] == "Yes"
                            break
                    break
            if not status:
                continue
            desc = ""
            for j in range(i + 1, status_at):
                if _is_text(toks[j]):
                    desc = toks[j]
                    break
            if not desc:
                for j in range(i - 1, max(i - 14, -1), -1):
                    if _is_text(toks[j]):
                        desc = toks[j]
                        break
            items.append((t, desc, status, corrected))
    return items

def _parse_slots_v2(page):
    """Capacity & open slots: self-reported, refreshed daily by the state.
    {total, capacity, as_of, groups:[{group, slots, capacity}]}"""
    p = _piped(page)
    out = {}
    m = _re.search(r"There are\|(\d+)\|open slots", p)
    if m:
        out["total"] = int(m.group(1))
    m = _re.search(r"self-reported to OCFS on\|?\s*(\d{1,2}/\d{1,2}/\d{4})", p)
    if m:
        mm, dd, yy = m.group(1).split("/")
        out["as_of"] = f"{yy}-{int(mm):02d}-{int(dd):02d}"
    # Walk the table as tokens: consecutive rows share their delimiter, so
    # a consuming regex silently dropped every second row (the "6 Weeks to
    # 12 Years" served-range row was lost that way).
    toks = [t.strip() for t in p.split("|")]
    groups = []
    try:
        i = toks.index("Age Group") + 3   # skip the header triplet
    except ValueError:
        i = len(toks)
    _val = ("Not Available", "Slots Available (contact program)")
    while i + 2 < len(toks):
        g, slots, cap = toks[i], toks[i + 1], toks[i + 2]
        if not (slots.isdigit() or slots in _val) or not (cap.isdigit() or cap in _val):
            break
        if g.lower() == "totals":
            if slots.isdigit():
                out["total"] = int(slots)
            if cap.isdigit():
                out["capacity"] = int(cap)
            break
        groups.append({"group": g,
                       "slots": int(slots) if slots.isdigit() else None,
                       "capacity": int(cap) if cap.isdigit() else None})
        i += 3
    if groups:
        out["groups"] = groups
        served = age_range_served(groups)
        if served:
            out["age_served"] = served
    return out or None

_RANGE_RE = _re.compile(r"^(\d+)\s*(Week|Month|Year)s?\s+to\s+(\d+)\s*(Week|Month|Year)s?$", _re.I)
def _months(n, unit):
    u = unit.lower()
    return n * 12 if u == "year" else (n if u == "month" else max(round(n / 4.345), 1))

def age_range_served(groups):
    """The overall served-age row (e.g. "6 Weeks to 12 Years") — distinct
    from regulatory sub-bands like "Under 2 years". Returns the state's own
    label verbatim plus derived month bounds, for age filtering."""
    for g in groups:
        m = _RANGE_RE.match(g.get("group") or "")
        if m:
            lo = _months(int(m.group(1)), m.group(2))
            hi = _months(int(m.group(3)), m.group(4))
            return {"label": g["group"], "min_months": lo, "max_months": hi}
    return None

def _parse_hours_v2(page):
    """Schedule & rates: traditional-hours flags, the provider's posted
    per-day hours, and whether rates are posted. The hours table is NOT in
    the rendered markup — the page ships it as JSON in a script tag
    (allHoursData) and renders client-side, so a text-level parse sees only
    the empty table headers (that misread called a provider's posted
    Mon-Fri 8-7 schedule "empty" in review)."""
    p = _piped(page)
    out = {}
    m = _re.search(r"Traditional Hours:\|(Yes|No)", p)
    if m:
        out["traditional"] = m.group(1) == "Yes"
    m = _re.search(r"Non-Traditional Hours:\|(Yes|No)", p)
    if m:
        out["nontraditional"] = m.group(1) == "Yes"
    out["rates_posted"] = "not available" not in p.lower()
    m = _re.search(r"self-reported to OCFS on\|?\s*(\d{1,2}/\d{1,2}/\d{4})", p)
    if m:
        mm, dd, yy = m.group(1).split("/")
        out["as_of"] = f"{yy}-{int(mm):02d}-{int(dd):02d}"
    m = _re.search(r"allHoursData\s*=\s*(\[.*?\])\s*;", page, _re.S)
    if m:
        try:
            rows = []
            for block in json.loads(m.group(1)):
                for h in (block.get("Hours") or []):
                    rows.append({"day": h.get("Day"), "start": h.get("StartTime"),
                                 "end": h.get("EndTime"),
                                 "type": block.get("Type")})
            if rows:
                out["schedule"] = rows
        except ValueError:
            pass
    return out or None

def _parse_complaints_v2(page):
    p = _piped(page)
    if "no complaint history" in p.lower():
        return {"count": 0}
    dates = _re.findall(r"(\d{2}/\d{2}/\d{4})", p)
    return {"count": max(len(dates), 1) if dates else None}

def scrape_profiles(limit=None, delay=2.0, checklist_budget=2500,
                    refresh_days=None, time_budget_min=None, workers=1):
    cache = {}
    if os.path.exists(PROFILE_CACHE):
        cache = json.load(open(PROFILE_CACHE, encoding="utf-8"))
    registry = {}
    if os.path.exists(OUT):
        registry = json.load(open(OUT, encoding="utf-8"))
    fids = []
    for rec in registry.values():
        fid = "".join(c for c in str(rec.get("facility_id") or "") if c.isdigit())
        if fid and (rec.get("program_type") in ("FDC", "GFDC")):
            fids.append(fid)
    if refresh_days:
        # Providers crawled inside the window are not due — steady-state
        # nights only touch the stale slice instead of the whole fleet.
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(days=refresh_days)).isoformat()
        total_known = len(fids)
        fids = [f for f in fids
                if ((cache.get(f) or {}).get("fetched_at") or "") < cutoff]
        print(f"due for crawl: {len(fids)} of {total_known} "
              f"(refresh window {refresh_days}d)")
    # new/never-crawled first, then stalest
    def prio(f):
        e = cache.get(f) or {}
        return (0 if not e.get("fetched_at") else 1, e.get("fetched_at") or "")
    fids.sort(key=prio)
    if limit:
        fids = fids[:limit]
    import copy
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    done = errors = new_visits = 0
    _bud_lock = threading.Lock()
    _bud = {"used": 0}

    def _chk_allow():
        # Checklist budget shared across workers; check-and-take under a lock.
        with _bud_lock:
            if _bud["used"] < checklist_budget:
                _bud["used"] += 1
                return True
            return False

    def _crawl_one(fid):
        """Fetch one provider completely. Runs on a worker thread: touches no
        shared state (the prior entry arrives as a copy, checklist pages are
        cached per provider, the budget is taken through _chk_allow) and
        keeps the full per-request politeness delay per worker."""
        entry = copy.deepcopy(cache.get(fid) or {"inspections": []})
        nv = 0
        _chk_local = {}
        info = _parse_info_v2(_get_page(INFO_TMPL.format(fid)))
        time.sleep(delay)
        visits = _parse_history_v2(_get_page(HIST_TMPL.format(fid)))
        time.sleep(delay)
        known = {i.get("id") for i in entry.get("inspections", []) if i.get("id")}
        for v in visits:
            if v["id"] and v["id"] in known:
                continue
            insp = {"date": v["date"], "type": v["type"], "id": v["id"],
                    "result": "Violations Found" if v["found"] else "No Violations Found",
                    "had_violation": v["found"], "violations": []}
            if v["found"] and v["chk_id"] and _chk_allow():
                try:
                    # The page itemizes only the visit it was opened for,
                    # so every cited visit gets its own fetch (keyed by
                    # chk_id — a provider-level cache starved all but the
                    # newest cited visit into "not_itemized").
                    _chk_page = _chk_local.get(v["chk_id"])
                    if _chk_page is None:
                        _chk_page = _get_page(CHK_TMPL.format(v["chk_id"], fid))
                        _chk_local[v["chk_id"]] = _chk_page
                    seg = _checklist_segment(_chk_page, v["id"])
                    time.sleep(delay)
                    # Labeled per-violation blocks carry the state's own
                    # published status word — use it verbatim, never a
                    # derived stand-in for it.
                    for code, desc, word, onsite in _parse_violation_blocks(seg):
                        insp["violations"].append({
                            "code": code, "desc": desc,
                            "status": ("Corrected on-Site" if onsite
                                       else (word or "Not Corrected"))})
                    if not insp["violations"]:
                        for code, desc, stat, corrected in _parse_checklist_v2(seg):
                            if stat == "N":
                                insp["violations"].append({
                                    "code": code, "desc": desc,
                                    "status": "Corrected on-Site" if corrected else "Not Corrected"})
                            elif stat == "P/V":
                                insp["violations"].append({
                                    "code": code, "desc": desc,
                                    "status": "Prior Violation - observed again"})
                except Exception:
                    insp["details_pending"] = True
            elif v["found"]:
                insp["details_pending"] = True
            if v["found"] and not insp["violations"] and not insp.get("details_pending"):
                # The state's checklist is officially "a partial list";
                # this visit's citation isn't itemized in it.
                insp["not_itemized"] = True
            entry["inspections"].append(insp)
            nv += 1
        entry["inspections"].sort(key=lambda i: i.get("date") or "", reverse=True)
        entry["last_inspection"] = (entry["inspections"][0]["date"]
                                    if entry["inspections"] else None)
        # Legacy basis preserved: entries crawled from the OLD app carry
        # the STATE'S OWN per-violation statuses and uncorrected total —
        # never overwrite the state's last published word with our
        # derivation (mixing the two taxonomies overstated one test
        # provider 5 -> 10). Derivation applies only where no legacy
        # basis exists (post-Aug-2026 providers).
        has_legacy = bool(entry.get("uncorrected") is not None
                          and entry.get("open_basis") != "derived_v2")
        latest_open = set()
        latest_by_code = {}
        for insp in sorted(entry["inspections"], key=lambda i: i.get("date") or ""):
            for viol in insp.get("violations", []):
                latest_by_code[viol.get("code")] = viol.get("status")
        for code, stat in latest_by_code.items():
            if stat in ("Not Corrected", "Prior Violation - observed again"):
                latest_open.add(code)
        if not has_legacy:
            entry["open_count"] = len(latest_open)
            entry["uncorrected"] = ("None" if not latest_open else
                                    "; ".join(sorted(latest_open)))
            entry["open_basis"] = "derived_v2"   # not a state-published total
        entry.update({k: v for k, v in info.items() if v is not None})
        try:
            entry["complaints"] = _parse_complaints_v2(_get_page(CMP_TMPL.format(fid)))
            time.sleep(delay)
        except Exception:
            pass
        # Self-reported open slots + hours flags (new in the state's app;
        # each carries its own self-reported as-of date for display).
        try:
            entry["open_slots"] = _parse_slots_v2(_get_page(
                "https://hs.ocfs.ny.gov/dcfs/Search/GetCapacityOpenSlots/" + fid))
            time.sleep(delay)
            entry["hours"] = _parse_hours_v2(_get_page(
                "https://hs.ocfs.ny.gov/dcfs/Search/GetScheduleAndRates/" + fid))
            time.sleep(delay)
        except Exception:
            pass
        entry["fetched_at"] = _now()
        return entry, nv

    # Wave-based submission: the main thread owns every cache write and
    # save (workers return finished entries), so a periodic snapshot can
    # never serialize an entry mid-mutation; the time budget is checked
    # between waves, overshooting by at most one wave.
    _t0 = time.monotonic()
    idx = 0
    W = max(1, int(workers or 1))
    degraded_from = None
    errors_pre_degrade = 0

    def _absorb(f2, ok_entry, nv2, exc):
        nonlocal done, errors, new_visits
        if exc is not None:
            errors += 1
            if errors <= 5:
                print(f"  ! {f2}: {exc}")
            return
        cache[f2] = ok_entry
        done += 1
        new_visits += nv2
        if done and done % 100 == 0:
            json.dump(cache, open(PROFILE_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"  ...{done} providers crawled ({new_visits} new visits, "
                  f"{_bud['used']} checklists, {errors} errors)")

    with ThreadPoolExecutor(max_workers=W) as ex:
        while idx < len(fids):
            if time_budget_min is not None and time.monotonic() - _t0 >= time_budget_min * 60:
                print(f"time budget ({time_budget_min:g}m) reached: {done} crawled, "
                      f"{len(fids) - done - errors} left in the queue for the next run")
                break
            wave = fids[idx: idx + W * 2]
            idx += len(wave)
            if W == 1:
                # Inline serial path: never more than one in-flight request.
                for f2 in wave:
                    try:
                        entry2, nv2 = _crawl_one(f2)
                        _absorb(f2, entry2, nv2, None)
                    except Exception as e:
                        _absorb(f2, None, 0, e)
            else:
                futs = {ex.submit(_crawl_one, f): f for f in wave}
                for fut in as_completed(futs):
                    f2 = futs[fut]
                    try:
                        entry2, nv2 = fut.result()
                        _absorb(f2, entry2, nv2, None)
                    except Exception as e:
                        _absorb(f2, None, 0, e)
            # Adaptive degrade: parallel datacenter traffic can be blocked
            # wholesale by the state's WAF (2026-09-11: 4 workers from a
            # GitHub runner went 0 ok / 456 errors while a serial run from
            # the same infrastructure crawled 754 / 0 hours earlier, and 4
            # concurrent from a residential IP all succeeded). If the early
            # waves are almost all errors while parallel, drop to a single
            # worker and judge the health gate on the serial phase alone.
            # If serial ALSO mass-fails, that is a dead source, not a
            # blocked traffic pattern, and the gate below stays fatal.
            if (degraded_from is None and W > 1 and done + errors >= 16
                    and errors > 0.8 * (done + errors)):
                print(f"::warning title=OCFS crawl degrading::{errors}/{done + errors} "
                      f"early errors with {W} workers - dropping to a single worker")
                degraded_from = W
                W = 1
                errors_pre_degrade = errors
                errors = 0
    chk_used = _bud["used"]
    if degraded_from is not None:
        print(f"(degraded from {degraded_from} workers after {errors_pre_degrade} "
              f"parallel-phase errors; serial phase: {done} ok / {errors} errors)")
    json.dump(cache, open(PROFILE_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    total = done + errors
    print(f"v2 crawl: {done} ok / {errors} errors of {total} attempted; "
          f"{new_visits} new visits; {chk_used} checklists fetched.")
    # Crawl-health gate: mass failure must be LOUD (the Aug-12 route move
    # 404ed every fetch for a month in silence).
    if total >= 20 and errors > 0.5 * total:
        print(f"::error title=OCFS crawl failing::{errors}/{total} profile fetches failed — "
              "the OCFS app has likely moved again. Histories are NOT updating.")
        sys.exit(3)
    return done


# ---- Raw-record minting (restored 2026-09-12) ----------------------------
# The crawler-v2 rewrite (PR #97) silently dropped mint_missing_homebased
# and its helpers while replacing the adjacent v1 parser. The registry step
# was not marked required, so every run since printed a NameError and went
# green with stale registry data — exactly the silent-failure class the
# gates exist to prevent. Restored verbatim from cf4dca8c60e; the step is
# now required and a lint test fails on any undefined name.
SRC_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "facilities"))

_BORO_FROM_COUNTY = {"Bronx": "Bronx", "Brooklyn": "Brooklyn", "Kings": "Brooklyn",
                     "Manhattan": "Manhattan", "New York": "Manhattan",
                     "Queens": "Queens", "Richmond": "Staten Island",
                     "Staten Island": "Staten Island"}

_TYPE_MAP = {
    "GFDC": ("grou", "group_home_care", "Group Family Day Care Home"),
    "FDC": ("home", "home_daycare", "Family Day Care Home"),
}

def _slug(text):
    import re as _re2
    t = _re2.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return _re2.sub(r"-{2,}", "-", t) or "x"

def mint_missing_homebased(registry):
    existing = set()
    for fn in os.listdir(SRC_DIR):
        if not (fn.startswith("grou-") or fn.startswith("home-")) or not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SRC_DIR, fn), encoding="utf-8") as fh:
                lic = str(json.load(fh).get("license_number") or "")
            if lic:
                existing.add("".join(c for c in lic if c.isdigit()))
        except Exception:
            continue
    minted, skipped_closed, by_boro = 0, 0, {}
    for rec in registry.values():
        fid = "".join(c for c in str(rec.get("facility_id") or "") if c.isdigit())
        ptype = _TYPE_MAP.get((rec.get("program_type") or "").strip())
        if not fid or not ptype or fid in existing:
            continue
        if (rec.get("status") or "") not in ("License", "Registration"):
            skipped_closed += 1
            continue
        prefix, ftype, flabel = ptype
        boro = _BORO_FROM_COUNTY.get((rec.get("county") or "").strip(), "")
        name, addr = rec.get("name") or "", rec.get("address") or ""
        base = f"{prefix}-{_slug(name)}-{_slug(addr)}"
        path = os.path.join(SRC_DIR, base + ".json")
        if os.path.exists(path):
            base = f"{base}-{fid}"
            path = os.path.join(SRC_DIR, base + ".json")
        mix = rec.get("capacity_mix") or {}
        out = {
            "facility_id": base,
            "facility_slug": f"{_slug(name)}-{_slug(boro)}",
            "facility_name": name,
            "license_number": fid,
            "license_status": rec.get("status"),
            "license_expiration": rec.get("license_expiration_date"),
            "address": addr,
            "borough": boro,
            "neighborhood": "",
            "zipcode": rec.get("zip") or "",
            "facility_type": ftype,
            "facility_type_label": flabel,
            "is_open": True,
            "maximum_capacity": rec.get("total_capacity") or "",
            "capacity_infant": mix.get("infant"),
            "capacity_toddler": mix.get("toddler"),
            "capacity_preschool": mix.get("preschool"),
            "capacity_school_age": mix.get("school_age"),
            "source": "NYS OCFS",
            "phone": rec.get("phone") or "",
            "email": "",
            "website": rec.get("profile_url") or "",
            "age_range": "",
            "inspections": [],
            "latitude": float(rec["latitude"]) if rec.get("latitude") else None,
            "longitude": float(rec["longitude"]) if rec.get("longitude") else None,
            "dcid": None,
            "address_private": False,
            "safety": {"score": None, "label": "No Data", "color": "gray"},
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False)
        minted += 1
        by_boro[boro] = by_boro.get(boro, 0) + 1
    print(f"Minted {minted} new home-based raw records (by borough: {by_boro}); "
          f"{skipped_closed} closed/inactive registry rows skipped; "
          f"{len(existing)} already had records.")

def main():
    if "--profiles" in sys.argv:
        def _argval(name):
            for i, a in enumerate(sys.argv):
                if a == name and i + 1 < len(sys.argv):
                    return sys.argv[i + 1]
                if a.startswith(name + "="):
                    return a.split("=", 1)[1]
            return None
        lim = _argval("--limit")
        rd = _argval("--refresh-days")
        tb = _argval("--time-budget-min")
        wk = _argval("--workers")
        scrape_profiles(limit=int(lim) if lim else None,
                        refresh_days=int(rd) if rd else None,
                        time_budget_min=float(tb) if tb else None,
                        workers=int(wk) if wk else 1)
        return
    if "--offline" in sys.argv:
        if os.path.exists(OUT):
            data = json.load(open(OUT, encoding="utf-8"))
            print(f"sidecar present: {len(data)} OCFS profiles at {OUT}")
        else:
            print("no sidecar yet; run without --offline (needs network)")
        return

    print("Fetching NYC OCFS registry from data.ny.gov cb42-qumz …")
    data = fetch_all()
    if not data:
        print("No rows fetched (offline or API unreachable). Sidecar unchanged.")
        return
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    byprog = {}
    for v in data.values():
        byprog[v["program_type"]] = byprog.get(v["program_type"], 0) + 1
    print(f"Wrote {len(data)} NYC OCFS profiles -> {OUT}")
    print(f"By program type: {byprog}")
    mint_missing_homebased(data)
    print("Next: Phase F2 (fetch_ocfs.py --profiles) scrapes violation history per profile.")


if __name__ == "__main__":
    main()
