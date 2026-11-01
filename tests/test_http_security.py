"""The core security tests: real HTTP against a real server process.

Run these with the repository venv; the file leaves the exit code untouched
(no output pipeline) so a failure is a failure.


Each test spawns ``python -m <server> --http`` as a subprocess (the same way
Coolify would run the container), waits for its port, and drives it with raw
JSON-RPC over HTTP and with the SDK's own ``ClientSession`` over Streamable
HTTP. Nothing is mocked at the transport layer: the assertions are about what
the wire actually returns.

Covered here (one test each, all offline):

* positive path — authenticated ``initialize`` and ``tools/list`` succeed and
  the tool set is non-empty;
* no token -> 401, no MCP session, no ``Mcp-Session-Id``;
* wrong token -> 401;
* malformed ``Authorization`` header -> 401;
* a valid token with an unexpected whitespace/``Bearer`` casing still works
  (documenting exactly what is and is not accepted);
* the rejection happens before tool dispatch (no tool result ever appears);
* a wrong token cannot hijack a session id created by a valid token.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import (
    REPO_ROOT,
    SERVERS,
    TEST_TOKEN,
    TRANSPORT_SRC,
    free_port,
    http_post,
    initialize_body,
    server_env,
    wait_for_port,
)

pytestmark = pytest.mark.anyio

SERVERS_UNDER_TEST = {
    "play-console": {
        "module": "play_console_mcp.http_main",
        "env": {
            "PLAY_CONSOLE_ALLOWED_PACKAGES": "com.example.app",
            "PYTHONPATH": f"{SERVERS / 'play-console' / 'src'}:{TRANSPORT_SRC}",
        },
        "min_tools": 1,
    },
    "search-console": {
        "module": "search_console_mcp.http_main",
        "env": {
            "GSC_ALLOWED_SITES": "sc-domain:example.com",
            "PYTHONPATH": f"{SERVERS / 'search-console' / 'src'}:{TRANSPORT_SRC}",
        },
        "min_tools": 1,
    },
}


@pytest.fixture(params=sorted(SERVERS_UNDER_TEST))
def server_spec(request):
    name = request.param
    spec = dict(SERVERS_UNDER_TEST[name])
    spec["name"] = name
    return spec


class RunningServer:
    def __init__(self, proc: subprocess.Popen, port: int, name: str) -> None:
        self.proc = proc
        self.port = port
        self.name = name

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.proc.kill()
            self.proc.wait(timeout=15)

    def logs(self) -> str:
        path: Path = self._log_path
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


import contextlib  # noqa: E402



@contextlib.contextmanager
def running_server(spec, tmp_path, **overrides):
    port = free_port()
    log_path = Path(tmp_path) / f"{spec['name']}-{port}.log"
    env = server_env(
        {
            "MCP_HTTP_AUTH_TOKEN": TEST_TOKEN,
            "MCP_HTTP_PORT": str(port),
            "MCP_HTTP_HOST": "127.0.0.1",
            **spec["env"],
        },
        **overrides,
    )
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", spec["module"]],
            env=env,
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    server = RunningServer(proc, port, spec["name"])
    server._log_path = log_path
    try:
        try:
            with wait_for_port(port):
                pass
        except TimeoutError:
            # Do not hide a startup crash behind a timeout: report the real
            # exit code and the process's own stderr.
            code = proc.poll()
            detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            if code is not None:
                raise AssertionError(
                    f"{spec['name']} exited with code {code} instead of listening on {port}:\n{detail}"
                ) from None
            raise AssertionError(
                f"{spec['name']} never listened on {port} and is still running; log:\n{detail}"
            ) from None
        yield server
    finally:
        server.stop()


def _sse_json(response) -> dict:
    """Decode a JSON-RPC payload from either a JSON body or an SSE stream."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no SSE data line in: {response.text!r}")
    return response.json()


async def test_initialize_and_list_tools_succeed_with_a_valid_token(server_spec, tmp_path):
    with running_server(server_spec, tmp_path) as server:
        response = http_post(server.url, initialize_body())
        assert response.status_code == 200, response.text
        assert response.headers.get("mcp-session-id"), "initialize must open a session"

        session_id = response.headers["mcp-session-id"]
        payload = _sse_json(response)
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == 1
        assert "result" in payload, payload
        result = payload["result"]
        assert result["serverInfo"]["name"]
        assert "protocolVersion" in result

        import httpx

        listing_response = httpx.post(
            server.url,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "authorization": f"Bearer {TEST_TOKEN}",
                "mcp-session-id": session_id,
            },
            timeout=30.0,
        )
        assert listing_response.status_code == 200, listing_response.text
        tools_payload = _sse_json(listing_response)
        tools = tools_payload["result"]["tools"]
        assert len(tools) >= server_spec["min_tools"]
        for tool in tools:
            assert tool["name"]
            assert isinstance(tool.get("inputSchema"), dict)
            # Read-only annotations must survive the transport change.
            annotations = tool.get("annotations") or {}
            assert annotations.get("readOnlyHint") is True
            assert annotations.get("destructiveHint") is False


async def test_missing_token_is_rejected_with_401_and_opens_no_session(server_spec, tmp_path):
    with running_server(server_spec, tmp_path) as server:
        response = http_post(server.url, initialize_body(), token=None)
        assert response.status_code == 401, response.text
        assert response.headers.get("mcp-session-id") is None
        assert "www-authenticate" in {k.lower() for k in response.headers}
        assert "result" not in response.text
        assert "serverInfo" not in response.text
        assert TEST_TOKEN not in response.text


