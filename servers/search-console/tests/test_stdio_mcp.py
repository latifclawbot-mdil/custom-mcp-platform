"""End-to-end tests over the real MCP stdio transport.

Each test spawns ``python -m search_console_mcp`` as a subprocess via the SDK's
own ``stdio_client`` and drives it with a real ``ClientSession``: a genuine
``initialize`` handshake, a genuine ``tools/list``, and genuine ``tools/call``
messages on the wire. Nothing in this file calls the tool functions directly.

The upstream is an in-process fake HTTP server, reached through the loopback-only
base override. The subprocess has an empty ``HOME`` and no
``GOOGLE_APPLICATION_CREDENTIALS``, so it cannot reach a real Google account.
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

from mcp_harness import (  # noqa: E402
    ALLOWED_SITE,
    ALLOWED_URL_PREFIX,
    FakeUpstream,
    read_capture,
    server_env,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def upstream():
    with FakeUpstream() as server:
        yield server


@pytest.fixture
def capture_dir(tmp_path):
    path = tmp_path / "capture"
    path.mkdir()
    return path


def params(upstream, capture_dir, **extra) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "search_console_mcp"],
        env={
            **server_env(
                upstream,
                capture_dir,
                GSC_TEST_STATIC_TOKEN="test-token-not-a-real-secret",
                **extra,
            )
        },
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def payload_of(result) -> dict:
    """Extract the JSON envelope from a CallToolResult."""
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    assert texts, "tool result contained no text content"
    return json.loads(texts[0])


async def with_client(upstream, capture_dir, body, **extra):
    sp = params(upstream, capture_dir, **extra)
    async with stdio_client(sp) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            return await body(session, init)


# --------------------------------------------------------------------------
# Handshake and discovery
# --------------------------------------------------------------------------


async def test_initialize_handshake_reports_server_identity(upstream, capture_dir):
    async def body(session, init):
        return init

    init = await with_client(upstream, capture_dir, body)
    assert init.server_info.name == "search-console"
    assert init.protocol_version


async def test_tools_list_advertises_exactly_the_read_only_surface(upstream, capture_dir):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, capture_dir, body)
    names = sorted(tool.name for tool in listing.tools)
    assert names == [
        "gsc_inspect_url",
        "gsc_list_sitemaps",
        "gsc_list_sites",
        "gsc_search_analytics",
    ]
    for tool in listing.tools:
        assert tool.description and len(tool.description) > 40
        assert tool.input_schema["type"] == "object"
        # Fail-closed contract: no free-form additional properties.
        assert tool.input_schema.get("additionalProperties") is False


async def test_tools_list_marks_tools_read_only(upstream, capture_dir):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, capture_dir, body)
    for tool in listing.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


async def test_required_arguments_are_advertised(upstream, capture_dir):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, capture_dir, body)
    by_name = {tool.name: tool for tool in listing.tools}
    assert by_name["gsc_search_analytics"].input_schema["required"] == [
        "site_url",
        "start_date",
        "end_date",
    ]
    assert by_name["gsc_inspect_url"].input_schema["required"] == ["site_url", "url"]
    assert by_name["gsc_list_sites"].input_schema["required"] == []


# --------------------------------------------------------------------------
# Real calls, real request construction
# --------------------------------------------------------------------------


async def test_list_sites_round_trip(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_list_sites", {})

    result = await with_client(upstream, capture_dir, body)
    assert result.is_error is not True
    payload = payload_of(result)
    assert payload["ok"] is True
    urls = [s["site_url"] for s in payload["sites"]]
    assert ALLOWED_SITE in urls
    # Every returned site is allowlisted; non-allowlisted properties are counted
    # but never named.
    assert all(s["allowlisted"] is True for s in payload["sites"])
    assert "not-allowlisted.example" not in result.content[0].text
    assert payload["sites_withheld"] == 1
    assert upstream.requests[-1].method == "GET"
    assert upstream.requests[-1].path.endswith("/sites")


async def test_search_analytics_round_trip(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_search_analytics",
            {
                "site_url": ALLOWED_SITE,
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "dimensions": ["query"],
                "row_limit": 50,
            },
        )

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is True
    assert payload["rows"][0]["keys"] == ["gsc api"]
    assert payload["rows_returned"] == 1

    sent = upstream.requests[-1]
    assert sent.method == "POST"
    # Property encoding is asserted on the wire: the colon is escaped and no raw
    # slash appears in the property position.
    assert "/sites/sc-domain%3Aexample.com/searchAnalytics/query" in sent.path
    assert sent.body["startDate"] == "2026-10-01"
    assert sent.body["rowLimit"] == 50
    assert sent.body["dimensions"] == ["query"]


async def test_url_prefix_property_encoding_on_the_wire(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_search_analytics",
            {
                "site_url": ALLOWED_URL_PREFIX,
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
            },
        )

    await with_client(upstream, capture_dir, body)
    sent = upstream.requests[-1]
    assert (
        "/sites/https%3A%2F%2Fwww.example.com%2F/searchAnalytics/query" in sent.path
    )


async def test_sitemaps_round_trip(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_list_sitemaps", {"site_url": ALLOWED_SITE})

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["sitemaps"][0]["path"] == "https://example.com/sitemap.xml"
    assert payload["sitemaps"][0]["contents"] == [
        {"type": "web", "submitted": 10, "indexed": 9}
    ]
    assert upstream.requests[-1].method == "GET"
    assert "/sites/sc-domain%3Aexample.com/sitemaps" in upstream.requests[-1].path


async def test_inspect_url_round_trip(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_inspect_url",
            {"site_url": ALLOWED_SITE, "url": "https://example.com/pricing"},
        )

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["inspection"]["verdict"] == "PASS"
    assert payload["inspection"]["coverage_state"] == "Submitted and indexed"
    sent = upstream.requests[-1]
    assert sent.path == "/v1/urlInspection/index:inspect"
    assert sent.body == {
        "inspectionUrl": "https://example.com/pricing",
        "siteUrl": "sc-domain:example.com",
        "languageCode": "en-US",
    }


# --------------------------------------------------------------------------
# Negative cases, over the wire
# --------------------------------------------------------------------------


async def test_non_allowlisted_property_is_refused_before_any_upstream_call(
    upstream, capture_dir
):
    async def body(session, init):
        return await session.call_tool(
            "gsc_search_analytics",
            {
                "site_url": "sc-domain:not-allowlisted.example",
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
            },
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "site_not_allowed"
    # Nothing was sent upstream for the rejected call.
    assert len(upstream.requests) == before


async def test_property_never_matches_by_prefix_or_case(upstream, capture_dir):
    async def body(session, init):
        out = []
        for candidate in (
            "sc-domain:example.com.evil.com",
            "sc-domain:example.com/extra",
            "https://www.example.com.evil.com/",
            "https://example.com/",
        ):
            r = await session.call_tool(
                "gsc_list_sitemaps", {"site_url": candidate}
            )
            out.append(payload_of(r)["error"]["code"])
        return out

    codes = await with_client(upstream, capture_dir, body)
    assert codes == ["site_not_allowed"] * 4


async def test_out_of_scope_inspection_url_is_refused(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_inspect_url",
            {"site_url": ALLOWED_SITE, "url": "https://not-example.com/pricing"},
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert len(upstream.requests) == before


async def test_credentialed_url_is_refused(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_inspect_url",
            {"site_url": ALLOWED_SITE, "url": "https://user:pw@example.com/"},
        )

    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["error"]["code"] == "invalid_argument"


async def test_path_traversal_inspection_url_is_refused(upstream, capture_dir):
    """A ``..`` segment must not be treated as "under" the property.

    The URL used to pass the raw string prefix check while resolving (per RFC
    3986) to a different path, so the inspection target escaped the property's
    scope. It must be refused before any request is built.
    """

    async def body(session, init):
        return await session.call_tool(
            "gsc_inspect_url",
            {"site_url": ALLOWED_SITE, "url": "https://example.com/../admin"},
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert payload["error"]["details"]["reason"] == "path_traversal"
    assert len(upstream.requests) == before


async def test_encoded_path_traversal_inspection_url_is_refused(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_inspect_url",
            {"site_url": ALLOWED_SITE, "url": "https://example.com/a/%2e%2e/b"},
        )

    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["error"]["details"]["reason"] == "path_traversal"


async def test_rejected_action_sets_the_mcp_error_flag(upstream, capture_dir):
    """``isError`` must be true for a refused action, not just a transport error.

    The envelope already said ``ok: false``; the MCP flag said otherwise, so a
    client that branches on ``isError`` saw a refused property as a success.
    """

    async def body(session, init):
        return await session.call_tool(
            "gsc_search_analytics",
            {
                "site_url": "sc-domain:not-allowlisted.example",
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
            },
        )

    result = await with_client(upstream, capture_dir, body)
    assert result.is_error is True
    assert payload_of(result)["error"]["code"] == "site_not_allowed"


async def test_successful_call_does_not_set_the_mcp_error_flag(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_list_sites", {})

    result = await with_client(upstream, capture_dir, body)
    assert result.is_error is False


async def test_unknown_argument_is_refused_over_the_wire(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_list_sites", {"host": "https://evil.example"}
        )

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


async def test_out_of_range_row_limit_is_refused(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_search_analytics",
            {
                "site_url": ALLOWED_SITE,
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "row_limit": 100000,
            },
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["error"]["code"] == "invalid_argument"
    assert len(upstream.requests) == before


async def test_reversed_date_range_is_refused(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_search_analytics",
            {
                "site_url": ALLOWED_SITE,
                "start_date": "2026-10-31",
                "end_date": "2026-10-01",
            },
        )

    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["error"]["code"] == "invalid_argument"


async def test_unknown_tool_name_is_refused(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_delete_site", {"site_url": ALLOWED_SITE})

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"


async def test_upstream_403_is_mapped_and_redacted(upstream, capture_dir):
    upstream.responses["/sites"] = (
        403,
        {
            "error": {
                "message": (
                    "Permission denied for 123456789012 "
                    "https://www.googleapis.com/webmasters/v3/sites token ya29.SECRETTOKEN123456"
                ),
                "code": 403,
            }
        },
    )

    async def body(session, init):
        return await session.call_tool("gsc_list_sites", {})

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["error"]["code"] == "unauthorized"
    text = result.content[0].text
    assert "ya29.SECRETTOKEN123456" not in text
    assert "googleapis.com" not in text
    assert "123456789012" not in text


# --------------------------------------------------------------------------
# Request construction asserted from inside the server process
# --------------------------------------------------------------------------


async def test_capture_proves_property_is_a_single_escaped_segment(
    upstream, capture_dir
):
    """The server's own record of its request, not a reconstruction by the test."""

    async def body(session, init):
        await session.call_tool(
            "gsc_search_analytics",
            {
                "site_url": ALLOWED_URL_PREFIX,
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
            },
        )
        await session.call_tool("gsc_list_sitemaps", {"site_url": ALLOWED_SITE})
        return True

    await with_client(upstream, capture_dir, body)
    records = read_capture(capture_dir)
    assert len(records) == 2
    for rec in records:
        url = rec["url"]
        segment = url.split("/sites/", 1)[1].split("/", 1)[0]
        # Exactly one path segment, fully escaped, no raw slash or colon.
        assert "/" not in segment
        assert ":" not in segment
        assert "%3A" in segment


