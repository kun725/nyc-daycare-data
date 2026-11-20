#!/usr/bin/env python3
"""
NYC Daycare Check — reconcile centers to DOHMH's authoritative active roster and
recover missing dcids.

Problem this fixes: build_data confirms "active" by dcid ONLY, and ~1,085 centers
in our extract carry no dcid — so they can never be scraped for fresh inspections
and are auto-labelled "unconfirmed", even when they ARE on DOHMH's current active
list under a slightly different address/zip. This step matches those centers back
to the roster and writes the recovered dcid into the per-facility source file, so
fetch_inspections.py can then pull their live inspection history.

SAFETY (each rule exists because a violation was OBSERVED in our data):
  * Only records with facility_type == 'childcare_under_6' are eligible. The
    roster covers GCC/SBCC (under-6) ONLY — a SACC or summer-camp record at the
    same name+address is a DIFFERENT DOHMH program with its own inspection
    history, and matching it to the under-6 dcid would attach the wrong record
    (e.g. 'Most Holy Redeemer Catholic Academy' runs both an under-6 program and
    a SACC program at one address).
  * A dcid is assigned ONLY when normalized name AND normalized street address
    match exactly one roster entry. Name-only matching is forbidden: operators
    run multiple locations, and a closed location must not inherit the active
    location's dcid (e.g. 'A House on Beekman' active at 390 Jackson Ave, closed
    at 454 E 148 St).
  * No fuzzy/partial matches. Centers that don't match are left untouched —
    this script NEVER deletes or closes a center.

Runs in capture.py, after the roster fetch and before fetch_inspections.py. It
has to run HERE, in the repo that scrapes: until 2026-11-19 it ran only in the
private build, so a center new to the roster got its stub there, this repo
never saw the file, and its inspections were never fetched (43 listed centers,
some stuck since September).

Run:
  python webapp/build/reconcile_active.py
  python webapp/build/reconcile_active.py --report   # analyse only, write nothing
"""
import argparse
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FAC_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "facilities"))
ROSTER = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "active_roster.json"))


