"""MCP stdio server: a thin adapter over :class:`~.tools.ToolRegistry`.

The MCP SDK version matters here. This targets **mcp 2.3.0**, where the v1
``FastMCP`` class was renamed to ``MCPServer`` (``mcp.server.mcpserver``) and the
old ``mcp.server.fastmcp`` module raises on import by design. The v1 spellings
are deliberately not used anywhere.

Everything interesting — validation, allowlisting, bounds, redaction — lives in
:mod:`.tools` and is testable without a transport. This module only:

* pins the server to stdio (this process is meant to be spawned by an MCP
  client such as a LiteLLM gateway, not to listen on a socket);
* registers each tool with its JSON schema and a no-argument passthrough;
* converts a :class:`ToolResult` into MCP content.

Note the tool callbacks take ``**kwargs`` at the SDK boundary only. The SDK has
already coerced arguments to the declared schema by then, and the registry
re-validates the key set against its own spec, so the passthrough cannot widen
what a caller may send.
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

logger = logging.getLogger("search_console_mcp.server")

SERVER_NAME = "search-console"

INSTRUCTIONS = """\
Read-only access to Google Search Console for one or more explicitly allowlisted
properties.

Rules this server enforces on every call:
- the property must be in the server's configured allowlist (there is no wildcard);
- URL inspection is limited to URLs inside the allowlisted property;
- date ranges, row limits, pagination, dimensions and filters are bounded;
- no operation writes to Search Console: no property or sitemap changes and no
  indexing requests.

This server is deliberately not configurable at call time. Exposing
``GSC_ALLOWED_SITES`` cannot be changed through any tool.
"""

# Read-only hints. These are advisory metadata for the client; the server does
# not depend on them for safety.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def build_registry(allowlist: PropertyAllowlist) -> ToolRegistry:
    """Wire a registry to real ADC-backed clients."""

    def factory() -> Any:
        # Called lazily, after validation, so a rejected call never fetches a
        # token.
        return build_client(allowlist=allowlist, timeout=REQUEST_TIMEOUT_SECONDS)

    return ToolRegistry(client_factory=factory, allowlist=allowlist)


def create_server(
    *,
    allowlist: PropertyAllowlist | None = None,
    registry: ToolRegistry | None = None,
) -> MCPServer:
    """Build the MCP server.

    The allowlist is resolved *here*, at construction, so a server with no
    configured properties fails immediately instead of serving an empty
    capability set that looks healthy.
    """
    if allowlist is None:
        allowlist = PropertyAllowlist.from_env()
    if registry is None:
        registry = build_registry(allowlist)

    # Tools are built explicitly and handed to the constructor. The alternative —
    # `server.add_tool(fn)` — derives the input schema from the function
    # signature; a `**kwargs` handler would then be advertised as "an object with
    # arbitrary keys", which is exactly the free-form contract the registry
    # refuses. Building each Tool ourselves keeps the advertised schema and the
    # enforced schema the same object.
    tools = [_build_tool(registry, spec) for spec in registry.specs()]

    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        tools=tools,
        # Transport-layer argument gating: the SDK would otherwise drop unknown
        # keys silently, and would report an unknown tool as a bare string.
        middleware=[reject_unknown_arguments(registry)],
    )
    return server


def _build_tool(registry: ToolRegistry, spec: Any) -> Tool:
    """Turn one registry spec into an SDK ``Tool`` with the registry's schema.

    The handler is given an explicit, generated signature naming each argument
    the registry accepts, each annotated ``Any``. Two reasons, both learned from
    the SDK's behaviour rather than assumed:

    * ``Tool.from_function`` builds a pydantic model from the signature and the
      SDK invokes the function as ``fn(**validated)``. A ``**kwargs`` handler is
      rejected at call time ("kwargs: Field required") because that model treats
      the variadic parameter as one required *named* field.
    * Annotating a parameter with a concrete type would make the SDK coerce it
      (``"1000"`` -> ``1000``) and silently override the registry's strict type
      checks. ``Any`` leaves the value exactly as it arrived, so the registry
      remains the only validator.

    The generated signature is also why ``additionalProperties`` is the SDK's to
    decide: an explicit signature permits unknown keys at the schema level. The
    registry still rejects them, which the stdio tests assert on the wire.
    """
    tool_name = spec.name
    accepted = list(spec.accepted)

    async def _dispatch(**kwargs: Any) -> Any:
        # The generated signature gives every parameter a `None` default, so an
        # omitted argument would otherwise arrive as an explicit null and trip
        # the registry's type checks ("row_limit must be an integer"). Treating
        # null as absent is the intended reading of an omitted optional JSON
        # argument, and keeps the registry's own defaults in charge.
        supplied = {name: value for name, value in kwargs.items() if value is not None}
        return _render(registry.call(tool_name, supplied))

    handler = _make_handler(tool_name, accepted, _dispatch)
    tool = Tool.from_function(
        handler,
        name=tool_name,
        description=spec.description,
        annotations=_READ_ONLY,
        # Unstructured output: the envelope is returned as text, so the SDK does
        # not try to serialise it a second time against an inferred schema.
        structured_output=False,
    )

    # Generated from `Any`-annotated defaults the schema would say
    # `{"type":"string","default":null}` per field, which is wrong for e.g.
    # `row_limit`. Replace it with the registry's explicit schema, which declares
    # the same argument set with correct types and bounds.
    schema = spec.input_schema()
    # The SDK's derived schema sets `additionalProperties` from the signature
    # shape (an explicit signature yields `true`). We know our own contract:
    # unknown keys are rejected, so advertise that. The rewrite in
    # `reject_unknown_arguments` is what makes the claim true on the wire.
    schema["additionalProperties"] = False
    tool.parameters = schema
    return tool


def reject_unknown_arguments(registry: ToolRegistry) -> Any:
    """Middleware that refuses unknown arguments before the SDK parses them.

    Necessary because the SDK's arg model derives its fields from the handler
    signature and *silently drops* any key not in that signature. A caller
    sending ``{"host": "https://evil.example"}`` would otherwise have the key
    vanish and the call succeed, which is the opposite of fail-closed.

    Running at the transport layer also means the rejection happens before any
    tool code, before the allowlist check, and before credential acquisition.
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
                # An unknown tool name would otherwise surface as a bare
                # "Unknown tool: x" string. Return the same structured envelope
                # every other rejection uses, and do it before dispatch.
                return _mcp_error_result(
                    str(name) if name is not None else "",
                    "invalid_argument",
                    "unknown tool",
                )
            arguments = params.get("arguments")
            if isinstance(arguments, dict) and not spec.allows_extra:
                unknown = set(arguments) - set(spec.accepted)
                if unknown:
                    # Return the registry's own redacted envelope rather than
                    # raising, so the client sees the same structured error
                    # shape as every other rejection.
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
    """Build a ``CallToolResult`` carrying a redacted error envelope."""
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
    # The names come from ToolSpec declarations in this package, never from a
    # caller, so this is not an injection surface. Names are still validated so
    # a future typo fails loudly at construction instead of at call time.
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

    The envelope text carries a structured ``ok: false`` error, and the MCP
    ``isError`` flag is set from the same decision. Returning only the text --
    as this did before -- left ``isError`` at the SDK default of false, so a
    refused property or rejected argument looked like a *successful* call to any
    client that branches on the flag. The two channels must agree.
    """
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=result.text)],
        is_error=result.is_error,
    )


def main() -> None:  # pragma: no cover - exercised via subprocess in tests
    from .__main__ import main as _main

    _main()