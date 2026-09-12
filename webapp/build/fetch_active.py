#!/usr/bin/env python3
"""
NYC Daycare Check — fetch the daily 'Active NYC Health Code Regulated Child Care
Programs' roster (NYC Open Data gy3q-4tzp) via the Socrata SODA API.

This is the authoritative list of currently-active DOHMH childcare programs.
build_data.py cross-references it to flag centers that are confirmed active vs.
those lingering in the inspection data after closing/lapsing.

Free, no key required (app token only raises rate limits). Updated daily.

Output: webapp/data/processed/active_roster.json (raw rows)
"""
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "active_roster.json"))
ENDPOINT = "https://data.cityofnewyork.us/resource/gy3q-4tzp.json"
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")


def main():
    rows, offset = [], 0
    while True:
        url = ENDPOINT + "?" + urllib.parse.urlencode({"$limit": 5000, "$offset": offset})
        headers = {"User-Agent": UA}
        if APP_TOKEN:
            headers["X-App-Token"] = APP_TOKEN
        try:
            req = urllib.request.Request(url, headers=headers)
            batch = json.loads(urllib.request.urlopen(req, timeout=40).read())
        except Exception as e:
            print(f"fetch failed at offset {offset}: {e}", file=sys.stderr)
            if rows:
                break          # keep what we have
            # nothing fetched and a sidecar already exists -> keep it, exit ok
            if os.path.exists(OUT):
                print("using existing active_roster.json (network unavailable)")
                return
            sys.exit(1)
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < 5000:
            break
    json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    dcids = sum(1 for r in rows if r.get("dcid"))
    print(f"Active roster: {len(rows)} programs ({dcids} with dcid) -> {OUT}")


if __name__ == "__main__":
    main()
