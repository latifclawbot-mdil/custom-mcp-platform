"""End-to-end tests over the real MCP stdio transport.

Each test spawns ``python -m play_console_writer`` as a subprocess via the SDK's
own ``stdio_client`` and drives it with a real ``ClientSession``: a genuine
``initialize`` handshake, ``tools/list`` and ``tools/call`` on the wire. Nothing
here calls the operation functions directly.

The upstream is an in-process fake HTTP server reached through the loopback-only
base override. The subprocess has an empty ``HOME`` and no ADC, so it cannot
reach a real Google account.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_harness import PKG, FakeUpstream, params  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def upstream():
    with FakeUpstream() as server:
        yield server


def payload_of(result) -> dict:
    texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
    assert texts, "tool result contained no text content"
    return json.loads(texts[0])


async def with_client(upstream, body, **extra):
    sp = params(upstream, **extra)
    async with stdio_client(sp) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            return await body(session, init)


# --------------------------------------------------------------------------
# Handshake and discovery
# --------------------------------------------------------------------------

async def test_initialize_reports_server_identity(upstream):
    async def body(session, init):
        return init

    init = await with_client(upstream, body)
    assert init.server_info.name == "play-console-writer"


async def test_tools_list_exposes_writes_and_the_capability_tool(upstream):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, body)
    names = {t.name for t in listing.tools}
    assert "play_listing_update" in names
    assert "play_describe_capability" in names
    assert "play_edit_commit" in names
    # No generic request tool, and no financial tool.
    assert not any("request" == n or n.endswith("_call") for n in names)
    for tool in listing.tools:
        assert tool.input_schema.get("additionalProperties") is False
        assert tool.description and len(tool.description) > 30


async def test_write_tools_are_not_marked_read_only(upstream):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, body)
    for tool in listing.tools:
        if tool.name == "play_describe_capability":
            assert tool.annotations.read_only_hint is True
        else:
            assert tool.annotations.read_only_hint is False


async def test_destructive_tools_are_marked(upstream):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, body)
    by_name = {t.name: t for t in listing.tools}
    assert by_name["play_listing_delete"].annotations.destructive_hint is True
    assert by_name["play_edit_commit"].annotations.destructive_hint is True
    assert by_name["play_listing_update"].annotations.destructive_hint is False


async def test_schemas_advertise_enums_and_bounds(upstream):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, body)
    by_name = {t.name: t for t in listing.tools}
    img = by_name["play_image_upload"].input_schema["properties"]
    assert "icon" in img["image_type"]["enum"]
    commit = by_name["play_edit_commit"].input_schema
    assert set(commit["required"]) == {"package_name", "edit_id", "confirm_edit_id"}


# --------------------------------------------------------------------------
# Real calls
# --------------------------------------------------------------------------

async def test_listing_update_round_trip(upstream):
    async def body(session, init):
        edit = payload_of(await session.call_tool("play_edit_insert", {"package_name": PKG}))
        edit_id = edit["data"]["edit_id"]
        result = await session.call_tool("play_listing_update", {
            "package_name": PKG, "edit_id": edit_id, "language": "en-US",
            "title": "Hello",
        })
        return edit_id, result

    edit_id, result = await with_client(upstream, body)
    assert result.is_error is not True
    payload = payload_of(result)
    assert payload["operation"] == "play_listing_update"
    assert payload["request"]["method_id"] == "edits.listings.update"
    # The wire saw the real path and verb.
    paths = [(r.method, r.path) for r in upstream.requests]
    assert ("PUT", f"/androidpublisher/v3/applications/{PKG}/edits/{edit_id}/listings/en-US") in paths


async def test_capability_tool_reports_gaps(upstream):
    async def body(session, init):
        return await session.call_tool("play_describe_capability", {})

    result = await with_client(upstream, body)
    payload = payload_of(result)
    caps = payload["data"]
    assert caps["financial_methods_excluded_count"] > 0
    assert caps["gates"]["release_gate_enabled"] is False
    assert any("publish" in c["capability"] for c in caps["unsupported_capabilities"])


async def test_audit_echo_names_the_write_not_the_readback(upstream):
    async def body(session, init):
        edit = payload_of(await session.call_tool("play_edit_insert", {"package_name": PKG}))
        return await session.call_tool("play_listing_update", {
            "package_name": PKG, "edit_id": edit["data"]["edit_id"],
            "language": "en-US", "title": "x",
        })

    result = await with_client(upstream, body)
    echo = payload_of(result)["request"]
    # The read-back GET runs after the PUT; the echo must name the PUT.
    assert echo["method_id"] == "edits.listings.update"
    assert echo["verb"] == "PUT"


# --------------------------------------------------------------------------
# Negative cases over the wire
# --------------------------------------------------------------------------

async def test_non_allowlisted_package_is_refused_before_upstream(upstream):
    async def body(session, init):
        return await session.call_tool("play_edit_insert", {"package_name": "com.other.app"})

    before = len(upstream.requests)
    result = await with_client(upstream, body)
    assert result.is_error is True
    assert "allowlist" in result.content[0].text
    assert len(upstream.requests) == before


async def test_unknown_argument_is_refused_over_the_wire(upstream):
    async def body(session, init):
        return await session.call_tool("play_edit_insert",
                                       {"package_name": PKG, "price": "9.99"})

    result = await with_client(upstream, body)
    assert result.is_error is True
    assert "not a recognised argument" in result.content[0].text


async def test_commit_is_refused_when_release_gate_is_off(upstream):
    async def body(session, init):
        edit = payload_of(await session.call_tool("play_edit_insert", {"package_name": PKG}))
        edit_id = edit["data"]["edit_id"]
        return await session.call_tool("play_edit_commit", {
            "package_name": PKG, "edit_id": edit_id, "confirm_edit_id": edit_id,
        })

    before = len(upstream.requests)
    result = await with_client(upstream, body)
    assert result.is_error is True
    assert "release gate" in result.content[0].text
    # Only the edit insert was sent; no commit reached the upstream.
    assert not any(r.path.endswith(":commit") for r in upstream.requests)


async def test_staging_into_a_foreign_edit_is_refused(upstream):
    async def body(session, init):
        return await session.call_tool("play_listing_update", {
            "package_name": PKG, "edit_id": "not-mine", "language": "en-US",
            "title": "x",
        })

    result = await with_client(upstream, body)
    assert result.is_error is True
    assert "was not opened by this server" in result.content[0].text


async def test_unknown_tool_name_fails(upstream):
    async def body(session, init):
        return await session.call_tool("play_call_any", {})

    result = await with_client(upstream, body)
    assert result.is_error is True


# --------------------------------------------------------------------------
# Startup fail-closed
# --------------------------------------------------------------------------

async def test_server_refuses_to_start_without_an_allowlist(upstream):
    sp = StdioServerParameters(
        command=sys.executable,
        args=["-m", "play_console_writer"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "HOME": os.environ.get("HOME", ""),
            "PLAY_WRITER_ALLOWED_PACKAGES": "",
        },
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    with pytest.raises(Exception):
        async with stdio_client(sp) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()


async def test_startup_exit_code_is_two_without_allowlist():
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "play_console_writer",
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "HOME": os.environ.get("HOME", ""),
        },
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode == 2
    assert stdout == b""