# Roadmap

Decisions parked here are operator calls, recorded with their reasoning.

## Near-term priority: reach before revenue

Decided 2026-08-15. A priced knowledge product built on this project needs
an audience that doesn't exist yet, and it's the wrong shape for the
identity fronting this venture anyway (proof-and-positioning to a small
credible audience, not cold reach to a large one). The first move is a
free technical write-up of this build, published where a technical
audience can actually see it, as the cheap test for whether that audience
exists at all before any paid product gets built on the assumption that it
does. Monetization planning below is unchanged; it's just explicitly
sequenced behind that test now, not ahead of it.

## v1 (now): export-and-query

- Manual Timeline export from the Google Maps app, ingested to SQLite,
  queried through the four MCP tools. Ships first; validates the product
  experience before any app work.

## v1.1 (now): Apple Health workouts

- `ingest_healthkit.py` ships: reads a Health app export (`export.xml`
  plus `workout-routes/*.gpx`) and writes workouts into the same
  `activities`/`path_points` tables ingest.py uses, tagged
  `source=healthkit`. Covers Apple Watch runs, rides, swims, and any
  other tracked session with workout-grade GPS, without a native
  collector app: manual export, same as Timeline's v1. Required a real
  schema change (a `source` column plus per-source rebuild instead of a
  full-table rebuild) so two importers can coexist in one database; see
  `schema.py` and the `python-mcp-server` stack convention for the
  gotcha this closed.
- Phone/Watch access to the index itself (streamable-HTTP transport,
  tunneled and access-controlled) is tracked separately; see the open
  PR adding it to `server.py`.

## v2: native collector app, Apple devices first

- Manual export-then-ingest (v1, v1.1) still requires opening an app and
  tapping export; a native collector removes that step entirely and can
  pull continuously instead of point-in-time. Same schema contract:
  collector data is one more importer, the server does not change.
- Monetization lives here (paid collector, open protocol). Repo stays
  private until v1 is solid and outside-business clearance is done,
  then flips public.

## Up next: Ultrahuman ring, AirTags

Operator decision 2026-08-21: after HealthKit, add an Ultrahuman importer
(sleep, activity, and recovery data via their developer API; blocked on
applying to that program) and an AirTags/Find My importer (no official
API; a community decryption path exists but is fragile, lowest priority
of the two). Same schema contract as HealthKit: a new `source` value, no
server changes.

## Parked for later versions

- Android and other-device collectors. Same schema contract; each
  platform is one more importer, not a redesign.
- Additional importers for legacy formats (Android on-device export,
  pre-2024 Takeout Semantic Location History archives).
- Place-name resolution for unnamed places (lazy, on demand, never by
  ingest).
