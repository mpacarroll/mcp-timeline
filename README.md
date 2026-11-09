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

Then in the OwnTracks app, Settings: set Mode to **HTTP**, the URL to your
public endpoint, **UserID** to anything you like, and **Password** to the
token, with the Authentication and Password toggles on.

The token goes in the Password field because HTTP Basic is what the app
uses by default, and it is the only path their documentation describes.
The receiver also accepts `Authorization: Bearer <token>`, for curl, other
clients, and the app's own **HTTP Headers** field under Expert Mode.

Pick one. An explicit `Authorization` header overrides the Basic
credentials, so a stale token left in the HTTP Headers field will keep
returning 401 no matter how correct the password is. Leaving that field
empty is the simpler configuration.

Basic credentials are only base64 encoded, not encrypted, which is
acceptable here because the tunnel is HTTPS end to end.

Fixes land in `path_points` tagged `source=owntracks`, alongside anything
the other importers wrote. Reports less accurate than
`OWNTRACKS_MAX_ACCURACY_M` (default 500m) are dropped rather than stored,
since a low-accuracy fix invents movement that never happened. Repeated
timestamps are ignored, so the app resending after a reconnect cannot
create duplicate rows.

This endpoint writes to the database, so it checks its own bearer token
rather than relying on a network gate in front of it: an unattended phone
app cannot complete an interactive browser login.

## Naming places the database does not know

A raw location feed knows where you were, not what was there. Google
Timeline shipped place identity with its exports, so `Home` and `Work`
have names. Anywhere new comes back as coordinates, which is unhelpful
the moment you travel.

`geocode.py` resolves those on demand. Set `GEOCODE_ENABLED=1` and a
derived stay with no known place is looked up and labeled.

It is **off by default**, because it sends coordinates to OpenStreetMap's
[Nominatim](https://nominatim.openstreetmap.org) service. Three limits
keep that as small as possible:

- Only **stays** are ever sent, never the full trail. A day sends a
  handful of points, not the thousands in between.
- Each coordinate is looked up **once, ever**, and cached in the
  database. Asking about the same day repeatedly sends nothing.
- Their usage policy (one request per second, identifying User-Agent) is
  enforced in code rather than left to callers.

A lookup that finds nothing is cached, so a genuinely nameless spot is
not re-requested forever. A lookup that *fails* is not cached, so a
momentary outage cannot permanently mark a real place as unidentifiable.

Every reconstructed visit carries a `place_source` saying how its name
was obtained, because the difference matters when interpreting an
answer:

| `place_source` | Means |
|---|---|
| `known place` | A source recorded visiting it. Strong evidence. |
| `address lookup, not a record of visiting` | Only what sits at those coordinates. Someone stopped at that address; what they did there is inference. |
| `coordinates only` | Nothing could be identified. |

That distinction is deliberate. A 50-minute stop at an address
containing a restaurant is not evidence of having eaten there, and the
tool docstring instructs the model to attribute the address and reason
openly rather than assert the activity.

## Running it unattended (macOS)

Started from a terminal, these processes die when the window closes or the
machine reboots. For the MCP server that is an outage you notice. For the
OwnTracks receiver it is worse and quieter: fixes that arrive while nothing
is listening are gone for good, and the database just stops growing.

`deploy/install-macos.sh` installs all three services (MCP server,
OwnTracks location receiver, Apple Health capture endpoint) as launchd
user agents, so they start at login and restart if they crash:

```bash
./deploy/install-macos.sh --dry-run   # inspect the generated plists first
./deploy/install-macos.sh             # install and load them
```

The first run creates `deploy/mcp-timeline.env` from the sample with a
freshly generated `OWNTRACKS_TOKEN`. Set `MCP_PUBLIC_HOST` in that file to
your tunnel hostname before installing; without it the MCP server rejects
tunneled requests with `421 Invalid Host header`. The env file and the
generated plists hold the token, so all three are written owner-only and
the env file is gitignored.

```bash
launchctl list | grep mcp-timeline          # status
tail -f ~/Library/Logs/mcp-timeline/*.log   # logs
./deploy/install-macos.sh --uninstall       # remove both services
```

The tunnel itself is separate. `cloudflared service install` already
registers it as its own background service, so it survives reboots on its
own.

## History studio: trips, stories, redaction and consent

The four tools above answer questions about your whole history. The
studio is for the other direction: turning a stretch of days into a trip
you edit, write up and share, while deciding exactly what leaves.

```bash
python3 tools/demo_studio.py demo.db          # synthetic Lisbon journey
TIMELINE_DB=demo.db python3 studio_web.py     # http://127.0.0.1:8010
```

Point `TIMELINE_DB` at your own database instead to use it for real. In
the browser studio you can:

- **Capture a trip** from a date range. Visits, journeys and recorded
  routes are copied in; days covered only by raw fixes (OwnTracks) are
  reconstructed and labeled as such. Overnight flights and stays that
  cross midnight show the next-day arrival.
- **Edit** names, captions and notes, hide items, and write each day's
  story over a plain auto-draft. Edits sit beside the captured snapshot;
  your history is never rewritten.
- **Inspect the source** of any item: what the source recorded, every
  edit you made, whether the source row still exists, and which
  redactions apply.
- **Redact** a place, an area, one item or a whole day, as *mask* (a
  neutral label, no name, coordinates or route) or *remove*. Routes into
  a redacted stop are dropped, and the redacted names are replaced in
  your own story text and captions too.
- **Share selectively**: pick an audience view (times exact, part of day
  or none; coordinates exact, neighborhood, city or none; routes,
  captions, story, movement, provenance, which days) with a live preview
  of exactly what leaves. Export JSON, GeoJSON, GPX, Markdown, HTML or
  CSV, or create a revocable local share page.
- **Delete** an item, a trip, or purge history for a date range from the
  source tables, every trip, share page and place-name cache. Earlier
  exports are marked stale; files the studio wrote can be removed, and
  browser downloads are listed so you can delete them yourself.

Every export, share page and agent read goes through one function
(`studio_export.build_view`), and `tests/test_studio_export.py` renders
every format under every view to prove redacted and deleted facts appear
in none of them, as names or as coordinates. Redactions apply to your own
exports too; there is no owner bypass.

The same is available from the command line: `python3 studio.py --help`.

### Giving an agent one trip, not your history

Agents get nothing from the studio by default. In the **Agent access**
tab, create a grant: one trip, one view, an expiry, read only or read
plus suggest. The token is shown once and only its hash is stored. Then
run the MCP server with only the studio tools:

```bash
claude mcp add trip -e TIMELINE_DB=/path/to/timeline.db \
    -e TIMELINE_TOOLS=studio -e TIMELINE_GRANT=<token> -- \
    /path/to/.venv/bin/python /path/to/server.py
```

| Tool | Does |
|---|---|
| `studio_scope` | What was shared, with which capabilities, until when. |
| `studio_trip` | The trip through the grant's view and your redactions. |
| `studio_day` | One shared day; other dates are refused. |
| `studio_propose` | Suggest a caption or story text; nothing changes until you accept it. |

The token lives in the server's environment, so the agent never sees or
chooses it. Every call, allowed or refused, appears in the access log in
the studio, and revoking a grant takes effect on the next call. With
`TIMELINE_TOOLS=studio` the four whole-history tools are not registered
at all. The default (`all`) keeps them, for your own trusted client.

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