def nname(s):
    """Normalize a program name for matching (drop punctuation + legal suffixes)."""
    s = (s or "").lower()
    s = re.sub(r"\b(inc|llc|corp|ltd|co|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def boro(b):
    return (b or "").strip().upper()


def naddr(a):
    """Normalize a street address: house number + street, no punctuation, common
    street-type words folded. Different LOCATIONS must NOT collapse together."""
    a = (a or "").lower()
    a = re.sub(r"\b(suite|ste|apt|unit|fl|floor|#)\b.*$", "", a)
    a = (a.replace("avenue", "ave").replace("street", "st").replace("boulevard", "blvd")
           .replace("road", "rd").replace("place", "pl").replace("drive", "dr")
           .replace("1st", "1").replace("2nd", "2").replace("3rd", "3")
           .replace("east ", "e ").replace("west ", "w ")
           .replace("north ", "n ").replace("south ", "s "))
    return re.sub(r"[^a-z0-9]", "", a)


def build_roster_index():
    """Index by (normalized-name, normalized-ADDRESS) — recovery requires BOTH the
    name AND the street address to match, so a former/closed location of an
    operator never inherits the active location's dcid."""
    roster = json.load(open(ROSTER, encoding="utf-8"))
    active_dcids = set()
    by_name_addr = {}     # (nname, naddr) -> set(dcid)
    for r in roster:
        dc = r.get("dcid")
        if not dc:
            continue
        active_dcids.add(dc)
        key = (nname(r.get("program_name")), naddr(r.get("address")))
        by_name_addr.setdefault(key, set()).add(dc)
    return roster, active_dcids, by_name_addr


def recover_dcid(fac, by_name_addr):
    """Assign a dcid ONLY when name AND address match a single roster entry."""
    nm = nname(fac.get("facility_name"))
    ad = naddr(fac.get("address"))
    if not nm or not ad:
        return None, None
    hits = by_name_addr.get((nm, ad), set())
    if len(hits) == 1:
        return next(iter(hits)), "name+address"
    return None, None


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s or "unknown"


def add_missing(active_rows, our_dcids, report_only):
    """Create stub records for roster centers our extract doesn't have at all
    (e.g. opened after the original scrape). The stub carries identity + geo from
    the roster; fetch_inspections.py fills the inspection history on its next run
    (no portal_inspections_fetched -> fetched immediately)."""
    created = 0
    for r in active_rows:
        dc = r.get("dcid")
        if not dc or dc in our_dcids:
            continue
        name = str(r.get("program_name") or "").title()
        addr = str(r.get("address") or "").title()
        fid = f"chil-{slugify(name)}-{slugify(addr)}"
        path = os.path.join(FAC_DIR, fid + ".json")
        if os.path.exists(path):  # id collision with a different center — disambiguate
            fid = f"{fid}-{dc.lower()}"
            path = os.path.join(FAC_DIR, fid + ".json")
            if os.path.exists(path):
                continue
        created += 1
        if report_only:
            continue
        stub = {
            "facility_id": fid,
            "facility_slug": f"{slugify(name)}-{slugify(r.get('borough'))}",
            "facility_name": name,
            "facility_type": "childcare_under_6",
            "facility_type_label": "Childcare Center",
            "source": "DOHMH",
            "dcid": dc,
            "dcid_source": "active_roster_add_missing",
            "address": addr,
            "borough": str(r.get("borough") or "").title(),
            "zipcode": str(r.get("zipcode") or ""),
            "phone": str(r.get("phone") or ""),
            "age_range": r.get("age_range") or "",
            "maximum_capacity": str(r.get("capacity") or ""),
            "latitude": float(r["latitude"]) if r.get("latitude") else None,
            "longitude": float(r["longitude"]) if r.get("longitude") else None,
            "is_open": True,
            "inspections": [],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stub, f, ensure_ascii=False)
    return created


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="analyse only, write nothing")
    args = ap.parse_args()

    roster, active_dcids, by_name_addr = build_roster_index()

    paths = [p for p in glob.glob(os.path.join(FAC_DIR, "*.json"))
             if "_hashes" not in os.path.basename(p)]

    total = had = recovered = still = 0
    on_roster = 0
    by_method = {}
    our_dcids = set()   # every dcid claimed by ANY of our DOHMH records
    for p in paths:
        try:
            fac = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if fac.get("source") != "DOHMH":
            continue
        # Roster covers under-6 (GCC/SBCC) programs ONLY — never match SACC or
        # summer-camp records against it (cross-program dcid = wrong history).
        # (A dcid on a SACC/camp record does NOT count as "claimed" either —
        # the under-6 program behind that roster dcid still deserves a stub.)
        if fac.get("facility_type") != "childcare_under_6":
            continue
        if fac.get("dcid"):
            our_dcids.add(fac["dcid"])
        total += 1
        if fac.get("dcid"):
            had += 1
            if fac["dcid"] in active_dcids:
                on_roster += 1
            continue
        dc, method = recover_dcid(fac, by_name_addr)
        if dc:
            recovered += 1
            on_roster += 1
            our_dcids.add(dc)
            by_method[method] = by_method.get(method, 0) + 1
            if not args.report:
                fac["dcid"] = dc
                fac["dcid_source"] = "active_roster_reconcile"
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(fac, f, ensure_ascii=False)
        else:
            still += 1

    # Roster centers we don't have at all (e.g. opened after the original
    # scrape): create stubs so the site gains them and the scraper fills them.
    added = add_missing(roster, our_dcids, args.report)

    print(f"[reconcile] DOHMH under-6 centers: {total}")
    print(f"  already had dcid: {had}")
    print(f"  recovered dcid (high-confidence name+address): {recovered}  {by_method}")
    print(f"  => on DOHMH current active roster: {on_roster}")
    print(f"  not on the roster (closed candidates; left untouched): {still}")
    print(f"  NEW stubs for roster centers we lacked entirely: {added}")
    if args.report:
        print("  [report mode — no files written]")


if __name__ == "__main__":
    main()