async def test_capture_proves_host_is_fixed(upstream, capture_dir):
    async def body(session, init):
        await session.call_tool("gsc_list_sites", {})
        return True

    await with_client(upstream, capture_dir, body)
    records = read_capture(capture_dir)
    assert records
    assert all(
        rec["url"].startswith("http://127.0.0.1:") for rec in records
    ), "requests must go to the overridden loopback base"


async def test_only_get_and_read_only_post_are_used(upstream, capture_dir):
    async def body(session, init):
        await session.call_tool("gsc_list_sites", {})
        await session.call_tool("gsc_list_sitemaps", {"site_url": ALLOWED_SITE})
        await session.call_tool(
            "gsc_inspect_url",
            {"site_url": ALLOWED_SITE, "url": "https://example.com/"},
        )
        return True

    await with_client(upstream, capture_dir, body)
    methods = [r.method for r in upstream.requests]
    assert methods == ["GET", "GET", "POST"]


# --------------------------------------------------------------------------
# Startup fail-closed behaviour
# --------------------------------------------------------------------------


async def test_server_refuses_to_start_without_an_allowlist(upstream):
    sp = StdioServerParameters(
        command=sys.executable,
        args=["-m", "search_console_mcp"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "HOME": os.environ.get("HOME", ""),
            "GSC_ALLOWED_SITES": "",
        },
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    with pytest.raises(Exception):
        async with stdio_client(sp) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()


async def test_startup_exit_code_is_non_zero_without_allowlist():
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "search_console_mcp",
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
    assert b"refusing to start" in stderr
    # Nothing on stdout: the protocol channel must stay clean even on failure.
    assert stdout == b""