"""End-to-end tests over the real MCP stdio transport.

Each test spawns ``python -m search_console_writer`` through the SDK's own
``stdio_client`` and drives it with a real ``ClientSession``: a genuine
``initialize`` handshake, a genuine ``tools/list``, and genuine ``tools/call``
messages on the wire, including real ``PUT`` and ``DELETE`` requests to a
loopback fake upstream.

Nothing here calls a tool function directly. The subprocess has an empty
``HOME``, no ADC file, and no service-account path, so it cannot reach a real
Google account; the only credential it sees is the loopback-gated static token.
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
    SITEMAP,
    FakeWriteUpstream,
    read_capture,
    server_env,
)

pytestmark = pytest.mark.anyio

STATIC_TOKEN = "test-token-not-a-real-secret"


@pytest.fixture
def upstream():
    with FakeWriteUpstream() as server:
        yield server


@pytest.fixture
def capture_dir(tmp_path):
    path = tmp_path / "capture"
    path.mkdir()
    return path


def params(upstream, capture_dir, **extra) -> StdioServerParameters:
    env = server_env(
        upstream,
        capture_dir,
        GSC_WRITE_TEST_STATIC_TOKEN=STATIC_TOKEN,
        **extra,
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "search_console_writer"],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def payload_of(result) -> dict:
    texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
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


async def test_initialize_reports_the_writer_identity(upstream, capture_dir):
    async def body(session, init):
        return init

    init = await with_client(upstream, capture_dir, body)
    assert init.server_info.name == "search-console-writer"
    assert init.protocol_version


async def test_tools_list_advertises_exactly_the_four_mutations(upstream, capture_dir):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, capture_dir, body)
    assert sorted(t.name for t in listing.tools) == [
        "gsc_add_site",
        "gsc_delete_site",
        "gsc_delete_sitemap",
        "gsc_submit_sitemap",
    ]
    for tool in listing.tools:
        assert tool.description and len(tool.description) > 40
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema.get("additionalProperties") is False


async def test_annotations_are_truthful_on_the_wire(upstream, capture_dir):
    async def body(session, init):
        return await session.list_tools()

    listing = await with_client(upstream, capture_dir, body)
    for tool in listing.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False, tool.name
        assert tool.annotations.idempotent_hint is True, tool.name
    by_name = {t.name: t for t in listing.tools}
    assert by_name["gsc_delete_site"].annotations.destructive_hint is True
    assert by_name["gsc_delete_sitemap"].annotations.destructive_hint is True
    assert by_name["gsc_add_site"].annotations.destructive_hint is False
    assert by_name["gsc_submit_sitemap"].annotations.destructive_hint is False


async def test_required_arguments_are_advertised(upstream, capture_dir):
    async def body(session, init):
        return await session.list_tools()

    by_name = {t.name: t for t in (await with_client(upstream, capture_dir, body)).tools}
    assert by_name["gsc_add_site"].input_schema["required"] == ["site_url"]
    assert by_name["gsc_submit_sitemap"].input_schema["required"] == ["site_url", "sitemap_url"]


# --------------------------------------------------------------------------
# Real calls: real PUT and DELETE on the wire
# --------------------------------------------------------------------------


async def test_add_site_sends_put(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_add_site", {"site_url": ALLOWED_SITE})

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is True
    assert payload["operation"] == "sites.add"
    sent = upstream.requests[-1]
    assert sent.method == "PUT"
    assert sent.path == "/webmasters/v3/sites/sc-domain%3Aexample.com"


async def test_delete_site_sends_delete(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_delete_site", {"site_url": ALLOWED_SITE})

    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["operation"] == "sites.delete"
    sent = upstream.requests[-1]
    assert sent.method == "DELETE"
    assert sent.path == "/webmasters/v3/sites/sc-domain%3Aexample.com"


async def test_submit_sitemap_sends_put_with_encoded_feedpath(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_submit_sitemap", {"site_url": ALLOWED_SITE, "sitemap_url": SITEMAP}
        )

    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["operation"] == "sitemaps.submit"
    sent = upstream.requests[-1]
    assert sent.method == "PUT"
    assert sent.path == (
        "/webmasters/v3/sites/sc-domain%3Aexample.com/sitemaps/"
        "https%3A%2F%2Fwww.example.com%2Fsitemap.xml"
    )


async def test_delete_sitemap_sends_delete(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_delete_sitemap", {"site_url": ALLOWED_SITE, "sitemap_url": SITEMAP}
        )

    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["operation"] == "sitemaps.delete"
    sent = upstream.requests[-1]
    assert sent.method == "DELETE"
    assert "/sitemaps/https%3A%2F%2Fwww.example.com%2Fsitemap.xml" in sent.path


async def test_url_prefix_property_is_encoded_on_the_wire(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_add_site", {"site_url": ALLOWED_URL_PREFIX})

    await with_client(upstream, capture_dir, body)
    assert upstream.requests[-1].path == (
        "/webmasters/v3/sites/https%3A%2F%2Fwww.example.com%2F"
    )


# --------------------------------------------------------------------------
# Refusals over the wire: no upstream request, MCP error set
# --------------------------------------------------------------------------


async def test_non_allowlisted_property_is_refused_before_any_upstream_call(
    upstream, capture_dir
):
    async def body(session, init):
        return await session.call_tool(
            "gsc_delete_site", {"site_url": "sc-domain:not-mine.example"}
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "site_not_allowed"
    assert result.is_error is True
    assert len(upstream.requests) == before


async def test_out_of_scope_sitemap_is_refused_before_any_upstream_call(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_submit_sitemap",
            {"site_url": ALLOWED_SITE, "sitemap_url": "https://evil.example/sitemap.xml"},
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["error"]["code"] == "invalid_argument"
    assert payload["error"]["details"]["reason"] == "host_out_of_scope"
    assert len(upstream.requests) == before


async def test_unknown_argument_is_refused_over_the_wire(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool(
            "gsc_add_site", {"site_url": ALLOWED_SITE, "http_method": "POST"}
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["error"]["code"] == "invalid_argument"
    assert result.is_error is True
    assert len(upstream.requests) == before


async def test_unknown_tool_is_refused_over_the_wire(upstream, capture_dir):
    async def body(session, init):
        return await session.call_tool("gsc_generic_request", {"url": "https://evil.example"})

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["error"]["code"] == "invalid_argument"
    assert len(upstream.requests) == before


async def test_upstream_403_is_mapped_and_redacted(upstream, capture_dir):
    upstream.status = 403
    upstream.error_message = (
        "Permission denied for 123456789012 "
        "https://www.googleapis.com/webmasters/v3/sites token ya29.SECRETTOKEN123456"
    )

    async def body(session, init):
        return await session.call_tool("gsc_add_site", {"site_url": ALLOWED_SITE})

    result = await with_client(upstream, capture_dir, body)
    payload = payload_of(result)
    assert payload["error"]["code"] == "unauthorized"
    text = result.content[0].text
    assert "ya29.SECRETTOKEN123456" not in text
    assert "googleapis.com" not in text
    assert "123456789012" not in text


async def test_upstream_500_is_not_retried(upstream, capture_dir):
    upstream.status = 500
    upstream.error_message = "backend failure"

    async def body(session, init):
        return await session.call_tool(
            "gsc_submit_sitemap", {"site_url": ALLOWED_SITE, "sitemap_url": SITEMAP}
        )

    before = len(upstream.requests)
    result = await with_client(upstream, capture_dir, body)
    assert payload_of(result)["error"]["code"] == "upstream_error"
    assert payload_of(result)["error"]["details"]["may_have_applied"] == "true"
    assert len(upstream.requests) - before == 1


# --------------------------------------------------------------------------
# Construction asserted from inside the server process
# --------------------------------------------------------------------------


async def test_all_four_tools_emit_only_put_and_delete(upstream, capture_dir):
    """The end-to-end verb set is exactly {PUT, DELETE}; no POST is ever sent."""

    async def body(session, init):
        await session.call_tool("gsc_add_site", {"site_url": ALLOWED_SITE})
        await session.call_tool("gsc_delete_site", {"site_url": ALLOWED_SITE})
        await session.call_tool(
            "gsc_submit_sitemap", {"site_url": ALLOWED_SITE, "sitemap_url": SITEMAP}
        )
        await session.call_tool(
            "gsc_delete_sitemap", {"site_url": ALLOWED_SITE, "sitemap_url": SITEMAP}
        )
        return True

    await with_client(upstream, capture_dir, body)
    methods = [r.method for r in upstream.requests]
    assert methods == ["PUT", "DELETE", "PUT", "DELETE"]
    assert "POST" not in methods


async def test_capture_proves_method_and_encoded_segment(upstream, capture_dir):
    async def body(session, init):
        await session.call_tool("gsc_add_site", {"site_url": ALLOWED_URL_PREFIX})
        await session.call_tool(
            "gsc_delete_sitemap", {"site_url": ALLOWED_SITE, "sitemap_url": SITEMAP}
        )
        return True

    await with_client(upstream, capture_dir, body)
    records = read_capture(capture_dir)
    assert [r["method"] for r in records] == ["PUT", "DELETE"]
    for rec in records:
        segment = rec["url"].split("/sites/", 1)[1].split("/", 1)[0]
        assert "/" not in segment
        assert ":" not in segment
        assert rec["url"].startswith("http://127.0.0.1:")


async def test_capture_is_inert_when_base_is_not_loopback(upstream, capture_dir, monkeypatch):
    """A deployment that inherits the capture variable must not write requests."""
    from search_console_writer import capture, client

    monkeypatch.delenv(client.BASE_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv(capture.CAPTURE_ENV, str(capture_dir))
    capture.record("DELETE", "https://www.googleapis.com/webmasters/v3/sites/x")
    assert not (capture_dir / "requests.jsonl").exists()


# --------------------------------------------------------------------------
# Startup fail-closed
# --------------------------------------------------------------------------


async def test_startup_exit_code_is_two_without_an_allowlist():
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "search_console_writer",
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
    assert stdout == b""