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
    print("\ncapture complete")


if __name__ == "__main__":
    main()
