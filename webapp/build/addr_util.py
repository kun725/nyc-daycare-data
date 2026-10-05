#!/usr/bin/env python3
"""The address normalizer, and nothing else.

Every address join in this project agrees only because both sides run the
string through canon_addr() first: the CACFP join, operator grouping, the
duplicate-license key, the name+borough counts, and the DOE Pre-K minting that
decides whether a directory site already has a raw record. ADDR_TOKEN also
mirrors canonQ() in app.js, so the browser and the build agree too.

It lives in its own module because it is shared by both repositories. It used
to be defined in fetch_signals.py, which cannot run in the public capture repo
(it globs the build's output directory, which does not exist there). When
fetch_signals.py was deleted from that repo on 2026-10-04, the deletion took
canon_addr with it and fetch_prek_myschools.py -- which imports it at module
level -- stopped importing at all. Because MySchools is a required fetcher,
every capture run aborted: the public repo collected nothing from 01:21Z until
this was fixed.

Copying the function into the public repo would have fixed that run and left
two definitions of the one thing both sides of every address join must agree
on. The same drift already happened to fetch_ocfs.py, where the public copy
had the better retry logic and the private one was quietly behind. So: one
definition per repo, in a module with no I/O and no dependencies beyond re,
and the two copies are expected to stay byte-identical.
"""
import re

# Mirrors canonQ() in app.js — both sides of any address join must agree.
ADDR_TOKEN = {
    "east": "e", "west": "w", "north": "n", "south": "s",
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "place": "pl", "road": "rd", "drive": "dr", "court": "ct",
    "parkway": "pkwy", "lane": "ln", "terrace": "ter", "square": "sq",
    "heights": "hts",
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12",
}


def canon_addr(s):
    s = re.sub(r"[.,'’#\-]", " ", str(s or "").lower())
    s = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", s)
    return " ".join(ADDR_TOKEN.get(t, t) for t in s.split())
