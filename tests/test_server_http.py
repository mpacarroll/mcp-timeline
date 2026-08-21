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
import http.client
import json
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


class PublicHostAllowlistTests(unittest.TestCase):
    """MCP_PUBLIC_HOST must widen the SDK's DNS-rebinding Host-header check
    to admit a tunnel's public hostname, without disabling the check for
    everything else -- an arbitrary Host header must still be rejected."""

    PUBLIC_HOST = "fake-tunnel.example.com"

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        env = os.environ.copy()
        env["MCP_TRANSPORT"] = "streamable-http"
        env["MCP_HOST"] = "127.0.0.1"
        env["MCP_PORT"] = str(cls.port)
        env["MCP_PUBLIC_HOST"] = cls.PUBLIC_HOST
        # No TIMELINE_DB needed: these requests fail DNS-rebinding validation
        # (or don't) before any tool ever touches the database.

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

    def post_with_host_header(self, host_header):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05",
                                      "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1.0"}}})
        conn.request("POST", "/mcp", body=body, headers={
            "Host": host_header, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        resp = conn.getresponse()
        status, text = resp.status, resp.read().decode()
        conn.close()
        return status, text

    def test_configured_public_host_is_allowed(self):
        status, text = self.post_with_host_header(self.PUBLIC_HOST)
        self.assertNotEqual(status, 421, f"public host wrongly rejected: {text!r}")

    def test_unrelated_host_header_still_rejected(self):
        status, text = self.post_with_host_header("some-other-host.example.com")
        self.assertEqual(status, 421)
        self.assertIn("Invalid Host header", text)


if __name__ == "__main__":
    unittest.main()
