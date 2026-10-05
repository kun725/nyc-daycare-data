#!/usr/bin/env python3
"""
NYC Daycare Check — DOE class-size fetcher (replaces the frozen 2021-22
Open-Data copy).

NYC Open Data's school-level class-size dataset (sgr7-hhwp) froze at 2021-22,
but the DOE posts current snapshots three times a year (Nov 15 / Feb 15 /
"end of June" per the class-size law) as Excel on InfoHub with a stable URL
pattern (verified live 2026-07-19, June 2025-26 present):

    https://infohub.nyced.org/docs/default-source/default-document-library/
        {november|february|june}-{YYYY-YY}-class-size---school.xlsx

We take the newest available snapshot's "K-5 Average" sheet, aggregate the
per-(grade, program-type) rows to one entry per (DBN, grade), and write the
same sidecar shape the hand-built 2021-22 file used, so build_data's
load_school_sidecars needs no changes:

    webapp/data/processed/schools_class_size.json
      [ {dbn, school_name, school_year, data_note, grades:[{grade, avg, max,
         num_classes}], avg_class_size, max_class_size, overcrowded_grades,
         source, last_updated} ]

"Overcrowded" = average above the NYS class-size law cap for that grade
(K-3: 20, grades 4-8: 23) — the law's own line, not our judgment.

Self-limiting: skips when the sidecar is <25 days old (snapshots change three
times a year; nightly runs shouldn't hammer InfoHub). --force overrides.
"""
import datetime
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "schools_class_size.json"))
URL_TMPL = ("https://infohub.nyced.org/docs/default-source/default-document-library/"
            "{month}-{sy}-class-size---school.xlsx")
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"
LAW_CAP = {"K": 20, "01": 20, "02": 20, "03": 20, "1": 20, "2": 20, "3": 20,
           "04": 23, "05": 23, "4": 23, "5": 23}
SELF_LIMIT_DAYS = 25


def _school_years():
    """Current school year first (July belongs to the year just ended)."""
    now = datetime.date.today()
    start = now.year if now.month >= 7 else now.year - 1
    return [f"{y}-{str(y + 1)[-2:]}" for y in (start, start - 1)]


def _find_latest():
    for sy in _school_years():
        for month in ("june", "february", "november"):
            url = URL_TMPL.format(month=month, sy=sy)
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    if r.status == 200:
                        return url, sy, month
            except Exception:
                continue
    return None, None, None


def _num(v):
    """'<15' masking and blanks -> None; else float."""
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def main():
    if "--force" not in sys.argv and os.path.exists(OUT):
        age = (datetime.date.today()
               - datetime.date.fromtimestamp(os.path.getmtime(OUT))).days
        if age < SELF_LIMIT_DAYS:
            print(f"[class-size] sidecar is {age}d old (<{SELF_LIMIT_DAYS}d) — skipping")
            return

    url, sy, month = _find_latest()
    if not url:
        print("[class-size] no InfoHub snapshot found — keeping existing sidecar")
        return
    print(f"[class-size] fetching {month} {sy} snapshot …")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    # openpyxl validates by file extension, so the temp file must end in .xlsx
    tmp = OUT + ".tmp.xlsx"
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
        fh.write(r.read())

    import openpyxl
    wb = openpyxl.load_workbook(tmp, read_only=True)
    if "K-5 Average" not in wb.sheetnames:
        print(f"[class-size] sheet layout changed ({wb.sheetnames}) — keeping existing sidecar")
        os.remove(tmp)
        return
    ws = wb["K-5 Average"]
    rows = ws.iter_rows(values_only=True)
    header = [str(c or "").strip() for c in next(rows)]
    col = {name: header.index(name) for name in
           ("DBN", "School Name", "Grade Level", "Number of Students",
            "Number of Classes", "Maximum Class Size") if name in header}
    if len(col) < 6:
        print(f"[class-size] header changed ({header}) — keeping existing sidecar")
        os.remove(tmp)
        return

    # Aggregate (DBN, grade) across program types (Gen Ed / ICT / G&T …).
    agg = {}
    names = {}
    for r in rows:
        dbn = str(r[col["DBN"]] or "").strip()
        grade = str(r[col["Grade Level"]] or "").strip()
        if not dbn or not grade:
            continue
        names[dbn] = str(r[col["School Name"]] or "").strip()
        students = _num(r[col["Number of Students"]]) or 0
        classes = _num(r[col["Number of Classes"]]) or 0
        mx = _num(r[col["Maximum Class Size"]])
        a = agg.setdefault((dbn, grade), {"students": 0, "classes": 0, "max": None})
        a["students"] += students
        a["classes"] += classes
        if mx is not None:
            a["max"] = mx if a["max"] is None else max(a["max"], mx)
    wb.close()
    os.remove(tmp)

    by_dbn = {}
    for (dbn, grade), a in agg.items():
        if not a["classes"]:
            continue
        avg = round(a["students"] / a["classes"], 1)
        by_dbn.setdefault(dbn, []).append(
            {"grade": grade, "avg": avg,
             "max": int(a["max"]) if a["max"] is not None else None,
             "num_classes": int(a["classes"])})

    note = (f"DOE InfoHub class-size report, {month.title()} {sy} snapshot "
            f"(published under the NYS class-size law).")
    out = []
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    grade_order = {"K": 0, "01": 1, "02": 2, "03": 3, "04": 4, "05": 5}
    for dbn, grades in sorted(by_dbn.items()):
        grades.sort(key=lambda g: grade_order.get(g["grade"], 9))
        over = [g["grade"] for g in grades
                if g["grade"] in LAW_CAP and g["avg"] > LAW_CAP[g["grade"]]]
        avgs = [g["avg"] for g in grades]
        maxes = [g["max"] for g in grades if g["max"] is not None]
        out.append({
            "dbn": dbn, "school_name": names.get(dbn, ""), "school_year": sy,
            "data_note": note, "grades": grades,
            "avg_class_size": round(sum(avgs) / len(avgs), 1) if avgs else None,
            "max_class_size": max(maxes) if maxes else None,
            "overcrowded_grades": over,
            "source": "NYC DOE InfoHub", "last_updated": stamp,
        })
    if len(out) < 400:
        print(f"[class-size] only {len(out)} schools parsed — refusing to overwrite")
        return
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    over_n = sum(1 for r in out if r["overcrowded_grades"])
    print(f"[class-size] wrote {len(out)} schools ({month} {sy}; "
          f"{over_n} with a grade over the class-size-law cap) -> {OUT}")


if __name__ == "__main__":
    main()
