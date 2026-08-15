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
3. Register the server with your MCP client (Claude Code shown):
   ```bash
   claude mcp add timeline -e TIMELINE_DB=/path/to/timeline.db -- \
       /path/to/.venv/bin/python /path/to/server.py
   ```

## Tools

| Tool | Answers |
|---|---|
| `activity_stats` | How much did I walk/drive/fly, and when? |
| `day_summary` | Where was I on a given day? |
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

The schema in `ingest.py` is the contract between importers and the server:
a new data source (Android's export format, a native collector app) is a new
importer that writes the same four tables, nothing else needs to change.
Issues and pull requests welcome.

## License

MIT. See [`LICENSE`](LICENSE).

---

The views expressed are solely my own and do not represent the views of my employer.
