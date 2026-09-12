# nyc-daycare-data

The capture layer behind [nycdaycarecheck.com](https://nycdaycarecheck.com): fetchers and raw public records for every licensed childcare provider in New York City.

## What's here

- `webapp/build/` — the fetchers. They pull from the same public sources any parent can check by hand: the NYC Health Department's day care roster, the NYS OCFS licensed-provider registry and its per-provider lookup (inspection visits, checklists, complaint history, capacity, posted hours), the DOE's MySchools directory, and the federal Head Start locator.
- `webapp/data/processed/` — the raw records those fetchers produce: one JSON file per facility, plus sidecars for inspection histories and program details, each carrying the date it was captured.
- `webapp/build/tests/` — parser tests pinned to saved copies of the state's real pages, and live canaries that probe every source on a schedule so a moved or changed government site surfaces within hours.

## What's not here

The site itself: page building, cross-agency enrichment, and everything you see at nycdaycarecheck.com happens in a separate repository that pulls this one. This repo is just the record of what the government publishes, kept current.

## Provenance

Every record here is the government's own, reproduced as published, with capture dates. Nothing is graded, scored, or edited. To verify anything against the originals, the full source list with direct links is on the site's [methodology page](https://nycdaycarecheck.com/methodology#check-yourself).

Fetchers run on a schedule with per-request pacing and an identified user agent. If you operate one of these sources and have concerns, open an issue.

## License

Code is MIT licensed. The records are public government data; no license is claimed over them.
