"""Shared helpers for the monorepo's protocol tests.

Everything here runs **offline**: the servers are spawned with a fake Google
upstream on a loopback port and a static test token seam, so no Google
credential is needed and no Google endpoint is contacted.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVERS = REPO_ROOT / "servers"
TRANSPORT_SRC = REPO_ROOT / "packages" / "mcp_http_transport" / "src"

#: A placeholder token used only inside this test process. It is not a secret
#: and is never valid against a real deployment.
TEST_TOKEN = "test-token-not-a-real-secret-0123456789"


def free_port() -> int:
    """Ask the kernel for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


@contextlib.contextmanager
def wait_for_port(port: int, timeout: float = 60.0, host: str = "127.0.0.1"):
    """Yield once ``port`` accepts connections, or raise on timeout."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_open(port, host):
            yield
            return
        time.sleep(0.1)
    raise TimeoutError(f"nothing listening on {host}:{port} after {timeout}s")


def http_post(url: str, body: dict, *, token: str | None = TEST_TOKEN, host: str | None = None):
    """POST a JSON-RPC body synchronously; returns the raw httpx response."""
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    if host is not None:
        headers["host"] = host
    return httpx.post(url, json=body, headers=headers, timeout=30.0)


def initialize_body(message_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "custom-mcp-platform-tests", "version": "0.1.0"},
        },
    }


def server_env(base: dict[str, str] | None = None, **overrides) -> dict[str, str]:
    """A minimal, credential-free environment for a spawned server."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Empty HOME: no ADC file can be discovered by accident.
        "HOME": "/nonexistent-home-for-tests",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    if base:
        env.update(base)
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


class FakeGoogleUpstream:
    """Loopback stand-in for a Google API host, used by server-level tests.

    Implements just enough of the surface to answer authenticated MCP calls
    without any Google credential: it accepts any request and records it.
    """

    def __init__(self) -> None:
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers)}
                )
                payload = outer.payload_for(self.path)
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = _handle
            do_POST = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def payload_for(self, path: str) -> dict:
        if path.endswith("/sites"):
            return {"siteEntry": [{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}]}
        return {"rows": [], "responseAggregationType": "byProperty"}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def import_server_module(server: str, module: str):
    """Import a module from one of the vendored servers without installing it."""
    src = SERVERS / server / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return __import__(module, fromlist=["*"])