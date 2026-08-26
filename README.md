# mcp-timeline

Query your own Google Maps Timeline location history through an MCP
(Model Context Protocol) server. Ingest your Timeline export into a local
SQLite index, then ask an AI client questions like "how much did I walk in
March," "where was I on the 14th," or "have I been here before."

This repository holds the open core: the ingest pipeline, the SQLite schema,
and the MCP server exposing it as four tools. See [`ROADMAP.md`](ROADMAP.md)
for what's planned (a hosted version, an Apple-device collector) and what's
intentionally out of scope for now.

## Getting started

1. Export your Timeline data: Google Maps app -> your profile picture ->
   Settings -> Location & Privacy -> Timeline export -> Save to Files.
2. Build the index:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/python ingest.py /path/to/location-history.json timeline.db
   ```
3. Optional: add Apple Health workouts (run, ride, swim, and other tracked
   sessions, including GPS routes from Apple Watch) to the same index.
   Health app -> profile icon -> Export All Health Data, unzip it, then:
   ```bash
   .venv/bin/python ingest_healthkit.py /path/to/apple_health_export timeline.db
   ```
   Both importers write into the same database without touching each
   other's rows; run either, both, or neither, in any order.
4. Register the server with your MCP client (Claude Code shown):
   ```bash
   claude mcp add timeline -e TIMELINE_DB=/path/to/timeline.db -- \
       /path/to/.venv/bin/python /path/to/server.py
   ```

## Keeping location current, automatically

Steps 1 and 3 above are one-time snapshots. A Google Timeline export in
particular can never refresh itself: Timeline has been stored on-device
and encrypted since 2025, with no API and no Shortcuts action on the
export button, so the data it produces starts aging the moment you save
it.

`owntracks_receiver.py` closes that gap without any manual step.
[OwnTracks](https://owntracks.org) is a free, open-source iOS and Android
app that POSTs your location to an endpoint you control. Point it here
and location keeps accumulating on its own:

```bash
OWNTRACKS_TOKEN=$(openssl rand -hex 20) TIMELINE_DB=timeline.db \
    .venv/bin/python owntracks_receiver.py 8002
```

Then in the OwnTracks app, Settings -> Connection: set Mode to HTTP, the
URL to your public endpoint, and add a request header
`Authorization: Bearer <the same token>`.

Fixes land in `path_points` tagged `source=owntracks`, alongside anything
the other importers wrote. Reports less accurate than
`OWNTRACKS_MAX_ACCURACY_M` (default 500m) are dropped rather than stored,
since a low-accuracy fix invents movement that never happened. Repeated
timestamps are ignored, so the app resending after a reconnect cannot
create duplicate rows.

This endpoint writes to the database, so it checks its own bearer token
rather than relying on a network gate in front of it: an unattended phone
app cannot complete an interactive browser login.

## Tools

| Tool | Answers |
|---|---|
| `activity_stats` | How much did I walk/drive/fly/run/swim, and when? |
| `day_summary` | Where was I on a given day, and what did I do? |
| `visits_near` | Have I been near these coordinates before? |
| `place_history` | Every visit to one place. |

See `server.py`'s tool docstrings for full parameter details -- they're the
same text your MCP client sees.

## Privacy and data

Your location data never leaves your machine in the default setup: the
export you provide stays local, the SQLite index is local, and the server
runs locally over stdio. See [`TERMS_AND_CONDITIONS.md`](TERMS_AND_CONDITIONS.md)
for the full data-handling terms, including for the hosted version described
in the roadmap.

## Contributing

The schema in `schema.py` is the contract between importers and the server:
a new data source (Android's export format, an Ultrahuman ring, a native
collector app) is a new importer that writes the same tables, tagging its
own rows with a `source` value and rebuilding only those rows on each run.
The server and other importers never need to change for it. Issues and
pull requests welcome.

## License

MIT. See [`LICENSE`](LICENSE).

---

The views expressed are solely my own and do not represent the views of my employer.