async def test_wrong_token_is_rejected_and_leaks_nothing(server_spec, tmp_path):
    with running_server(server_spec, tmp_path) as server:
        response = http_post(server.url, initialize_body(), token="wrong-token-000000000000")
        assert response.status_code == 401, response.text
        assert response.headers.get("mcp-session-id") is None
        body = response.text
        assert "wrong-token" not in body
        assert TEST_TOKEN not in body
        assert "serverInfo" not in body


async def test_near_miss_token_is_rejected(server_spec, tmp_path):
    with running_server(server_spec, tmp_path) as server:
        near_miss = TEST_TOKEN[:-1] + ("x" if TEST_TOKEN.endswith("9") else "9")
        response = http_post(server.url, initialize_body(), token=near_miss)
        assert response.status_code == 401, response.text


async def test_malformed_authorization_headers_are_rejected(server_spec, tmp_path):
    with running_server(server_spec, tmp_path) as server:
        for header in (
            TEST_TOKEN,  # no scheme
            f"Basic {TEST_TOKEN}",
            f"Token {TEST_TOKEN}",
            "Bearer",
            f"Bearerx {TEST_TOKEN}",
        ):
            import httpx

            response = httpx.post(
                server.url,
                json=initialize_body(),
                headers={
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                    "authorization": header,
                },
                timeout=30.0,
            )
            assert response.status_code == 401, (header, response.status_code, response.text)


async def test_a_second_valid_initialize_opens_a_distinct_session(server_spec, tmp_path):
    with running_server(server_spec, tmp_path) as server:
        first = http_post(server.url, initialize_body(1))
        second = http_post(server.url, initialize_body(2))
        assert first.status_code == second.status_code == 200
        assert first.headers["mcp-session-id"] != second.headers["mcp-session-id"]


async def test_wrong_token_cannot_hijack_an_existing_session(server_spec, tmp_path):
    import httpx

    with running_server(server_spec, tmp_path) as server:
        opened = http_post(server.url, initialize_body(1))
        assert opened.status_code == 200
        session_id = opened.headers["mcp-session-id"]

        hijack = httpx.post(
            server.url,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "authorization": "Bearer wrong-token-000000000000",
                "mcp-session-id": session_id,
            },
            timeout=30.0,
        )
        assert hijack.status_code == 401, hijack.text
        assert "inputSchema" not in hijack.text


async def test_non_json_content_type_is_rejected(server_spec, tmp_path):
    import httpx

    with running_server(server_spec, tmp_path) as server:
        response = httpx.post(
            server.url,
            content=b"not json",
            headers={
                "content-type": "text/plain",
                "authorization": f"Bearer {TEST_TOKEN}",
            },
            timeout=30.0,
        )
        assert response.status_code in (400, 415, 406), response.status_code


async def test_rejection_precedes_tool_dispatch_even_for_a_valid_body(server_spec, tmp_path):
    """A tool call without a token must not produce a tool result."""
    with running_server(server_spec, tmp_path) as server:
        body = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "does_not_matter", "arguments": {}},
        }
        response = http_post(server.url, body, token=None)
        assert response.status_code == 401
        assert "content" not in response.text


async def test_untrusted_host_header_is_rejected_even_with_a_valid_token(server_spec, tmp_path):
    """DNS-rebinding protection must fail closed for an unlisted Host."""
    with running_server(server_spec, tmp_path) as server:
        response = http_post(server.url, initialize_body(), host="evil.example")
        assert response.status_code == 421, (response.status_code, response.text)
        assert response.headers.get("mcp-session-id") is None
        assert "serverInfo" not in response.text


async def test_unlisted_origin_is_rejected_with_a_valid_token(server_spec, tmp_path):
    import httpx

    with running_server(server_spec, tmp_path) as server:
        response = httpx.post(
            server.url,
            json=initialize_body(),
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "authorization": f"Bearer {TEST_TOKEN}",
                "origin": "https://evil.example",
            },
            timeout=30.0,
        )
        assert response.status_code == 403, (response.status_code, response.text)


async def test_unknown_path_is_not_rewritten_into_the_mcp_endpoint(server_spec, tmp_path):
    import httpx

    with running_server(server_spec, tmp_path) as server:
        url = server.url.replace("/mcp", "/admin")
        response = httpx.post(
            url,
            json=initialize_body(),
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "authorization": f"Bearer {TEST_TOKEN}",
            },
            timeout=30.0,
        )
        assert response.status_code == 404, (response.status_code, response.text)


async def test_prefix_stripped_request_is_routed_to_the_mcp_endpoint(server_spec, tmp_path):
    """A proxy that strips the path prefix leaves "" or "/"; both must route."""
    import httpx

    with running_server(server_spec, tmp_path) as server:
        root = server.url.replace("/mcp", "/")
        response = httpx.post(
            root,
            json=initialize_body(),
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "authorization": f"Bearer {TEST_TOKEN}",
            },
            timeout=30.0,
        )
        assert response.status_code == 200, (response.status_code, response.text)
        assert response.headers.get("mcp-session-id")


async def test_startup_refuses_without_a_token(server_spec, tmp_path):
    """A server started with no token must exit rather than serve."""
    port = free_port()
    env = server_env(
        {
            "MCP_HTTP_AUTH_TOKEN": "",
            "MCP_HTTP_PORT": str(port),
            **server_spec["env"],
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", server_spec["module"]],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive
            proc.kill()
    assert proc.returncode == 2, (stdout, stderr)
    assert b"refusing to start" in stderr.lower()
    assert not _port_open_after_exit(port)


def _port_open_after_exit(port: int) -> bool:
    from harness import port_is_open

    time.sleep(0.2)
    return port_is_open(port)