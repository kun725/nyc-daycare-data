#!/usr/bin/env python3
"""
NYC Daycare Check — DOHMH per-facility INSPECTION refresh (the piece the daily
cron was missing).

The DOHMH inspection history is the core of the product, but it does NOT come
from a bulk feed: the NYC Open Data dataset (dsg6-ifza) is frozen at 2019. The
current data lives on DOHMH's live Child Care Connect portal, read per-facility:

  https://a816-healthpsi.nyc.gov/ChildCare/childCareViolations?linkPK={dcid}&type=inspectionHistory

This script re-scrapes that portal for each licensed center (by its `dcid`) and
writes the fresh inspection history back into the per-facility source files that
build_data.py reads (webapp/data/processed/facilities/*.json). Ported from the
original scrapers/dohmh_portal_inspections.py, adapted to this repo's per-file
layout and stdlib-only (urllib) so it runs in CI with no extra deps.

Politeness: 0.35s/request, retries, 429/5xx backoff. ~2,300 centers → ~15 min.
Only re-fetches facilities whose portal data is older than REFRESH_AFTER_DAYS
(7), so a WEEKLY run covers everything without hammering the server.

Run:
  python webapp/build/fetch_inspections.py            # refresh stale (>7d) centers
  python webapp/build/fetch_inspections.py --force    # re-fetch all
  python webapp/build/fetch_inspections.py --limit 5  # smoke test
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
FAC_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "facilities"))

PORTAL = "https://a816-healthpsi.nyc.gov/ChildCare/childCareViolations"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NYCDaycareCheck/1.0; +https://nycdaycarecheck.com)",
    "Accept": "text/html,application/xhtml+xml",
    "X-Requested-With": "XMLHttpRequest",
}
REQUEST_DELAY = 0.35
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
# How stale a center's portal data must be before we re-fetch it. Both the
# daily refresh (refresh.py) and the weekly workflow call this scraper against
# this same window, so whichever runs first once the window opens performs the
# full sweep and the other correctly finds 0 due — that is normal, not a fault.
# Kept below 7 so the end-to-end ingestion lag stays comfortably inside the
# one-week freshness bar rather than landing exactly on it.
REFRESH_AFTER_DAYS = 5

# DOHMH Article 47 tiers → our severity, matching the original scraper.
CATEGORY_SEVERITY = {
    "public health hazard": "Critical",
    "violations requiring immediate correction": "Critical",
    "critical violation": "Serious",
    "critical": "Serious",
    "general violation": "Moderate",
    "minor violation": "Moderate",
    "general": "Moderate",
}
SEVERITY_ORDER = {"Critical": 4, "Serious": 3, "Moderate": 2, "Minor": 1, "": 0}


def now_iso():
    return datetime.now(tz=timezone.utc).isoformat()


def gh_output(**kv):
    """Expose counts to the workflow (GITHUB_OUTPUT) so the weekly email can
    report what was actually scraped — and warn on a silent 0-center no-op."""
    gh = os.environ.get("GITHUB_OUTPUT")
    if not gh:
        return
    with open(gh, "a", encoding="utf-8") as fh:
        for k, v in kv.items():
            fh.write(f"{k}={v}\n")


def parse_date(raw):
    raw = (raw or "").strip()
    try:
        return datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return raw


def map_severity(category_raw):
    low = (category_raw or "").lower()
    for key, sev in CATEGORY_SEVERITY.items():
        if key in low:
            return sev
    return ""


def fetch_rows(dcid):
    """Fetch + parse the portal inspection table for one dcid. None on hard error."""
    url = PORTAL + "?" + urllib.parse.urlencode({"linkPK": dcid, "type": "inspectionHistory"})
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                html = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            if e.code == 429:
                time.sleep(30); continue
            if e.code >= 500:
                time.sleep(5); continue
            if attempt < MAX_RETRIES:
                time.sleep(2)
            continue
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(2)
            continue

        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)]
            if len(cells) >= 2 and cells[1]:  # cells[1] = date
                rows.append({
                    "description": cells[0] if cells else "",
                    "date_raw": cells[1] if len(cells) > 1 else "",
                    "category": cells[2] if len(cells) > 2 else "",
                    "health_code": cells[3] if len(cells) > 3 else "",
                    "status": cells[4] if len(cells) > 4 else "",
                })
        return rows
    return None


HISTORY = "https://a816-healthpsi.nyc.gov/ChildCare/History.do"


def fetch_types(dcid):
    """Per-visit inspection types from the portal's History.do view.

    The violations table carries no visit type; History.do lists each visit as
    a header ("... INSPECTION DATE: 2/20/26") paired (in document order) with a
    hidden summary div whose first segment is the type — e.g.
    "Initial Annual Inspection - Reinspection Required; Fines pending".
    Both halves are captured: the type, and the outcome tail VERBATIM (it is
    DOHMH's own summary wording, rendered attributed and never paraphrased —
    approved as a product decision 2026-09-05).

    Returns {iso_date: {"type", "outcome"}}. Empty dict on any failure — the
    fields are nice-to-haves and must never fail a refresh.
    """
    url = HISTORY + "?" + urllib.parse.urlencode({"linkPK": dcid})
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return {}
    dates = re.findall(r"DATE:\s*([0-9/]+)\s*</A>", html, re.IGNORECASE)
    summaries = re.findall(r"SUMMARY:</A><br\s*/?>\s*([^<]+)", html, re.IGNORECASE)
    # Pairing is by document order; if the counts diverge the page's shape
    # changed and a zip would silently mislabel visits — return nothing.
    if len(dates) != len(summaries):
        return {}
    out = {}
    for raw_date, summary in zip(dates, summaries):
        parts = raw_date.split("/")
        if len(parts) != 3:
            continue
        m, d, y = parts
        if len(y) == 2:
            y = "20" + y
        try:
            iso = "%04d-%02d-%02d" % (int(y), int(m), int(d))
        except ValueError:
            continue
        vtype = summary.split(" - ")[0].strip()
        # The tail after the type is DOHMH's own outcome wording for the visit
        # ("Passed inspection with no violations", "Reinspection Required;
        # Fines pending"). Kept VERBATIM — it is the agency's summary, shown
        # attributed and never paraphrased.
        tail = summary.split(" - ", 1)[1].strip() if " - " in summary else ""
        # Sanity: a type is a short phrase, not a paragraph of summary text.
        if vtype and len(vtype) <= 60:
            out.setdefault(iso, {"type": vtype, "outcome": tail[:200]})
    return out


def group_into_inspections(rows, types=None):
    """Collapse violation rows into per-date inspection records (newest first)."""
    by_date = {}
    for row in rows:
        by_date.setdefault(parse_date(row["date_raw"]), []).append(row)

    inspections = []
    for date_str in sorted(by_date.keys(), reverse=True):
        violations, worst_sev, worst_ord, action_taken = [], "", 0, ""
        for row in by_date[date_str]:
            desc = row["description"].strip()
            if "no new violations" in desc.lower() or not desc:
                continue
            sev = map_severity(row["category"])
            violations.append({
                "description": desc, "severity": sev,
                "health_code": row["health_code"], "status": row["status"],
            })
            if SEVERITY_ORDER.get(sev, 0) > worst_ord:
                worst_sev, worst_ord, action_taken = sev, SEVERITY_ORDER[sev], row["health_code"]

        if not violations:
            result = "No Violations"
        elif worst_sev in ("Critical", "Serious"):
            result = "Violation(s) cited — Critical"
        elif worst_sev:
            result = "Violation(s) cited — General"
        else:
            result = "Violation(s) cited"

        rec = {
            "inspection_date": date_str,
            "result": result,
            "severity": worst_sev,
            "violation_description": violations[0]["description"] if violations else "",
            "action_taken": action_taken,
            "violation_status": violations[0]["status"] if violations else "",
            "violations": violations,
            "source": "DOHMH_portal",
        }
        t = (types or {}).get(date_str)
        if t:
            if t.get("type"):
                rec["inspection_type"] = t["type"]
            if t.get("outcome"):
                rec["inspection_outcome"] = t["outcome"]
        inspections.append(rec)
    return inspections


def needs_refresh(fac, force, cutoff_iso):
    if fac.get("source") != "DOHMH" or not fac.get("dcid"):
        return False
    if force:
        return True
    fetched = fac.get("portal_inspections_fetched", "")
    return not fetched or fetched < cutoff_iso


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch all, ignore age")
    ap.add_argument("--days", type=int, default=REFRESH_AFTER_DAYS)
    ap.add_argument("--limit", type=int, default=0, help="cap facilities (smoke test)")
    args = ap.parse_args()

    cutoff_iso = (datetime.now(tz=timezone.utc) - timedelta(days=args.days)).isoformat()
    paths = sorted(p for p in glob.glob(os.path.join(FAC_DIR, "*.json"))
                   if "_hashes" not in os.path.basename(p))

    targets = []
    for p in paths:
        try:
            fac = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if needs_refresh(fac, args.force, cutoff_iso):
            targets.append((p, fac))
    if args.limit:
        targets = targets[:args.limit]

    print(f"[inspections] {len(targets)} centers to refresh "
          f"(window {args.days}d, ~{len(targets) * REQUEST_DELAY / 60:.0f} min)", flush=True)
    if not targets:
        print("  -> nothing stale; all fresh within the window.")
        gh_output(targets=0, updated=0, errors=0)
        return

    updated = errors = 0
    for i, (path, fac) in enumerate(targets, 1):
        rows = fetch_rows(fac["dcid"])
        if rows is None:
            errors += 1
        else:
            # Second, lighter request for the visit types; same pacing between
            # the two so the sweep stays polite (it doubles requests, not rate).
            time.sleep(REQUEST_DELAY)
            types = fetch_types(fac["dcid"])
            insp = group_into_inspections(rows, types)
            fac["inspections"] = insp
            fac["inspection_count"] = len(insp)
            fac["violation_count"] = sum(1 for x in insp if x.get("severity"))
            fac["critical_violation_count"] = sum(1 for x in insp if x.get("severity") == "Critical")
            fac["last_inspection_date"] = insp[0]["inspection_date"] if insp else ""
            fac["portal_inspections_fetched"] = now_iso()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(fac, f, ensure_ascii=False)
            updated += 1
        if i % 100 == 0 or i == len(targets):
            print(f"  [{i:,}/{len(targets):,}] updated={updated} errors={errors}", flush=True)
        time.sleep(REQUEST_DELAY)

    print(f"[inspections] done — {updated:,} updated, {errors:,} errors")
    gh_output(targets=len(targets), updated=updated, errors=errors)
    # A high error rate means the portal changed or is down; fail loudly so the
    # weekly workflow's failure-alert fires rather than silently committing nothing.
    if targets and errors > len(targets) * 0.5:
        print("ERROR: majority of portal fetches failed — aborting.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
