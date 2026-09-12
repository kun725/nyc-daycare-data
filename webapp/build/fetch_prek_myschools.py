#!/usr/bin/env python3
"""
NYC Daycare Check — current Pre-K/3-K site directory from DOE MySchools.

The open-data Pre-K directory (kiyv-ks3f) froze 2017-11-29 and DOE publishes no
successor dataset — but MySchools (the DOE's own live parent portal) serves an
unauthenticated public JSON API with the CURRENT school year's sites:

    https://www.myschools.nyc/en/api/v2/schools/process/5/   (Pre-K)
    https://www.myschools.nyc/en/api/v2/schools/process/2/   (3-K)

Paginated (~20 sites/page). We page politely (0.5s delay, UA identified) and
write a sidecar keyed like the prek_dbn sidecar (canon(name)|canon(street)) plus
a DBN index, used at build to (a) mark which of our Pre-K records appear in the
CURRENT directory, (b) refresh contact info, (c) flag records the DOE no longer
lists. This is an undocumented app endpoint: schema drift is expected, so every
extraction is defensive and a failed run keeps the previous sidecar.

Output: webapp/data/processed/prek_myschools.json
    { "fetched": iso, "year": "...", "sites": {"<canon key>": {...}},
      "by_dbn": {"15K001": {...}} }
"""
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "prek_myschools.json"))
BASE = "https://www.myschools.nyc/en/api/v2/schools/process/{}/"
PROCESSES = {"5": "prek", "2": "3k"}
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-directory ingest)"
DELAY = 0.5

_ABBR = {"street": "st", "avenue": "ave", "boulevard": "blvd", "road": "rd",
         "place": "pl", "drive": "dr", "east": "e", "west": "w", "north": "n",
         "south": "s"}


