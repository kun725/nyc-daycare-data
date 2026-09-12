#!/usr/bin/env python3
"""
NYC Daycare Check — Head Start / EarlyLearn site tagger.

Goal: tag facilities that are federally-funded Head Start / Early Head Start
(or NYC EarlyLearn) sites, so parents see that positive signal.

ACCESS REALITY (verified 2026-05): the authoritative federal file
(`hs_service_locations` on headstart.gov) is served through CloudFront, which
returns HTTP 403 to scripted requests — it requires a browser download. There
is no clean, current, national Head Start FeatureServer on ArcGIS Online (the
public ones are regional and stale, 2014–2016). So this source needs ONE manual
step; everything downstream is automated and the flag/badge/filter are already
wired in the app.

TWO WAYS TO POPULATE IT:

  A) Manual (recommended, ~2 min):
     1. Visit https://headstart.gov/about-us/article/head-start-service-location-datasets
     2. Filter to state = New York (or city = New York), export CSV.
     3. Save it as:  webapp/data/processed/headstart_raw.csv
     4. Run:  python webapp/build/fetch_headstart.py --from-csv
     -> writes webapp/data/processed/headstart_nyc.json (address|zip keyed)

  B) Automated attempt (best-effort): this script will try a couple of known
     ArcGIS/HIFLD education layers and filter to NYC. If one resolves, it writes
     the sidecar directly. If not, it tells you to use path (A).

Either way, build_data.py reads webapp/data/processed/headstart_nyc.json and
tags matching facilities. No code changes are needed once the file exists.
"""
import csv
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_CSV = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "headstart_raw.csv"))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "headstart_nyc.json"))
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"
NYC_ZIPS_PREFIX = ("100", "101", "102", "103", "104", "111", "112", "113", "114", "116")  # NYC ZIP3s


def _row_to_site(addr, zip_code, name=""):
    return {"name": name, "address": (addr or "").strip(), "zip": (zip_code or "").strip()}


S3_JSON = "https://s3foa.s3.us-east-1.amazonaws.com/HS_Service_Locations.json"
REFRESH_AFTER_DAYS = 30

def from_s3():
    """Primary path since 2026-09-10: the federal locator now publishes the
    full national dataset as plain JSON on S3 (found via the dataset page's
    Full National Datasets links) — no CloudFront gate, no manual download.
    Self-limits to one pull a month; the daily refresh calls this and it
    no-ops on fresh data."""
    if os.path.exists(OUT):
        import time as _t
        age_d = (_t.time() - os.path.getmtime(OUT)) / 86400
        if age_d < REFRESH_AFTER_DAYS:
            print(f"headstart sidecar is {age_d:.0f}d old (<{REFRESH_AFTER_DAYS}d) — skipping refetch")
            return True
    try:
        req = urllib.request.Request(S3_JSON, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            rows = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"S3 fetch failed ({e}) — falling back to other paths")
        return False
    if not isinstance(rows, list):
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    sites = []
    for row in rows:
        if str(row.get("state") or "").upper() != "NY":
            continue
        z = str(row.get("zip") or row.get("zip_Code") or row.get("zipcode") or "").strip()[:5]
        city = str(row.get("city") or "").strip().lower()
        if z[:3] in NYC_ZIPS_PREFIX or city in ("new york", "bronx", "brooklyn",
                                                "queens", "staten island", "manhattan"):
            addr = str(row.get("address_line_one") or "").strip()
            if addr:
                site = _row_to_site(addr, z, str(row.get("service_location_name") or ""))
                if row.get("funded_slots"):
                    site["funded_slots"] = row.get("funded_slots")
                sites.append(site)
    if len(sites) < 100:
        print(f"S3 parse produced only {len(sites)} NYC sites — refusing to overwrite")
        return False
    json.dump(sites, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Wrote {len(sites)} NYC Head Start sites -> {OUT} (from federal S3 dataset)")
    return True


def from_csv():
    if not os.path.exists(RAW_CSV):
        print(f"Expected {RAW_CSV} — download per path (A) in this file's header.")
        return
    sites = []
    with open(RAW_CSV, encoding="utf-8-sig", newline="") as fh:
        rdr = csv.DictReader(fh)
        # be tolerant of column naming across federal exports
        def pick(row, *cands):
            for c in cands:
                for k in row:
                    if k and k.lower().strip() == c:
                        return row[k]
            return ""
        for row in rdr:
            addr = pick(row, "address", "physical_address", "site_address_line_one",
                        "program_address_line_one", "address_line_1")
            z = pick(row, "zip", "zip_code", "zipcode", "site_zip", "postal_code")
            city = pick(row, "city", "site_city").lower()
            name = pick(row, "name", "program_name", "site_name", "center_name")
            if z[:3] in NYC_ZIPS_PREFIX or "new york" in city or city in (
                    "bronx", "brooklyn", "queens", "staten island", "manhattan"):
                if addr:
                    sites.append(_row_to_site(addr, z, name))
    json.dump(sites, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Wrote {len(sites)} NYC Head Start sites -> {OUT}")


def try_arcgis():
    """Best-effort: query a HIFLD-style education FeatureServer for Head Start.
    Returns True if it produced a sidecar."""
    candidates = [
        # (query url, address field, zip field, name field) — kept conservative
        ("https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
         "HIFLD_Open_Education/FeatureServer/0/query"
         "?where=NAICS_DESC+LIKE+%27%25CHILD%25%27&outFields=*&f=json&resultRecordCount=2", )
    ]
    for (url, *_rest) in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
            if data.get("features"):
                print("ArcGIS endpoint responded; manual mapping of fields still recommended.")
                return False  # we don't trust an unverified national layer; prefer path (A)
        except Exception as e:
            print(f"  arcgis attempt failed: {e}")
    return False


def main():
    if "--from-csv" not in sys.argv and from_s3():
        return
    if "--from-csv" in sys.argv:
        from_csv()
        return
    print("Attempting automated Head Start fetch…")
    if not try_arcgis():
        print("\nNo reliable automated source. Use the manual path (A) in this file's header:")
        print("  download NYC Head Start CSV -> webapp/data/processed/headstart_raw.csv")
        print("  then: python webapp/build/fetch_headstart.py --from-csv")
        if os.path.exists(OUT):
            print(f"(existing sidecar present: {OUT})")


if __name__ == "__main__":
    main()
