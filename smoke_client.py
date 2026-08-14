#!/usr/bin/env python3
"""End-to-end smoke test: spawn server.py over stdio as a real MCP client.

Exercises the same handshake an AI client performs: initialize, list tools,
call each tool once. All inputs are derived from the database the server is
pointed at (TIMELINE_DB), so this file stays free of anyone's actual data.

Usage:
    TIMELINE_DB=/path/to/timeline.db .venv/bin/python smoke_client.py
"""

import asyncio
import json
import os
import sqlite3
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DB = os.environ.get("TIMELINE_DB", "timeline.db")


def probe_inputs():
    """Pull demo inputs out of the target database itself."""
    db = sqlite3.connect(DB)
    top = db.execute(
        "SELECT place_id, lat, lng FROM places ORDER BY visit_count DESC LIMIT 1"
    ).fetchone()
    last_day = db.execute("SELECT MAX(local_date) FROM activities").fetchone()[0]
    span = db.execute(
        "SELECT MIN(local_date), MAX(local_date) FROM activities").fetchone()
    db.close()
    return {"top_place_id": top[0], "lat": top[1], "lng": top[2],
            "last_day": last_day, "first_date": span[0], "last_date": span[1]}


def show(name, result):
    body = result.structured_content or [c.text for c in result.content]
    print(f"\n=== {name} ===")
    print(json.dumps(body, indent=2)[:1500])


async def main():
    p = probe_inputs()
    params = StdioServerParameters(
        command=sys.executable, args=["server.py"],
        env={**os.environ, "TIMELINE_DB": DB})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools advertised:", [t.name for t in tools.tools])

            show("activity_stats (walking, full range, by month)",
                 await s.call_tool("activity_stats", {
                     "mode": "walking", "start_date": p["first_date"],
                     "end_date": p["last_date"], "group_by": "month"}))
            show(f"day_summary ({p['last_day']})",
                 await s.call_tool("day_summary", {"date": p["last_day"]}))
            show("visits_near (coords of most-visited place)",
                 await s.call_tool("visits_near", {
                     "lat": p["lat"], "lng": p["lng"]}))
            show("place_history (by label: Home)",
                 await s.call_tool("place_history", {"place": "Home", "limit": 3}))
            show("day_summary (1999-01-01, before any data)",
                 await s.call_tool("day_summary", {"date": "1999-01-01"}))


if __name__ == "__main__":
    asyncio.run(main())