def canon(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    s = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", s)   # 29th -> 29 (street ordinals)
    return " ".join(_ABBR.get(w, w) for w in s.split())


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_page(url, retries=3):
    """One page with retry on transient 5xx (observed: sporadic 500s mid-run)."""
    for attempt in range(retries):
        try:
            return _get(url)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def _parse_site(s, tag):
    """Site objects nest the real record under 'school' (verified live):
    school.dbn, school.name, school.school_year, school.address.address_1,
    school.school_type.name; telephone/email at top level."""
    sc = s.get("school") or {}
    addr = sc.get("address") or {}
    name = (sc.get("name") or s.get("name") or "").strip()
    # names carry a trailing "(DBN)" parenthetical — strip for matching
    name = re.sub(r"\s*\([0-9A-Z]{4,8}\)\s*$", "", name)
    stype = sc.get("school_type") or {}
    # Official seat/demand data from the most recent completed admissions
    # cycle (programs[].demand_last_year — verified live 2026-07-19). This is
    # the CURRENT replacement for the frozen 2015-16 open-data seat counts.
    seats = apps = None
    filled_flags = []
    for p in (s.get("programs") or []):
        if not isinstance(p, dict):
            continue
        dem = p.get("demand_last_year") or {}
        for grp in ("general_education", "students_with_disabilities"):
            g = dem.get(grp) or {}
            try:
                if g.get("seats") is not None:
                    seats = (seats or 0) + int(g["seats"])
                    if g.get("applicants") is not None:
                        apps = (apps or 0) + int(g["applicants"])
                    if g.get("all_seats_filled") is not None:
                        filled_flags.append(bool(g["all_seats_filled"]))
            except (TypeError, ValueError):
                continue
    return {
        "seats": seats,
        "applicants": apps,
        "seats_filled": (all(filled_flags) if filled_flags else None),
        "website": (s.get("independent_website") or "").strip() or None,
        "dbn": (sc.get("dbn") or "").strip() or None,
        "name": name,
        "address": (addr.get("address_1") or "").strip(),
        "phone": (s.get("telephone") or "").strip() or None,
        "email": (s.get("email") or "").strip() or None,
        "lat": addr.get("latitude"), "lng": addr.get("longitude"),
        "district": ((sc.get("district") or {}).get("code")
                     if isinstance(sc.get("district"), dict) else None),
        "school_type": (stype.get("name") if isinstance(stype, dict) else None),
        "year": (sc.get("school_year") or "").replace(" School Year", "").strip() or None,
        # Official DOE per-site flags: whether this admissions process is
        # currently accepting applications / running a waitlist. false→true
        # transitions drive "applications opened" alerts (dated + attributed).
        "admissions_open": bool(s.get("admissions_open")),
        "waitlist_open": bool(s.get("waitlist_open")),
        "process": tag,
    }


def fetch_process(pid, tag):
    """All sites for one admissions process, defensively parsed."""
    sites, page, total = [], 1, None
    while True:
        try:
            data = _get_page(BASE.format(pid) + f"?page={page}")
        except Exception as e:
            print(f"  ! page {page} failed after retries ({e}) — stopping this process")
            break
        batch = data.get("results") if isinstance(data, dict) else None
        if batch is None and isinstance(data, list):
            batch = data
        if not batch:
            break
        if total is None and isinstance(data, dict):
            total = data.get("count")
        for s in batch:
            if isinstance(s, dict):
                sites.append(_parse_site(s, tag))
        got = len(sites)
        if page % 20 == 0:
            print(f"  …{tag}: page {page}, {got} sites")
        if total is not None and got >= total:
            break
        if isinstance(data, dict) and not data.get("next"):
            break
        page += 1
        time.sleep(DELAY)
    ok = total is None or len(sites) >= total
    print(f"  {tag}: {len(sites)} sites{f' (API count {total})' if total is not None else ''}"
          f"{'' if ok else ' — INCOMPLETE'}")
    return sites, ok


# ── Mint raw records for DOE directory sites with no page (2026-09-10) ──────
# The Pre-K tier's raw records descend from the DOE's FROZEN 2015-16 file;
# sites the DOE added since (this pull, refreshed nightly) had no page. Any
# directory site whose canonical address matches NO existing raw record in
# any tier gets a minimal DOE-sourced record; the build's own MySchools join
# then lights it up (current-directory badge, demand, contacts) because the
# name/address key comes from this very file. No cross-source claims are
# made: the page says what the DOE lists, nothing else.
from fetch_signals import canon_addr as _canon
SRC_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "facilities"))
_DISTRICT_BORO = {}
for _d in range(1, 7): _DISTRICT_BORO[_d] = "Manhattan"
for _d in range(7, 13): _DISTRICT_BORO[_d] = "Bronx"
for _d in list(range(13, 24)) + [32]: _DISTRICT_BORO[_d] = "Brooklyn"
for _d in range(24, 31): _DISTRICT_BORO[_d] = "Queens"
_DISTRICT_BORO[31] = "Staten Island"

def _slug2(t):
    import re as _r
    t = _r.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")
    return _r.sub(r"-{2,}", "-", t) or "x"

