"""MCP-level tests through the real server object (no socket, no Logto)."""
from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from logto_ops.confirm import ConfirmationVerifier
from logto_ops.gate import load_gate
from logto_ops.server import build_server
from logto_ops.settings import load_settings


class FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self._settings = type("S", (), {"app_id": "app-self", "base_url": "https://logto.test"})()

    def request(self, method, path, *, params=None, json_body=None, operation, idempotent=True):
        self.calls.append((method, path, json_body))
        if method == "POST" and path == "/applications":
            return {"id": "new-app", "type": jwt_type(json_body), "name": (json_body or {}).get("name")}
        return {"id": "x"}

    def list_page(self, path, *, page=1, page_size=None, params=None, operation):
        self.calls.append(("GET", path, None))
        return []


def jwt_type(body):
    return (body or {}).get("type")


def make_server(tmp_path, *, mode="apply", fake=None):
    secret = tmp_path / "m2m"
    secret.write_text("client-secret-value", encoding="utf-8")
    secret.chmod(0o600)
    settings = load_settings(
        {
            "LOGTO_BASE_URL": "https://logto.test",
            "LOGTO_M2M_APP_ID": "app-self",
            "LOGTO_M2M_SECRET_FILE": str(secret),
        }
    )
    client = fake or FakeClient()
    server = build_server(
        settings=settings,
        gate=load_gate({"LOGTO_MCP_MODE": mode}),
        verifier=ConfirmationVerifier(secret=b"k" * 32),
        client_factory=lambda _settings: client,
    )
    return server, client


async def call(server, name: str, arguments: dict):
    result = await server.call_tool(name, arguments)
    if isinstance(result, CallToolResult):
        return result.structured_content or {}, result.is_error
    # Some SDK versions return (content, structured) tuples.
    return result, False


async def test_read_tool_is_executed_immediately(tmp_path):
    server, client = make_server(tmp_path)
    payload, is_error = await call(server, "list_applications", {"page": 1})
    assert is_error in (False, None)
    assert client.calls == [("GET", "/applications", None)]


async def test_write_without_a_token_returns_a_plan_and_does_not_execute(tmp_path):
    server, client = make_server(tmp_path)
    payload, is_error = await call(server, "create_m2m_application", {"name": "scheduler"})
    assert is_error is True
    assert payload["error"]["code"] == "confirmation_required"
    assert payload["plan"]["operation"] == "create_m2m_application"
    assert payload["confirmation_token"]
    assert client.calls == [], "a gated write must not reach Logto"


async def test_dry_run_returns_a_plan_and_a_token(tmp_path):
    server, client = make_server(tmp_path)
    payload, is_error = await call(
        server, "create_m2m_application", {"name": "scheduler", "dry_run": True}
    )
    assert is_error in (False, None)
    assert payload["plan"]["will_execute"] is False
    assert "confirmation_token" in payload
    assert client.calls == []


async def test_plan_then_apply_executes_exactly_once(tmp_path):
    server, client = make_server(tmp_path)
    plan, _ = await call(server, "create_m2m_application", {"name": "scheduler", "dry_run": True})
    payload, is_error = await call(
        server,
        "create_m2m_application",
        {"name": "scheduler", "confirmation_token": plan["confirmation_token"]},
    )
    assert is_error in (False, None)
    assert payload["id"] == "new-app"
    posts = [c for c in client.calls if c[0] == "POST"]
    assert len(posts) == 1


async def test_apply_with_changed_arguments_is_refused(tmp_path):
    server, client = make_server(tmp_path)
    plan, _ = await call(server, "create_m2m_application", {"name": "scheduler", "dry_run": True})
    payload, is_error = await call(
        server,
        "create_m2m_application",
        {"name": "different-name", "confirmation_token": plan["confirmation_token"]},
    )
    assert is_error is True
    assert payload["error"]["code"] == "confirmation_mismatch"
    assert client.calls == []


async def test_read_mode_refuses_a_write_outright(tmp_path):
    server, client = make_server(tmp_path, mode="read")
    payload, is_error = await call(
        server, "create_m2m_application", {"name": "x", "dry_run": True}
    )
    assert is_error is True
    assert payload["error"]["code"] == "write_not_permitted"
    assert client.calls == []


async def test_unknown_argument_is_refused_at_validation_and_never_reaches_logto(tmp_path):
    # The SDK's argument model is built from the generated signature and would
    # otherwise *drop* an undeclared key, so `create_m2m_application(type=...)`
    # would succeed while creating a different type. The tool sets
    # extra="forbid" on that model so validation refuses instead.
    server, client = make_server(tmp_path)
    with pytest.raises(ToolError):
        await server.call_tool("list_applications", {"totally": "wrong"})
    assert client.calls == [], "an undeclared argument must not trigger a Logto call"


async def test_every_advertised_schema_forbids_extra_properties(tmp_path):
    server, _ = make_server(tmp_path)
    tools = await server.list_tools()
    assert tools
    for tool in tools:
        assert tool.input_schema.get("additionalProperties") is False, tool.name


async def test_no_delete_tool_is_advertised(tmp_path):
    server, _ = make_server(tmp_path)
    names = {tool.name for tool in await server.list_tools()}
    assert "create_m2m_application" in names
    assert "create_application_secret" in names
    assert not [name for name in names if name.startswith("delete_")]
