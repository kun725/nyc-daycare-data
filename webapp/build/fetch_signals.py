#!/usr/bin/env python3
"""
NYC Daycare Check — building & provider signals fetcher (sidecar).

Adds four verified-current source families on top of fetch_enrichment.py's
HPD/DOB-violations/rodent pull:

  * 311 Service Requests            (erm2-nwe9, daily)   — by BBL
      - building complaints, last 36 months (count + most recent w/ type)
      - complaint_type='Day Care': complaints filed with 311 ABOUT childcare
        at this location (incl. the 'Unlicensed Day Care' descriptor), 2020+
  * DOB NOW approved permits        (rbx6-tga4, daily)   — by BIN
      - issued, unexpired MAJOR work (general construction / structural /
        foundation / earthwork / excavation / demolition) = active
        construction authorized at the building
  * DOB complaint dispositions      (eabe-havv, daily)   — by BIN
      - disposition A3 (full) / L1 (partial) = stop-work order served.
        The city's only public SWO signal; rescissions can land under a
        different complaint number, so display copy must say "verify current
        status with DOB".
  * FDNY Building Vacate List       (n5xc-7jfa, ~annual) — by BIN/BBL
      - vacate orders, excluding rescinded/dismissed rows
  * CACFP participation             (dmn7-mpa8, health.data.ny.gov, quarterly)
      - federal food-program participation (+ Breastfeeding-Friendly). The
        state list omits street addresses, so the join is normalized
        site name + borough, accepted only when unambiguous on both sides.

Output sidecar (consumed by build_data.py):
  webapp/data/processed/signals.json
    { "fetched": "YYYY-MM-DD",
      "sources": { "<source>": "ok (<n> rows)" | "error: ..." },
      "building": { "bbl:<bbl>": {...}, "bin:<bin>": {...} },
      "cacfp": { "<canon-street>|<zip5>": {"bff": bool} } }

Each source is independent: a failure records an error status and leaves that
signal absent instead of killing the run (the build then simply shows nothing
for it — never a wrong claim).

Run:
  python webapp/build/fetch_signals.py
"""
import datetime as dt
import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_FAC = os.path.normpath(os.path.join(HERE, "..", "data", "build", "facilities"))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "signals.json"))
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"
TOKEN = os.environ.get("SOCRATA_APP_TOKEN")

C311 = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
DOBNOW = "https://data.cityofnewyork.us/resource/rbx6-tga4.json"
DOBCOMP = "https://data.cityofnewyork.us/resource/eabe-havv.json"
VACATE = "https://data.cityofnewyork.us/resource/n5xc-7jfa.json"
CACFP = "https://health.data.ny.gov/resource/dmn7-mpa8.json"

MAJOR_WORK = ("general construction", "structural", "foundation", "earthwork",
              "support of excavation", "full demolition", "demolition")

# Mirrors canonQ() in app.js — both sides of any address join must agree.
ADDR_TOKEN = {
    "east": "e", "west": "w", "north": "n", "south": "s",
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "place": "pl", "road": "rd", "drive": "dr", "court": "ct",
    "parkway": "pkwy", "lane": "ln", "terrace": "ter", "square": "sq",
    "heights": "hts",
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12",
}


def canon_addr(s):
    s = re.sub(r"[.,'’#\-]", " ", str(s or "").lower())
    s = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", s)
    return " ".join(ADDR_TOKEN.get(t, t) for t in s.split())


