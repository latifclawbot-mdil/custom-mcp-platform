"""MCP stdio server: a thin adapter over :class:`~.tools.ToolRegistry`.

Targets **mcp 2.3.0**, where the v1 ``FastMCP`` class is ``MCPServer``
(``mcp.server.mcpserver``). Everything interesting — validation, allowlisting,
scope checks, redaction — lives in :mod:`.tools` and is testable without a
transport.

The one thing this layer *does* own that the read-only server's does not is the
per-tool MCP ``ToolAnnotations``. A write server must describe itself
truthfully: ``readOnlyHint`` is false on every tool, and ``destructiveHint`` is
true exactly on the two deletes.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations

from . import __version__
from .allowlist import PropertyAllowlist
from .auth import build_client
from .client import REQUEST_TIMEOUT_SECONDS
from .tools import ToolRegistry, ToolResult

logger = logging.getLogger("search_console_writer.server")

SERVER_NAME = "search-console-writer"

INSTRUCTIONS = """\
Write access to Google Search Console for one or more explicitly allowlisted
properties.

This server exposes four mutations, and nothing else:

- gsc_add_site        (sites.add, PUT)        - add a property to the account
- gsc_delete_site     (sites.delete, DELETE)  - remove a property from the account
- gsc_submit_sitemap  (sitemaps.submit, PUT)  - submit a sitemap
- gsc_delete_sitemap  (sitemaps.delete, DELETE) - remove a sitemap

Rules this server enforces on every call:
- the property must be in the server's configured write allowlist (there is no
  wildcard, and it is a separate allowlist from the read-only server's);
- a sitemap URL must be inside the property it is submitted to or deleted from;
- the host, scheme, and HTTP method are fixed by this server; no argument can
  change them, and there is no generic "make a request" tool;
- each call sends at most one request. A write is never retried automatically,
  because a retried mutation can duplicate or invert an unknown outcome.

Approval is NOT enforced by this server. Authorisation is delegated to the
gateway: a caller of this MCP endpoint is authorised by the LiteLLM MCP gateway
(and its configured credential and policy) before a request ever reaches here.
A human or policy approval step, if required, belongs in that gateway layer and
is not something this server can prove was obtained.
"""


def _annotations(spec: Any) -> ToolAnnotations:
    """Build truthful annotations for one tool from its spec."""
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=bool(spec.destructive),
        idempotent_hint=bool(spec.idempotent),
        open_world_hint=bool(spec.open_world),
    )


def build_registry(
    allowlist: PropertyAllowlist,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> ToolRegistry:
    """Wire a registry to a write client.

    ``client_factory`` is injectable so tests can supply a recording client;
    production passes ``None`` and gets an ADC/service-account-backed client,
    constructed lazily after validation.
    """
    if client_factory is None:
        def _default_factory() -> Any:
            return build_client(allowlist=allowlist, timeout=REQUEST_TIMEOUT_SECONDS)

        client_factory = _default_factory

    return ToolRegistry(client_factory=client_factory, allowlist=allowlist)


def create_server(
    *,
    allowlist: PropertyAllowlist | None = None,
    registry: ToolRegistry | None = None,
) -> MCPServer:
    """Build the MCP server.

    The allowlist is resolved here, at construction, so a server with no
    configured properties fails immediately instead of serving an empty
    capability set that looks healthy.
    """
    if allowlist is None:
        allowlist = PropertyAllowlist.from_env()
    if registry is None:
        registry = build_registry(allowlist)

    tools = [_build_tool(registry, spec) for spec in registry.specs()]

    return MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        tools=tools,
        middleware=[reject_unknown_arguments(registry)],
    )


def _build_tool(registry: ToolRegistry, spec: Any) -> Tool:
    """Turn one registry spec into an SDK ``Tool`` with its schema and hints."""
    tool_name = spec.name
    accepted = list(spec.accepted)

    async def _dispatch(**kwargs: Any) -> Any:
        supplied = {name: value for name, value in kwargs.items() if value is not None}
        return _render(registry.call(tool_name, supplied))

    handler = _make_handler(tool_name, accepted, _dispatch)
    tool = Tool.from_function(
        handler,
        name=tool_name,
        description=spec.description,
        annotations=_annotations(spec),
        structured_output=False,
    )
    schema = spec.input_schema()
    schema["additionalProperties"] = False
    tool.parameters = schema
    return tool


def reject_unknown_arguments(registry: ToolRegistry) -> Any:
    """Middleware that refuses unknown arguments before the SDK parses them.

    Necessary because the SDK's arg model derives its fields from the handler
    signature and silently drops any key not in it. A caller sending
    ``{"host": "https://evil.example"}`` would otherwise have the key vanish and
    the call succeed. Running at the transport layer means the rejection happens
    before tool code, before the allowlist check, and before credential
    acquisition.
    """

    async def middleware(ctx: Any, call_next: Any) -> Any:
        try:
            method = getattr(ctx, "method", None)
            params = getattr(ctx, "params", None)
        except Exception:  # pragma: no cover - context shape change
            return await call_next(ctx)

        if method == "tools/call" and isinstance(params, dict):
            name = params.get("name")
            spec = registry.get(name) if isinstance(name, str) else None
            if spec is None:
                return _mcp_error_result(
                    str(name) if name is not None else "",
                    "invalid_argument",
                    "unknown tool",
                )
            arguments = params.get("arguments")
            if isinstance(arguments, dict) and not spec.allows_extra:
                unknown = set(arguments) - set(spec.accepted)
                if unknown:
                    return _mcp_error_result(
                        str(name),
                        "invalid_argument",
                        "unknown argument(s) were supplied",
                        {"count": str(len(unknown))},
                    )
        return await call_next(ctx)

    return middleware


def _mcp_error_result(
    tool_name: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> Any:
    from mcp.types import CallToolResult, TextContent

    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    text = json.dumps({"tool": tool_name, "ok": False, "error": error})
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _make_handler(
    tool_name: str,
    accepted: list[str],
    dispatch: Callable[..., Any],
) -> Callable[..., Any]:
    """Build an async handler with ``accepted`` as its literal parameter names."""
    if not accepted:
        async def handler():
            return await dispatch()

        handler.__name__ = tool_name
        return handler

    params = ", ".join(f"{name}: Any = None" for name in accepted)
    source = (
        f"async def handler({params}):\n"
        f"    return await _dispatch({_call_args(accepted)})\n"
    )
    namespace: dict[str, Any] = {"_dispatch": dispatch, "Any": Any}
    for name in accepted:
        if not name.isidentifier():
            raise ValueError(f"invalid argument name in tool spec: {name!r}")
    exec(compile(source, f"<tool:{tool_name}>", "exec"), namespace)
    handler = namespace["handler"]
    handler.__name__ = tool_name
    return handler


def _call_args(accepted: list[str]) -> str:
    return ", ".join(f"{name}={name}" for name in accepted)


def _render(result: ToolResult) -> Any:
    """Render a tool result as an MCP ``CallToolResult``.

    The envelope carries a structured ``ok: false`` error, and the MCP
    ``isError`` flag is set from the same decision, so the two channels agree.
    """
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=result.text)],
        is_error=result.is_error,
    )


def main() -> None:  # pragma: no cover - exercised via subprocess in tests
    from .__main__ import main as _main

    _main()