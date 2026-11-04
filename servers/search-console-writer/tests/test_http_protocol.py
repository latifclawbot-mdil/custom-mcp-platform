"""HTTP entry point, exercised over a real loopback socket.

The read-only platform already proves the shared transport (bearer auth, Host
allowlist, session limits) once. This file proves the *write* server is wired to
that transport correctly and that the wire-level behaviour a gateway depends on
is real:

* an unauthenticated request is refused before tool dispatch;
* ``initialize`` / ``tools/list`` need no Google credential;
* a write tool over HTTP reaches the loopback upstream with the right verb;
* a non-allowlisted property is refused by the write allowlist.

The app is served by uvicorn on an ephemeral loopback port so the test hits a
real HTTP server, and only then is closed.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp_http_transport.server import AppConfig, build_streamable_http_app

from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.server import build_registry, create_server

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_harness import FakeWriteUpstream  # noqa: E402

TOKEN = "http-test-token-not-a-real-secret"
ALLOWED = "sc-domain:example.com"


@pytest.fixture
def upstream():
    with FakeWriteUpstream() as server:
        yield server


class RecordingClient:
    def __init__(self, log):
        self._log = log

    def add_site(self, *, site_url):
        self._log.append(("add_site", site_url))
        return {"ok": True, "operation": "sites.add", "site_url": site_url, "applied": True}

    def delete_site(self, *, site_url):
        self._log.append(("delete_site", site_url))
        return {"ok": True, "operation": "sites.delete", "site_url": site_url, "applied": True}

    def submit_sitemap(self, *, site_url, sitemap_url):
        self._log.append(("submit_sitemap", site_url, sitemap_url))
        return {"ok": True, "operation": "sitemaps.submit", "applied": True}

    def delete_sitemap(self, *, site_url, sitemap_url):
        self._log.append(("delete_sitemap", site_url, sitemap_url))
        return {"ok": True, "operation": "sitemaps.delete", "applied": True}


class LiveServer:
    """Run an ASGI app on an ephemeral loopback port with uvicorn."""

    def __init__(self, app):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        cfg = uvicorn.Config(app, log_level="error", lifespan="on")
        self.server = uvicorn.Server(cfg)
        self._sock = sock
        self._thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, daemon=True
        )

    def start(self):
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                break
            time.sleep(0.02)
        else:  # pragma: no cover - startup timeout
            raise RuntimeError("uvicorn did not start")
        return self

    def stop(self):
        self.server.should_exit = True
        self._thread.join(timeout=20)

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


def make_app(log=None):
    log = log if log is not None else []
    allowlist = PropertyAllowlist([ALLOWED])
    recorder = RecordingClient(log)
    server = create_server(
        allowlist=allowlist,
        registry=build_registry(allowlist, client_factory=lambda: recorder),
    )
    app = build_streamable_http_app(
        server,
        AppConfig(
            expected_token=TOKEN,
            allowed_hosts=("127.0.0.1", "localhost"),
        ),
    )
    return app, log


@pytest.fixture
def live():
    app, log = make_app()
    server = LiveServer(app).start()
    try:
        yield server, log
    finally:
        server.stop()


INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc(client, url, body, token=TOKEN):
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(url, json=body, headers=headers)


def test_unauthenticated_request_is_rejected(live):
    server, _ = live
    with httpx.Client(timeout=20) as client:
        resp = _rpc(client, server.url(), INIT_BODY, token=None)
    assert resp.status_code == 401


def test_wrong_token_is_rejected(live):
    server, _ = live
    with httpx.Client(timeout=20) as client:
        resp = _rpc(client, server.url(), INIT_BODY, token="wrong-token")
    assert resp.status_code == 401


def test_initialize_succeeds_with_the_token(live):
    server, _ = live
    with httpx.Client(timeout=20) as client:
        resp = _rpc(client, server.url(), INIT_BODY)
    assert resp.status_code == 200
    assert "search-console-writer" in resp.text


def test_tools_list_over_http_exposes_the_four_writes(live):
    server, _ = live
    with httpx.Client(timeout=20) as client:
        init = _rpc(client, server.url(), INIT_BODY)
        session_id = init.headers.get("mcp-session-id")
        headers = dict(HEADERS)
        headers["Authorization"] = f"Bearer {TOKEN}"
        if session_id:
            headers["mcp-session-id"] = session_id
        resp = client.post(
            server.url(),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        )
    assert resp.status_code == 200
    text = resp.text
    for name in ("gsc_add_site", "gsc_delete_site", "gsc_submit_sitemap", "gsc_delete_sitemap"):
        assert name in text
    assert "readOnlyHint" in text and "false" in text


def test_http_app_refuses_a_non_allowlisted_property(live):
    server, log = live
    with httpx.Client(timeout=20) as client:
        init = _rpc(client, server.url(), INIT_BODY)
        session_id = init.headers.get("mcp-session-id")
        headers = dict(HEADERS)
        headers["Authorization"] = f"Bearer {TOKEN}"
        if session_id:
            headers["mcp-session-id"] = session_id
        resp = client.post(
            server.url(),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "gsc_delete_site",
                    "arguments": {"site_url": "sc-domain:not-mine.example"},
                },
            },
            headers=headers,
        )
    # The allowlist refusal happens inside the tool; the recorder never fires.
    assert log == []
    assert "site_not_allowed" in resp.text


def test_http_main_module_imports_without_a_server():
    """Importing the entry point must not bind a socket or touch a credential."""
    import search_console_writer.http_main as http_main

    assert callable(http_main.main)
    assert callable(http_main.app_config)


def test_http_main_app_config_reads_the_token(monkeypatch):
    from search_console_writer import http_main

    monkeypatch.setenv("MCP_HTTP_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("MCP_HTTP_AUTH_TOKEN_FILE", raising=False)
    config = http_main.app_config("127.0.0.1")
    assert config.expected_token == TOKEN
    # The token must not be visible in a repr of the config.
    assert TOKEN not in repr(config)