def mint_missing_prek(sites_by_key):
    existing = set()
    for fn in os.listdir(SRC_DIR):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        try:
            with open(os.path.join(SRC_DIR, fn), encoding="utf-8") as fh:
                existing.add(_canon(json.load(fh).get("address")))
        except Exception:
            continue
    minted, by_boro = 0, {}
    for site in sites_by_key.values():
        addr = site.get("address") or ""
        if not addr or _canon(addr) in existing:
            continue
        name = site.get("name") or ""
        try:
            dist = int(site.get("district") or 0)
        except (TypeError, ValueError):
            dist = 0
        boro = _DISTRICT_BORO.get(dist, "")
        offers = site.get("offers") or []
        age = ("3-4 years" if len(offers) > 1 else
               "3 years" if "3k" in offers else "4 years")
        base = f"cbo_-{_slug2(name)}-{_slug2(addr)}"
        path = os.path.join(SRC_DIR, base + ".json")
        if os.path.exists(path):
            base += "-" + _slug2(str(site.get("dbn") or "x"))
            path = os.path.join(SRC_DIR, base + ".json")
        rec = {
            "facility_id": base,
            "facility_slug": f"{_slug2(name)}-{_slug2(boro)}",
            "facility_name": name.title() if name.isupper() else name,
            "license_number": site.get("dbn") or "",
            "address": addr.title() if addr.isupper() else addr,
            "borough": boro, "neighborhood": "",
            "zipcode": "",
            "facility_type": "cbo_prek",
            "facility_type_label": "Community-Based Pre-K",
            "is_open": True,
            "maximum_capacity": "",
            "source": "DOE Pre-K",
            "phone": site.get("phone") or "",
            "email": site.get("email") or "",
            "website": (site.get("website") or "") if (site.get("website") or "") != "DOE Website" else "",
            "age_range": age,
            "inspections": [],
            "latitude": float(site["lat"]) if site.get("lat") else None,
            "longitude": float(site["lng"]) if site.get("lng") else None,
            "dcid": None,
            "safety": {"score": None, "label": "No Data", "color": "gray"},
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        existing.add(_canon(addr))
        minted += 1
        by_boro[boro or "?"] = by_boro.get(boro or "?", 0) + 1
    print(f"[myschools] minted {minted} raw records for directory sites with no page "
          f"(by borough: {by_boro})")


def main():
    all_sites, complete = [], True
    for pid, tag in PROCESSES.items():
        print(f"[myschools] fetching process {pid} ({tag}) …")
        got, ok = fetch_process(pid, tag)
        all_sites.extend(got)
        complete = complete and ok
    if len(all_sites) < 500:
        print(f"[myschools] only {len(all_sites)} sites — refusing to overwrite "
              "the sidecar with a suspiciously small result")
        if os.path.exists(OUT):
            print("[myschools] keeping existing sidecar")
        sys.exit(0)
    if not complete:
        print("[myschools] NOTE: one process came back incomplete — matching still "
              "works (absence flags are only trustworthy for the complete set); "
              "the next nightly run retries.")

    sites_by_key, by_dbn = {}, {}
    for s in all_sites:
        k = canon(s["name"]) + "|" + canon(s["address"])
        cur = sites_by_key.setdefault(k, {**s, "offers": set(), "open_procs": set()})
        targets = [cur]
        if s.get("dbn"):
            targets.append(by_dbn.setdefault(s["dbn"], {**s, "offers": set(), "open_procs": set()}))
        for m in targets:
            m["offers"].add(s["process"])
            if s.get("admissions_open") or s.get("waitlist_open"):
                m["open_procs"].add(s["process"])
            for f in ("phone", "email", "dbn", "website"):
                if not m.get(f) and s.get(f):
                    m[f] = s[f]
            # Seat/demand data is per admissions process — keep the split.
            if s.get("seats") is not None:
                m.setdefault("demand", {})[s["process"]] = {
                    "seats": s["seats"], "applicants": s.get("applicants"),
                    "filled": s.get("seats_filled")}
    for m in list(sites_by_key.values()) + list(by_dbn.values()):
        m["offers"] = sorted(m["offers"])
        m["open_procs"] = sorted(m.get("open_procs") or [])
        for scalar in ("process", "seats", "applicants", "seats_filled"):
            m.pop(scalar, None)

    import datetime as dt
    json.dump({"fetched": dt.datetime.now(dt.timezone.utc).isoformat(),
               "complete": complete,
               "year": next((s.get("year") for s in all_sites if s.get("year")), None),
               "sites": sites_by_key, "by_dbn": by_dbn},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[myschools] wrote {len(sites_by_key)} unique sites "
          f"({len(by_dbn)} with DBN) -> {OUT}")
    mint_missing_prek(sites_by_key)


if __name__ == "__main__":
    main()
