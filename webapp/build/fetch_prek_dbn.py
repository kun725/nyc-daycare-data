#!/usr/bin/env python3
"""
NYC Daycare Check — Pre-K site → DBN (+ building ids) sidecar.

Our DOE school-based Pre-K records came from kiyv-ks3f ("Universal Pre-K (UPK)
School Locations", frozen upstream 2017-11-29) but the legacy normalize DROPPED
the school code, so nothing could ever join school-quality data to them. This
re-fetches the same dataset and emits a sidecar keyed by canon(name)|canon(street):

    prek_dbn.json = { "<canon name>|<canon street>": {
        "dbn": "15K001",          # sems_code — the DOE district-borough-school id
        "bin": ..., "bbl": ...,   # building ids (future building-signal joins)
        "seats": int|None } }

Matching is SAME-SOURCE identity re-derivation (the records were generated from
these very rows), and only unambiguous keys are kept — a key that appears twice
with different DBNs is dropped rather than guessed.

The dataset is frozen, so this is fetch-once: if the sidecar exists and the
fetch fails, the existing file is kept.
"""
import json
import os
import re
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "processed", "prek_dbn.json"))
SRC = "https://data.cityofnewyork.us/resource/kiyv-ks3f.json"
UA = "NYCDaycareCheck/1.0 (+https://nycdaycarecheck.com; public-data ingest)"

_ABBR = {"street": "st", "avenue": "ave", "boulevard": "blvd", "road": "rd",
         "place": "pl", "drive": "dr", "east": "e", "west": "w", "north": "n",
         "south": "s"}


def canon(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    s = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", s)   # 29th -> 29 (street ordinals)
    words = [_ABBR.get(w, w) for w in s.split()]
    return " ".join(words)


def main():
    rows, offset = [], 0
    try:
        while True:
            req = urllib.request.Request(
                f"{SRC}?$limit=5000&$offset={offset}", headers={"User-Agent": UA})
            page = json.loads(urllib.request.urlopen(req, timeout=40).read())
            rows.extend(page)
            offset += len(page)
            if len(page) < 5000:
                break
    except Exception as e:
        print(f"[prek_dbn] fetch failed: {e}")
        if os.path.exists(OUT):
            print("[prek_dbn] keeping existing sidecar")
        return

    out, dropped = {}, 0
    for r in rows:
        dbn = (r.get("sems_code") or "").strip()
        name, addr = r.get("locname"), r.get("address")
        if not dbn or not name or not addr:
            continue
        k = canon(name) + "|" + canon(addr)
        seats = r.get("seats")
        try:
            seats = int(seats) if seats not in (None, "") else None
        except (TypeError, ValueError):
            seats = None
        entry = {"dbn": dbn, "bin": (r.get("bin") or "").strip() or None,
                 "bbl": (str(r.get("bbl") or "")).strip() or None, "seats": seats}
        if k in out and out[k]["dbn"] != dbn:
            out[k] = None      # ambiguous — poison the key, drop below
            dropped += 1
        elif k not in out:
            out[k] = entry
    out = {k: v for k, v in out.items() if v}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[prek_dbn] {len(rows)} source rows -> {len(out)} unambiguous name|street keys "
          f"({dropped} ambiguous dropped) -> {OUT}")


if __name__ == "__main__":
    main()