def _get(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA}
    if TOKEN:
        headers["X-App-Token"] = TOKEN
    req = urllib.request.Request(full, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _in_list(vals):
    return ",".join("'" + str(v).replace("'", "") + "'" for v in vals)


def _parse_date(s):
    """ISO ('2026-01-05T00:00:00') or US text ('01/05/2026') -> 'YYYY-MM-DD'."""
    s = str(s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def collect_keys():
    """Distinct BBLs / BINs / (canon street|zip) across built center facilities."""
    bbls, bins_ = set(), set()
    for fp in glob.glob(os.path.join(BUILD_FAC, "*.json")):
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        if d.get("tier") != "center":
            continue
        if d.get("bbl"):
            bbls.add(str(d["bbl"]).strip())
        if d.get("bin"):
            bins_.add(str(d["bin"]).strip())
    return sorted(bbls), sorted(bins_)


def bucket(building, key):
    return building.setdefault(key, {})


# ---------------------------------------------------------------------------
def fetch_311(building, bbls):
    """Building complaints (36 mo) + 'Day Care' complaints (2020+), by BBL."""
    floor = (dt.date.today() - dt.timedelta(days=36 * 30)).isoformat()
    rows_seen = 0
    per_bbl, dc_per_bbl = {}, {}
    for batch in _chunks(bbls, 150):
        where = f"created_date>='{floor}T00:00:00' AND bbl in({_in_list(batch)})"
        data = _get(C311, {"$select": "bbl,complaint_type,created_date",
                           "$where": where, "$limit": 100000})
        rows_seen += len(data)
        for r in data:
            b = str(r.get("bbl") or "")
            if not b:
                continue
            per_bbl.setdefault(b, []).append(
                (r.get("created_date") or "", r.get("complaint_type") or ""))
        # Day Care complaints: no date floor (dataset starts 2020). Fetch the
        # city's own outcome fields too (status/resolution) — the report tells
        # parents to ask how a complaint was resolved, so show what the city
        # says happened rather than a bare count. (Only for this small category,
        # ~4k rows citywide; the building-wide pull stays lean.)
        dc = _get(C311, {"$select": "bbl,descriptor,created_date,status,resolution_description,closed_date",
                         "$where": f"complaint_type='Day Care' AND bbl in({_in_list(batch)})",
                         "$limit": 50000})
        for r in dc:
            b = str(r.get("bbl") or "")
            if not b:
                continue
            dc_per_bbl.setdefault(b, []).append((
                r.get("created_date") or "", r.get("descriptor") or "",
                r.get("status") or "", (r.get("resolution_description") or "")[:300],
                (r.get("closed_date") or "")[:10]))
    for b, rows in per_bbl.items():
        rows.sort(reverse=True)
        e = bucket(building, "bbl:" + b)
        e["c311_count"] = len(rows)
        e["c311_recent"] = [{"date": (d or "")[:10], "complaint_type": t}
                            for d, t in rows[:3]]
        # Full-set breakdown by complaint type (top 6), so the report can show
        # WHAT the complaints are (noise/heat/parking…) instead of a bare count.
        types = Counter(t for _, t in rows if t)
        e["c311_types"] = [{"type": k, "n": n} for k, n in types.most_common(6)]
    for b, rows in dc_per_bbl.items():
        rows.sort(reverse=True)
        e = bucket(building, "bbl:" + b)
        e["dc311_count"] = len(rows)
        e["dc311_recent"] = [{"date": (d or "")[:10], "descriptor": t,
                              "status": s or None, "resolution": res or None,
                              "closed": c or None}
                             for d, t, s, res, c in rows[:3]]
        dtypes = Counter(t for _, t, _s, _r, _c in rows if t)
        e["dc311_types"] = [{"type": k, "n": n} for k, n in dtypes.most_common(6)]
    return f"ok ({rows_seen} building rows, {sum(len(v) for v in dc_per_bbl.values())} Day Care rows)"


def fetch_dob_permits(building, bins_):
    """Active (issued, unexpired) MAJOR-work DOB NOW permits, by BIN."""
    today = dt.date.today().isoformat()
    per_bin = {}
    for batch in _chunks(bins_, 200):
        where = (f"permit_status='Permit Issued' AND expired_date>='{today}T00:00:00' "
                 f"AND bin in({_in_list(batch)})")
        # job_description / estimated_job_costs / work_on_floor tell a parent the
        # SCALE and PLACE of the work — "$2.4M facade job on floors 1-3" reads
        # very differently from a boiler swap.
        data = _get(DOBNOW, {"$select": "bin,work_type,job_description,estimated_job_costs,work_on_floor,issued_date",
                             "$where": where, "$limit": 50000})
        for r in data:
            wt = (r.get("work_type") or "").strip()
            if not any(m in wt.lower() for m in MAJOR_WORK):
                continue
            b = str(r.get("bin") or "")
            slot = per_bin.setdefault(b, {"types": set(), "jobs": []})
            slot["types"].add(wt)
            if len(slot["jobs"]) < 3:
                cost = r.get("estimated_job_costs")
                try:
                    cost = int(float(cost)) if cost not in (None, "") else None
                except (TypeError, ValueError):
                    cost = None
                slot["jobs"].append({
                    "type": wt,
                    "desc": (r.get("job_description") or "").strip()[:200] or None,
                    "cost": cost,
                    "floor": (r.get("work_on_floor") or "").strip()[:40] or None,
                    "issued": (r.get("issued_date") or "")[:10] or None,
                })
    n = 0
    for b, slot in per_bin.items():
        if not b:
            continue
        e = bucket(building, "bin:" + b)
        e["dob_permits"] = len(slot["types"])
        e["dob_permit_types"] = sorted(slot["types"])
        e["dob_permit_jobs"] = slot["jobs"]
        n += 1
    return f"ok ({n} buildings with active major work)"


def fetch_swo(building, bins_):
    """Stop-work-order dispositions (A3 full / L1 partial) in the last 12 months."""
    cutoff = dt.date.today() - dt.timedelta(days=365)
    per_bin = {}
    for batch in _chunks(bins_, 200):
        where = f"disposition_code in('A3','L1') AND bin in({_in_list(batch)})"
        data = _get(DOBCOMP, {"$select": "bin,disposition_code,disposition_date",
                              "$where": where, "$limit": 50000})
        for r in data:
            d = _parse_date(r.get("disposition_date"))
            if not d or dt.date.fromisoformat(d) < cutoff:
                continue
            b = str(r.get("bin") or "")
            cur = per_bin.get(b)
            if not cur or d > cur[0]:
                per_bin[b] = (d, r.get("disposition_code") == "L1")
    for b, (d, partial) in per_bin.items():
        if not b:
            continue
        e = bucket(building, "bin:" + b)
        e["swo_date"] = d
        e["swo_partial"] = partial
    return f"ok ({len(per_bin)} buildings with a stop-work disposition in 12 mo)"


def fetch_vacate(building, bins_, bbls):
    """FDNY vacate orders. Verified schema: `description` holds the disposition
    — 'Rescinded' / 'Dismissal', or EMPTY for orders still in force — and
    `vac_date` is the vacate date. Only in-force orders are flagged: showing a
    dismissed vacate order as active would be a false adverse claim."""
    rows, offset = [], 0
    while True:
        page = _get(VACATE, {"$limit": 50000, "$offset": offset})
        rows.extend(page)
        if len(page) < 50000:
            break
        offset += 50000
    bins_set, bbls_set = set(bins_), set(bbls)
    hits = 0
    for r in rows:
        if str(r.get("description") or "").strip():
            continue                      # Rescinded / Dismissal — not in force
        rb = str(r.get("bin") or "").strip()
        rl = str(r.get("bbl") or "").strip()
        key = None
        if rb and rb in bins_set:
            key = "bin:" + rb
        elif rl and rl in bbls_set:
            key = "bbl:" + rl
        if not key:
            continue
        e = bucket(building, key)
        e["vacate_date"] = _parse_date(r.get("vac_date"))
        e["vacate_status"] = "in force per the FDNY vacate list"
        hits += 1
    return f"ok ({len(rows)} rows citywide, {hits} IN-FORCE orders matched our buildings)"


def fetch_cacfp():
    """CACFP participants in the five boroughs. The state list OMITS street
    addresses (address_omitted=YES) and uses borough names as counties, so the
    join key is canon(site_name)|BOROUGH. Names appearing more than once in a
    borough are marked ambiguous — the build skips them rather than guess."""
    rows = _get(CACFP, {
        "$where": "county in('BRONX','BROOKLYN','MANHATTAN','QUEENS','STATEN ISLAND')",
        "$limit": 50000})
    out = {}
    for r in rows:
        name, boro = r.get("site_name"), str(r.get("county") or "").strip().upper()
        if not name or not boro:
            continue
        k = canon_addr(name) + "|" + boro
        bff = str(r.get("breastfeeding_friendly_certified") or "").strip().upper() == "YES"
        # Eat Well Play Hard: NYS nutrition/physical-activity program — a second
        # positive signal from the same rows (either the child-care-services or
        # day-care-homes variant).
        ewph = (str(r.get("eat_well_play_hard_ccs_participant") or "").strip().upper() == "YES"
                or str(r.get("eat_well_play_hard_dch_participant") or "").strip().upper() == "YES")
        if k in out:
            out[k]["ambiguous"] = True
            out[k]["bff"] = out[k]["bff"] or bff
            out[k]["ewph"] = out[k].get("ewph") or ewph
        else:
            out[k] = {"bff": bff, "ewph": ewph}
    return out, f"ok ({len(rows)} NYC rows, {len(out)} name keys)"


# ---------------------------------------------------------------------------
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    bbls, bins_ = collect_keys()
    print(f"== fetch_signals: {len(bbls)} BBLs, {len(bins_)} BINs ==")
    if not bbls and not bins_:
        print("No geo keys found — run build_data.py (pass 1) first.")
        sys.exit(1)

    building, sources, cacfp = {}, {}, {}
    steps = [
        ("c311", lambda: fetch_311(building, bbls)),
        ("dob_permits", lambda: fetch_dob_permits(building, bins_)),
        ("swo", lambda: fetch_swo(building, bins_)),
        ("vacate", lambda: fetch_vacate(building, bins_, bbls)),
    ]
    for name, fn in steps:
        try:
            sources[name] = fn()
        except Exception as e:
            sources[name] = f"error: {e}"
        print(f"  {name}: {sources[name]}")
    try:
        cacfp, sources["cacfp"] = fetch_cacfp()
    except Exception as e:
        sources["cacfp"] = f"error: {e}"
    print(f"  cacfp: {sources['cacfp']}")

    out = {
        "fetched": dt.date.today().isoformat(),
        "sources": sources,
        "building": building,
        "cacfp": cacfp,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT} ({len(building)} building keys, {len(cacfp)} CACFP keys)")
    errors = [k for k, v in sources.items() if str(v).startswith("error")]
    if errors:
        print(f"WARNING: sources failed: {', '.join(errors)} — their signals are absent, not wrong.")


if __name__ == "__main__":
    main()
