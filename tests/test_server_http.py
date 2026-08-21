#!/usr/bin/env python3
"""Real streamable-HTTP protocol test: starts server.py as an actual HTTP
server on a background thread, connects a real client to it over the
network stack, calls a tool, asserts on the result. No real data;
uses tests/fixtures/sample_export.json.

This is the transport the Claude API's MCP connector and remote custom
connectors use; stdio-only testing does not exercise it. Run:
    python3 -m unittest discover -s tests
"""

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ingest

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_export.json")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        ingest.ingest(FIXTURE, cls.db_path)
        cls.port = free_port()

        env = os.environ.copy()
        env["TIMELINE_DB"] = cls.db_path
        env["MCP_TRANSPORT"] = "streamable-http"
        env["MCP_HOST"] = "127.0.0.1"
        env["MCP_PORT"] = str(cls.port)

        import subprocess
        server_py = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "server.py")
        cls.proc = subprocess.Popen(
            [sys.executable, server_py], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            cls.proc.terminate()
            raise RuntimeError("server did not start listening in time")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        os.remove(cls.db_path)

    def call(self, tool, args):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def run():
            async with streamable_http_client(
                    f"http://127.0.0.1:{self.port}/mcp") as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    return await s.call_tool(tool, args)
        return asyncio.run(run())

    def test_tools_reachable_over_http(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def run():
            async with streamable_http_client(
                    f"http://127.0.0.1:{self.port}/mcp") as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    return [t.name for t in (await s.list_tools()).tools]
        names = asyncio.run(run())
        self.assertEqual(sorted(names),
                          ["activity_stats", "day_summary", "place_history",
                           "visits_near"])

    def test_real_call_over_http_returns_structured_data(self):
        result = self.call("visits_near", {"lat": 1.0, "lng": 1.0, "radius_m": 50})
        self.assertEqual(result.structured_content["result"][0]["place_id"],
                          "TESTPLACE_HOME_0001")


if __name__ == "__main__":
    unittest.main()
