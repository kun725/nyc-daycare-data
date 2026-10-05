#!/usr/bin/env python3
"""
NYC Daycare Check — rooftop geocoder via NYC Planning Labs GeoSearch.

A third of DOHMH centers arrive with no coordinates and fall back to a ZIP
centroid (every center in a ZIP stacks on one pin — useless for "near me" /
radius). This script geocodes those addresses to rooftop precision using
GeoSearch (https://geosearch.planninglabs.nyc), the City's own Pelias geocoder
built on the authoritative Property Address Directory. It is FREE, needs no API
key, and is NYC-authoritative.

Output sidecar (consumed by build_data.py, which prefers it over ZIP centroid):
    webapp/data/processed/geocode.json
      { "<facility_id>": {lat, lng, label, confidence} }

Idempotent + cached: re-runs only geocode facilities not already in the sidecar.
Polite: ~5 req/s with a short sleep.

Usage:
    python webapp/build/fetch_geocode.py            # geocode all missing
    python webapp/build/fetch_geocode.py --limit=50 # test on a few
"""
import glob
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FAC = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "facilities"))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "geocode.json"))

ENDPOINT = "https://geosearch.planninglabs.nyc/v2/search"
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"
MIN_CONFIDENCE = 0.5


def geocode(text):
    url = ENDPOINT + "?" + urllib.parse.urlencode({"text": text, "size": 1})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    feats = data.get("features") or []
    if not feats:
        return None
    f = feats[0]
    lng, lat = f["geometry"]["coordinates"]
    props = f.get("properties", {})
    conf = props.get("confidence", 0)
    if conf < MIN_CONFIDENCE:
        return None
    out = {"lat": round(lat, 6), "lng": round(lng, 6),
           "label": props.get("label", ""), "confidence": conf,
           "accuracy": props.get("accuracy") or ""}
    # GeoSearch is built on the city's Property Address Directory, so an exact
    # point match carries the official building keys — the same BBL/BIN the
    # roster provides for centers. Capturing them unlocks building-level joins
    # (HPD/DOB/rodent) for records the roster can't key (home-based).
    pad = (props.get("addendum") or {}).get("pad") or {}
    if pad.get("bbl"):
        out["bbl"] = str(pad["bbl"]).strip()
    if pad.get("bin"):
        out["bin"] = str(pad["bin"]).strip()
    return out


def main():
    limit = None
    for a in sys.argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])

    cache = {}
    if os.path.exists(OUT):
        cache = json.load(open(OUT, encoding="utf-8"))

    # Collect facilities that need geocoding:
    #   - any record with no source coords (rooftop pin), as before
    #   - home-based records not yet in the sidecar even WITH coords — we want
    #     their PAD BBL/BIN (the registry gives coordinates but no building
    #     key, so these records had no HPD/DOB/rodent joins at all)
    todo = []
    for fp in glob.glob(os.path.join(FAC, "*.json")):
        if os.path.basename(fp).startswith("_"):
            continue
        d = json.load(open(fp, encoding="utf-8"))
        if d.get("source") == "NYC Parks (DPR)":
            continue
        fid = d.get("facility_id") or d.get("id")
        if not fid or fid in cache:
            continue
        wants_bbl = d.get("source") == "NYS OCFS"
        if d.get("latitude") and d.get("longitude") and not wants_bbl:
            continue
        addr, z = (d.get("address") or "").strip(), (d.get("zipcode") or "").strip()
        boro = (d.get("borough") or "").strip()
        if addr and (z or boro):
            todo.append((fid, f"{addr}, {boro} {z}".strip()))

    if limit:
        todo = todo[:limit]
    print(f"Geocoding {len(todo)} facilities via NYC GeoSearch (cached: {len(cache)})…")

    ok = fail = 0
    for i, (fid, text) in enumerate(todo, 1):
        try:
            res = geocode(text)
        except Exception as e:
            res = None
            print(f"  ! {fid}: {e}")
        if res:
            cache[fid] = res
            ok += 1
        else:
            fail += 1
        if i % 200 == 0:
            json.dump(cache, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
            print(f"  …{i}/{len(todo)} (resolved {ok}, unresolved {fail})")
        time.sleep(0.2)  # ~5 req/s, polite

    json.dump(cache, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Done. Resolved {ok}, unresolved {fail}. Sidecar -> {OUT} ({len(cache)} total)")


if __name__ == "__main__":
    main()
