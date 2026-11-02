"""MCP-layer tests: truthful annotations and schema, in process.

Annotations are advisory, but they are also what an MCP client (and a human
reviewing the tool list) uses to decide whether a call is destructive. Getting
them wrong on a write server is not a cosmetic bug: a delete advertised as
non-destructive invites exactly the misuse this server must not invite.
"""

from __future__ import annotations

import json

import anyio
import pytest

from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.server import build_registry, create_server

ALLOWED = "sc-domain:example.com"


class RecordingClient:
    def __init__(self):
        self.calls = []

    def add_site(self, *, site_url):
        self.calls.append(("add_site", site_url))
        return {"ok": True, "operation": "sites.add", "site_url": site_url, "applied": True}

    def delete_site(self, *, site_url):
        self.calls.append(("delete_site", site_url))
        return {"ok": True, "operation": "sites.delete", "site_url": site_url, "applied": True}

    def submit_sitemap(self, *, site_url, sitemap_url):
        self.calls.append(("submit_sitemap", site_url, sitemap_url))
        return {"ok": True, "operation": "sitemaps.submit", "applied": True}

    def delete_sitemap(self, *, site_url, sitemap_url):
        self.calls.append(("delete_sitemap", site_url, sitemap_url))
        return {"ok": True, "operation": "sitemaps.delete", "applied": True}


def make():
    allowlist = PropertyAllowlist([ALLOWED])
    client = RecordingClient()
    registry = build_registry(allowlist, client_factory=lambda: client)
    return create_server(allowlist=allowlist, registry=registry), client


def call(server, name, arguments):
    return anyio.run(server.call_tool, name, arguments)


def envelope(result):
    return json.loads(result.content[0].text)


def test_tools_list_contains_the_four_write_tools():
    server, _ = make()
    tools = anyio.run(server.list_tools)
    assert sorted(t.name for t in tools) == [
        "gsc_add_site",
        "gsc_delete_site",
        "gsc_delete_sitemap",
        "gsc_submit_sitemap",
    ]


def test_no_tool_is_advertised_read_only():
    """Every tool mutates, so every readOnlyHint must be false."""
    server, _ = make()
    for tool in anyio.run(server.list_tools):
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False, tool.name


def test_destructive_hint_is_true_only_for_the_deletes():
    server, _ = make()
    by_name = {t.name: t for t in anyio.run(server.list_tools)}
    assert by_name["gsc_delete_site"].annotations.destructive_hint is True
    assert by_name["gsc_delete_sitemap"].annotations.destructive_hint is True
    assert by_name["gsc_add_site"].annotations.destructive_hint is False
    assert by_name["gsc_submit_sitemap"].annotations.destructive_hint is False


def test_every_tool_is_advertised_idempotent():
    """PUT and DELETE are idempotent at the HTTP level; repeat is a no-op."""
    server, _ = make()
    for tool in anyio.run(server.list_tools):
        assert tool.annotations.idempotent_hint is True, tool.name


def test_open_world_hint_is_true():
    server, _ = make()
    for tool in anyio.run(server.list_tools):
        assert tool.annotations.open_world_hint is True, tool.name


def test_schemas_are_closed_and_advertise_required_arguments():
    server, _ = make()
    by_name = {t.name: t for t in anyio.run(server.list_tools)}
    assert by_name["gsc_add_site"].input_schema["required"] == ["site_url"]
    assert by_name["gsc_delete_site"].input_schema["required"] == ["site_url"]
    assert by_name["gsc_submit_sitemap"].input_schema["required"] == ["site_url", "sitemap_url"]
    assert by_name["gsc_delete_sitemap"].input_schema["required"] == ["site_url", "sitemap_url"]
    for tool in by_name.values():
        assert tool.input_schema["additionalProperties"] is False


def test_successful_call_is_not_an_mcp_error():
    server, client = make()
    result = call(server, "gsc_add_site", {"site_url": ALLOWED})
    assert result.is_error is False
    assert envelope(result)["applied"] is True
    assert client.calls == [("add_site", ALLOWED)]


def test_refused_property_is_an_mcp_error():
    server, client = make()
    result = call(server, "gsc_delete_site", {"site_url": "sc-domain:not-mine.example"})
    assert result.is_error is True
    assert envelope(result)["error"]["code"] == "site_not_allowed"
    assert client.calls == []


def test_unknown_argument_is_refused_by_the_transport_middleware():
    """The SDK silently drops unknown keys, so the middleware must reject them.

    ``server.call_tool`` bypasses middleware, so this drives the middleware the
    way the transport does; the stdio e2e suite asserts the same over a real
    socket.
    """
    from types import SimpleNamespace

    from search_console_writer.server import reject_unknown_arguments

    registry = build_registry(PropertyAllowlist([ALLOWED]), client_factory=RecordingClient)
    middleware = reject_unknown_arguments(registry)

    async def call_next(ctx):
        return "passed"

    ctx = SimpleNamespace(
        method="tools/call",
        params={"name": "gsc_add_site", "arguments": {"site_url": ALLOWED, "method": "POST"}},
    )
    result = anyio.run(middleware, ctx, call_next)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"]["code"] == "invalid_argument"


def test_unknown_tool_is_refused_by_the_transport_middleware():
    from types import SimpleNamespace

    from search_console_writer.server import reject_unknown_arguments

    registry = build_registry(PropertyAllowlist([ALLOWED]), client_factory=RecordingClient)
    middleware = reject_unknown_arguments(registry)

    async def call_next(ctx):
        return "passed"

    ctx = SimpleNamespace(
        method="tools/call",
        params={"name": "gsc_generic_request", "arguments": {"url": "https://evil.example"}},
    )
    result = anyio.run(middleware, ctx, call_next)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"]["code"] == "invalid_argument"


def test_middleware_passes_a_well_formed_call_through():
    from types import SimpleNamespace

    from search_console_writer.server import reject_unknown_arguments

    registry = build_registry(PropertyAllowlist([ALLOWED]), client_factory=RecordingClient)
    middleware = reject_unknown_arguments(registry)

    async def call_next(ctx):
        return "passed"

    ctx = SimpleNamespace(
        method="tools/call",
        params={"name": "gsc_add_site", "arguments": {"site_url": ALLOWED}},
    )
    assert anyio.run(middleware, ctx, call_next) == "passed"


def test_instructions_state_writes_and_delegated_approval():
    """The server must not claim it enforces per-call human approval itself."""
    from search_console_writer.server import INSTRUCTIONS

    lowered = INSTRUCTIONS.lower()
    assert "write" in lowered
    assert "approval" in lowered
    assert "gateway" in lowered or "litellm" in lowered


def test_server_identity():
    server, _ = make()
    # MCPServer name is fixed at construction; the stdio handshake test covers
    # the wire-level value, this covers the object graph.
    assert server.name == "search-console-writer"