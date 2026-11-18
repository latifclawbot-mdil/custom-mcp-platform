"""End-to-end over the real transport stack, in the platform's own idiom.

The server runs as a real subprocess (``logto_ops.http_main``) and a fake
Management API runs on loopback. Nothing reaches Logto.

What this proves that the unit suite cannot: bearer auth on the HTTP endpoint,
Host allowlisting, MCP session setup, tool listing, the confirmation gate over
real JSON-RPC, and that the outbound request Logto receives is the one the
operation describes.

The fake API is plain HTTP, so the subprocess is launched with a patched
settings loader that keeps every fail-closed check but swaps the base URL.
Config parsing has its own suite; this one is about transport and dispatch.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[1]
LOGTO_SRC = REPO_ROOT / "servers" / "logto-ops" / "src"
TRANSPORT_SRC = REPO_ROOT / "packages" / "mcp_http_transport" / "src"

TEST_TOKEN = "logto-ops-test-token-0123456789"
CONFIRM_SECRET = "confirmation-secret-" + "x" * 32

#: Builds Settings directly so the fake API can be plain HTTP. Every other
#: startup check (gate, secret file, confirmation secret) still runs.
LAUNCHER = """
import os, sys
from dataclasses import replace
from logto_ops import settings as s
from logto_ops.http_main import main

_base = sys.argv[1]
real = s.load_settings
patched_settings = replace(real(None), base_url=_base)

# http_main imports load_settings by name, so patch it there too.
import logto_ops.http_main as http_main
http_main.load_settings = lambda env=None: patched_settings
main()
"""


class FakeLogto:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status: int, payload) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return None

            def do_POST(self):
                body = self._body()
                outer.requests.append(("POST", self.path, body))
                if self.path == "/oidc/token":
                    return self._send(200, {"access_token": "tok", "expires_in": 3600, "scope": "all"})
                if self.path == "/api/applications":
                    body = body if isinstance(body, dict) else {}
                    return self._send(
                        201, {"id": "app-new", "name": body.get("name"), "type": body.get("type")}
                    )
                if self.path.endswith("/secrets"):
                    body = body if isinstance(body, dict) else {}
                    return self._send(201, {"name": body.get("name"), "value": "one-time-secret"})
                return self._send(201, {"ok": True})

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                if self.path.startswith("/api/applications"):
                    return self._send(200, [{"id": "app-1", "name": "existing", "type": "MachineToMachine"}])
                return self._send(200, [])

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def creates(self) -> list[tuple[str, str, object]]:
        return [r for r in self.requests if r[0] == "POST" and r[1] == "/api/applications"]

    def close(self) -> None:
        self.httpd.shutdown()
        # shutdown() stops serve_forever but leaves the listening socket bound;
        # server_close() releases it so the strict pytest filter does not report
        # an unclosed socket after the last test.
        self.httpd.server_close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def spawn(port: int, tmp_path: Path, logto: FakeLogto):
    secret = Path(tmp_path) / "m2m_secret"
    secret.write_text("client-secret-value", encoding="utf-8")
    secret.chmod(0o600)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": f"{LOGTO_SRC}:{TRANSPORT_SRC}",
        "MCP_HTTP_AUTH_TOKEN": TEST_TOKEN,
        "MCP_HTTP_PORT": str(port),
        "MCP_HTTP_HOST": "127.0.0.1",
        "MCP_HTTP_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "LOGTO_BASE_URL": "https://placeholder.example",
        "LOGTO_MCP_MODE": "apply",
        "LOGTO_M2M_APP_ID": "app-self",
        "LOGTO_M2M_SECRET_FILE": str(secret),
        "LOGTO_MCP_CONFIRM_SECRET": CONFIRM_SECRET,
    }
    log_path = Path(tmp_path) / f"logto-ops-{port}.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", LAUNCHER, logto.base_url],
            env=env,
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.time() + 30
        started = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    started = True
                    break
            except OSError:
                time.sleep(0.1)
        if not started:
            raise AssertionError(
                "logto-ops did not start:\n"
                + log_path.read_text(encoding="utf-8", errors="replace")
            )
        yield proc, log_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def http_client(token: str = TEST_TOKEN) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"authorization": f"Bearer {token}"}, timeout=30.0)


@pytest.fixture()
def stack(tmp_path):
    logto = FakeLogto()
    port = free_port()
    with spawn(port, tmp_path, logto) as (proc, log_path):
        yield logto, port, proc, log_path
    logto.close()


async def test_unauthenticated_request_is_rejected(stack):
    _, port, _, log_path = stack
    import asyncio

    def post():
        return httpx.post(f"http://127.0.0.1:{port}/mcp", json={}, timeout=10)

    response = await asyncio.to_thread(post)
    assert response.status_code == 401
    assert TEST_TOKEN not in read_log(log_path)


async def test_tools_list_shows_the_write_surface_and_no_deletes(stack):
    _, port, _, log_path = stack
    async with http_client() as client:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert "create_m2m_application" in names
    assert "create_application_secret" in names
    assert not [n for n in names if n.startswith("delete_")]
    schema = {tool.name: tool.input_schema for tool in tools.tools}["create_m2m_application"]
    assert schema.get("additionalProperties") is False
    assert "type" not in schema.get("properties", {})
    assert "Traceback" not in read_log(log_path)


async def test_gated_write_without_a_token_does_not_execute(stack):
    logto, port, _, log_path = stack
    async with http_client() as client:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("create_m2m_application", {"name": "scheduler"})
    assert result.is_error is True
    assert logto.creates() == []
    log = read_log(log_path)
    assert "client-secret-value" not in log
    assert TEST_TOKEN not in log


async def test_plan_then_apply_creates_the_application_exactly_once(stack):
    logto, port, _, _ = stack
    async with http_client() as client:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                plan = await session.call_tool(
                    "create_m2m_application", {"name": "scheduler", "dry_run": True}
                )
                token = plan.structured_content["confirmation_token"]
                applied = await session.call_tool(
                    "create_m2m_application", {"name": "scheduler", "confirmation_token": token}
                )
    assert plan.structured_content["plan"]["will_execute"] is False
    assert applied.is_error in (False, None)
    creates = logto.creates()
    assert len(creates) == 1
    assert creates[0][2] == {"name": "scheduler", "type": "MachineToMachine"}


async def test_apply_with_a_changed_argument_is_refused(stack):
    logto, port, _, _ = stack
    async with http_client() as client:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                plan = await session.call_tool(
                    "create_m2m_application", {"name": "scheduler", "dry_run": True}
                )
                changed = await session.call_tool(
                    "create_m2m_application",
                    {"name": "other", "confirmation_token": plan.structured_content["confirmation_token"]},
                )
    assert changed.is_error is True
    assert logto.creates() == []


async def test_secret_creation_returns_the_value_once(stack):
    _, port, _, _ = stack
    async with http_client() as client:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                plan = await session.call_tool(
                    "create_application_secret",
                    {"application_id": "app-1", "name": "ci", "dry_run": True},
                )
                applied = await session.call_tool(
                    "create_application_secret",
                    {
                        "application_id": "app-1",
                        "name": "ci",
                        "confirmation_token": plan.structured_content["confirmation_token"],
                    },
                )
    assert applied.structured_content["value"] == "one-time-secret"
