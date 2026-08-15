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

## v2: native collector app, Apple devices first

- The collector app targets the whole Apple device family, not just
  iPhone. Apple Watch matters: watchOS location plus workout-grade GPS
  extends coverage to phone-free runs and rides, and HealthKit workout
  routes (HKWorkoutRoute) are themselves a candidate importer since
  Apple already records them.
- Collector data replaces the manual export step; it writes the same
  SQLite schema. The schema is the contract, the server does not change.
- Monetization lives here (paid collector, open protocol). Repo stays
  private until v1 is solid and outside-business clearance is done,
  then flips public.

## Parked for later versions

- Android and other-device collectors. Same schema contract; each
  platform is one more importer, not a redesign.
- Additional importers for legacy formats (Android on-device export,
  pre-2024 Takeout Semantic Location History archives).
- Place-name resolution for unnamed places (lazy, on demand, never by
  ingest).
