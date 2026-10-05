#!/usr/bin/env python3
"""Capture orchestrator for nyc-daycare-data.

Runs every fetcher in dependency order and stops loudly if a required one
fails. This is the whole job of this repository: pull what the government
publishes, write it under webapp/data/processed/ with capture dates, and
let the scheduled workflow commit the result. Site building happens in a
separate private repository that pulls this one.

The OCFS profile crawl is shaped by environment (see fetch_ocfs.py):
  OCFS_CRAWL_WORKERS    default 1  (GitHub runners are datacenter IPs; the
                                    state's WAF only allows parallel
                                    crawling from residential networks)
  OCFS_TIME_BUDGET_MIN  default 300 (the job self-stops with time to
                                     commit; GitHub kills jobs at 360)
  OCFS_REFRESH_DAYS     default 14
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(label, args, required=True):
    print(f"\n=== {label} ===", flush=True)
    rc = subprocess.call([PY] + args, cwd=HERE)
    if rc != 0:
        if required:
            print(f"CAPTURE ABORTED at: {label} (exit {rc})", file=sys.stderr)
            sys.exit(rc)
        print(f"  (non-fatal) {label} exited {rc}; continuing.")
    return rc


def main():
    run("DOHMH active roster", [os.path.join(HERE, "fetch_active.py")])
    run("OCFS registry (with per-borough receipt gate)",
        [os.path.join(HERE, "fetch_ocfs.py")])
    run("Pre-K DBN sidecar", [os.path.join(HERE, "fetch_prek_dbn.py")])
    run("MySchools current Pre-K directory (mints missing sites)",
        [os.path.join(HERE, "fetch_prek_myschools.py")])
    run("Head Start locator", [os.path.join(HERE, "fetch_headstart.py")],
        required=False)
    run("OCFS profile crawl (histories, checklists, contacts, hours)",
        [os.path.join(HERE, "fetch_ocfs.py"), "--profiles",
         "--refresh-days", os.environ.get("OCFS_REFRESH_DAYS", "14"),
         "--time-budget-min", os.environ.get("OCFS_TIME_BUDGET_MIN", "300"),
         "--workers", os.environ.get("OCFS_CRAWL_WORKERS", "1")])
    # Moved here from the private repo on 2026-10-04. All six read a public
    # government source and write into data/processed/, which is this repo's
    # job -- they were simply never migrated when the split was made, so they
    # kept running on the private repo's metered minutes. Measured there: the
    # DOHMH portal scrape was 51.5 min of a 60-min billed run (85%), and the
    # Article-47 water-lead sweep 48.9 min of a 50-min weekly run.
    #
    # Order matters: inspections and water-lead read AND write the per-facility
    # files under data/processed/facilities/, so they must follow the OCFS crawl
    # above that creates them. All six are required=False: a DOHMH portal outage
    # or an InfoHub 404 must never discard a good OCFS capture.
    # Per-run caps, because these run in SEQUENCE and the two portal sweeps
    # are the long ones. Uncapped, a full inspections sweep (68.5 min) plus a
    # full water-lead sweep (48.9) can consume the step's whole budget and the
    # four fetchers below them never run at all -- which looks like those
    # sources being broken rather than never reached. Both write incrementally
    # and both window on a timestamp, so a cap is a slice, not a loss: three
    # runs a day drain the backlog instead of one run trying to.
    run("DOHMH inspection histories (per-facility portal)",
        [os.path.join(HERE, "fetch_inspections.py"),
         "--limit", os.environ.get("INSPECTIONS_LIMIT", "900")], required=False)
    run("Article 47 water-lead results (per-facility portal)",
        [os.path.join(HERE, "fetch_water_lead.py"),
         "--limit", os.environ.get("WATER_LEAD_LIMIT", "900")], required=False)
    run("GeoSearch rooftop geocode (fills coordinate gaps)",
        [os.path.join(HERE, "fetch_geocode.py")], required=False)
    run("MTA subway entrances (transit proximity)",
        [os.path.join(HERE, "fetch_transit.py")], required=False)
    run("DOE class size (InfoHub snapshot; self-limits to monthly)",
        [os.path.join(HERE, "fetch_class_size.py")], required=False)
    run("DOE school extras (survey/demographics/attendance; self-limits)",
        [os.path.join(HERE, "fetch_school_extras.py")], required=False)

    print("\ncapture complete")


if __name__ == "__main__":
    main()
