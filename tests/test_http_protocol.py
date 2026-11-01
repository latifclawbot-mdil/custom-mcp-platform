"""The real MCP protocol client test over Streamable HTTP.

This uses the SDK's own ``ClientSession`` + ``streamable_http_client`` — not
hand-rolled JSON-RPC — so it proves the servers are usable by the client that
LiteLLM itself uses. Three cases, each against a freshly spawned process:

1. authenticated session completes ``initialize`` and ``tools/list``;
2. a wrong token fails the handshake with HTTP 401, and the token never
   appears in the server's log;
3. a policy refusal (package/site outside the allowlist) still returns the
   server's own structured error envelope over HTTP — the stdio behaviour is
   preserved, not replaced.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from harness import (
    REPO_ROOT,
    SERVERS,
    TEST_TOKEN,
    TRANSPORT_SRC,
    free_port,
    server_env,
    wait_for_port,
)

pytestmark = pytest.mark.anyio

PLAY = {
    "name": "play-console",
    "module": "play_console_mcp.http_main",
    "env": {
        "PLAY_CONSOLE_ALLOWED_PACKAGES": "com.example.app",
        "PYTHONPATH": f"{SERVERS / 'play-console' / 'src'}:{TRANSPORT_SRC}",
        "PLAY_CONSOLE_REQUEST_TIMEOUT": "5",
    },
    "refusal_tool": "list_reviews",
    "refusal_args": {"package_name": "com.not.allowlisted"},
    "refusal_needle": "allowlist",
}

SEARCH = {
    "name": "search-console",
    "module": "search_console_mcp.http_main",
    "env": {
        "GSC_ALLOWED_SITES": "sc-domain:example.com",
        "PYTHONPATH": f"{SERVERS / 'search-console' / 'src'}:{TRANSPORT_SRC}",
    },
    "refusal_tool": "gsc_search_analytics",
    "refusal_args": {
        "site_url": "sc-domain:not-allowlisted.example",
        "start_date": "2026-10-01",
        "end_date": "2026-10-02",
    },
    "refusal_needle": "allowlist",
}

SPECS = [PLAY, SEARCH]


@pytest.fixture(params=SPECS, ids=[spec["name"] for spec in SPECS])
def spec(request):
    return dict(request.param)


@contextlib.contextmanager
def spawn(spec, port: int, tmp_path: Path, **overrides):
    env = server_env(
        {
            "MCP_HTTP_AUTH_TOKEN": TEST_TOKEN,
            "MCP_HTTP_PORT": str(port),
            "MCP_HTTP_HOST": "127.0.0.1",
            **spec["env"],
        },
        **overrides,
    )
    log_path = Path(tmp_path) / f"{spec['name']}-{port}.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", spec["module"]],
            env=env,
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        with wait_for_port(port):
            yield proc, log_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def http_client(token: str):
    import httpx

    return httpx.AsyncClient(headers={"authorization": f"Bearer {token}"}, timeout=30.0)


async def test_client_session_initializes_and_lists_tools(spec, tmp_path):
    port = free_port()
    with spawn(spec, port, tmp_path) as (_, log_path):
        url = f"http://127.0.0.1:{port}/mcp"
        async with http_client(TEST_TOKEN) as client:
            async with streamable_http_client(url, http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    assert init.server_info.name
                    tools = await session.list_tools()
                    assert len(tools.tools) >= 1
                    assert {tool.name for tool in tools.tools}
        assert "Traceback" not in read_log(log_path)


async def test_wrong_token_fails_the_handshake_and_never_reaches_tools(spec, tmp_path):
    """Wrong token: 401 on the wire, and the SDK client cannot connect.

    Asserted in two places because they prove different things. The raw request
    shows the exact status the server returned; the SDK session shows that a
    real MCP client surfaces the failure instead of getting a tool list.
    """
    import httpx

    port = free_port()
    with spawn(spec, port, tmp_path) as (_, log_path):
        url = f"http://127.0.0.1:{port}/mcp"

        raw = httpx.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "wrong-token-probe", "version": "0"},
                },
            },
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "authorization": "Bearer wrong-token-000000000000",
            },
            timeout=30.0,
        )
        assert raw.status_code == 401, raw.text
        assert "serverInfo" not in raw.text

        async with http_client("wrong-token-000000000000") as client:
            with pytest.raises(BaseExceptionGroup) as excinfo:
                async with streamable_http_client(url, http_client=client) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
            # Unwrap the task group and assert the client failed for an
            # MCP-level reason rather than succeeding with an empty session.
            leaves = _leaf_exceptions(excinfo.value)
            message = " | ".join(f"{type(exc).__name__}: {exc}" for exc in leaves)
            assert "error" in message.lower(), message
        assert TEST_TOKEN not in read_log(log_path)


def _leaf_exceptions(error: BaseException) -> list[BaseException]:
    """Flatten an exception group into its leaf exceptions."""
    if isinstance(error, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in error.exceptions:
            leaves.extend(_leaf_exceptions(sub))
        return leaves
    return [error]


async def test_policy_refusal_is_preserved_over_http(spec, tmp_path):
    port = free_port()
    with spawn(spec, port, tmp_path) as (_, log_path):
        url = f"http://127.0.0.1:{port}/mcp"
        async with http_client(TEST_TOKEN) as client:
            async with streamable_http_client(url, http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        spec["refusal_tool"], dict(spec["refusal_args"])
                    )
                    assert result.is_error is True
                    text = result.content[0].text
                    assert spec["refusal_needle"] in text.lower(), text
        assert "Traceback" not in read_log(log_path)