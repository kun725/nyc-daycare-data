#!/usr/bin/env python3
"""
NYC Daycare Check — subway-entrance points for transit-proximity context.

Distance to the nearest subway entrance is a genuine childcare-decision factor
for NYC parents (commute / drop-off). We treat it strictly as neighborhood
amenity context — never a facility judgment.

Source: MTA Subway Entrances and Exits: 2024 (data.ny.gov i9wp-a4ja), current,
free, no key. We cache entrance points WITH station name + routes so the site
can say "0.4 mi to Fulton St (A, C)" instead of a bare number, and we keep only
entrances that allow ENTRY (some are exit-only — pointing a parent at an
exit-only stair would be wrong).

Output: webapp/data/processed/transit_entrances.json
        -> [[lng, lat, "Stop Name", "A C"], ...]
build_data.py computes each facility's nearest-entrance distance (haversine)
and carries the winning entrance's name/routes.
"""
import json
import os
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "transit_entrances.json"))
SRC = "https://data.ny.gov/resource/i9wp-a4ja.json"
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"


def main():
    url = SRC + "?" + urllib.parse.urlencode(
        {"$select": "entrance_latitude,entrance_longitude,stop_name,daytime_routes,entry_allowed",
         "$limit": 50000})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        rows = json.loads(urllib.request.urlopen(req, timeout=40).read())
    except Exception as e:
        print(f"fetch failed: {e}")
        if os.path.exists(OUT):
            print("keeping existing transit_entrances.json")
        return
    pts, skipped_exit_only = [], 0
    for r in rows:
        entry = str(r.get("entry_allowed") or "").strip().upper()
        # Field observed as YES/NO; treat unknown as allowed rather than drop data.
        if entry in ("NO", "N", "FALSE"):
            skipped_exit_only += 1
            continue
        try:
            pts.append([round(float(r["entrance_longitude"]), 6),
                        round(float(r["entrance_latitude"]), 6),
                        (r.get("stop_name") or "").strip()[:60],
                        (r.get("daytime_routes") or "").strip()[:20]])
        except (KeyError, TypeError, ValueError):
            continue
    json.dump(pts, open(OUT, "w", encoding="utf-8"))
    print(f"Wrote {len(pts)} entry-allowed subway entrances (skipped {skipped_exit_only} exit-only) -> {OUT}")


if __name__ == "__main__":
    main()
