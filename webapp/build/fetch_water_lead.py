#!/usr/bin/env python3
"""
NYC Daycare Check — Article 47 water-lead test status, per center.

Port of the legacy dohmh_water_lead.py. Scrapes the DOHMH Child Care Connect
DETAIL page (a different page from the inspection history):

    https://a816-healthpsi.nyc.gov/ChildCare/detail.action?linkPK={dcid}

for two safety fields that exist in NO open-data export:
    water_lead_test_done  — the center has had drinking water tested for lead
                            (NYC Article 47 requires a test every 3 years)
    water_lead_elevated   — any sample exceeded the 15 ppb action threshold

Self-limiting like fetch_inspections.py: only records whose water_lead_updated
stamp is older than REFRESH_AFTER_DAYS are re-scraped, so the weekly run is a
no-op for fresh records and a staggered refresh otherwise. The underlying data
changes on a 3-year legal cycle — 30 days is comfortably fresh.

Usage:
    python webapp/build/fetch_water_lead.py            # stale records only
    python webapp/build/fetch_water_lead.py --force    # everything with a dcid
    python webapp/build/fetch_water_lead.py --limit 50 # smoke test
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import time
import urllib.request
from html.parser import HTMLParser

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
FAC_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "facilities"))
PORTAL = "https://a816-healthpsi.nyc.gov/ChildCare/detail.action"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NYCDaycareCheck/1.0; +https://nycdaycarecheck.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_DELAY = 0.4
REQUEST_TIMEOUT = 20
REFRESH_AFTER_DAYS = 30
MAX_FAIL_RATIO = 0.5   # abort if more than half of attempts fail (portal outage)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_data(self, data):
        s = data.strip()
        if s:
            self.chunks.append(s)


def parse_water(html):
    """Return (test_done, elevated) as bool/None from the detail-page text."""
    p = _TextParser()
    p.feed(html)
    text = " ".join(p.chunks)

    def find_yn(label):
        i = text.find(label)
        if i == -1:
            return None
        snippet = text[i + len(label): i + len(label) + 80].lower()
        if "yes" in snippet:
            return True
        if "no" in snippet:
            return False
        return None

    return find_yn("Water Lead Test Done"), find_yn("Water Lead Test Elevated")


def fetch_one(dcid):
    """(done, elevated, ok) — ok=False only on transport failure (not 404)."""
    url = f"{PORTAL}?linkPK={dcid}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return (*parse_water(r.read().decode("utf-8", "replace")), True)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None, True    # gone from the portal — legitimate
            if e.code == 429:
                time.sleep(30)
                continue
            if e.code >= 500:
                time.sleep(5)
                continue
            return None, None, False
        except Exception:
            if attempt < 2:
                time.sleep(2)
                continue
            return None, None, False
    return None, None, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-scrape everything with a dcid")
    ap.add_argument("--limit", type=int, default=0, help="cap targets (smoke test)")
    ap.add_argument("--days", type=int, default=REFRESH_AFTER_DAYS)
    args = ap.parse_args()

    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).isoformat()
    targets = []
    for p in sorted(glob.glob(os.path.join(FAC_DIR, "*.json"))):
        if os.path.basename(p).startswith("_"):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("source") != "DOHMH" or not d.get("dcid"):
            continue
        stamp = d.get("water_lead_updated") or ""
        if args.force or not stamp or stamp < cutoff:
            targets.append((p, d))
    if args.limit:
        targets = targets[: args.limit]

    est = len(targets) * (REQUEST_DELAY + 0.4) / 60
    print(f"[water_lead] {len(targets)} centers to refresh (window {args.days}d, ~{est:.0f} min)")
    if not targets:
        print("[water_lead] all fresh — nothing to do")
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    updated = errors = elevated = tested = 0
    for i, (p, d) in enumerate(targets, 1):
        done, elev, ok = fetch_one(d["dcid"])
        if not ok:
            errors += 1
        else:
            d["water_lead_test_done"] = done
            d["water_lead_elevated"] = elev
            d["water_lead_updated"] = now
            json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            updated += 1
            if done:
                tested += 1
            if elev:
                elevated += 1
        if errors > 25 and errors / i > MAX_FAIL_RATIO:
            print(f"[water_lead] ABORT: {errors}/{i} failures — portal likely down; "
                  "existing data left untouched")
            sys.exit(1)
        if i % 100 == 0 or i == len(targets):
            print(f"  [{i:,}/{len(targets):,}] updated={updated} tested={tested} "
                  f"elevated={elevated} errors={errors}")
        time.sleep(REQUEST_DELAY)

    print(f"[water_lead] done — {updated} updated, {tested} tested, "
          f"{elevated} elevated, {errors} errors")


if __name__ == "__main__":
    main()
