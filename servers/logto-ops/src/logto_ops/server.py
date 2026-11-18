"""MCP server wiring: tools, argument gating, and the confirmation gate.

The gate lives here, in middleware, rather than in the handlers, so that no
handler can be reached without it and no future handler can forget it.

Request path for a write
------------------------
1. The SDK has coerced arguments to the advertised schema.
2. :class:`GateMiddleware` resolves the operation, rejects unknown arguments,
   checks the effect against the server's configured mode, and — in ``apply``
   mode — verifies ``confirmation_token`` against the arguments.
3. A call with ``dry_run=true`` (or a write with no token) returns the
   **plan**: resolved operation, redacted arguments, computed digest and the
   token to present for execution. Nothing is sent to Logto.
4. Otherwise the handler executes through :class:`LogtoClient`.

Errors are returned as structured JSON in ``structuredContent`` **and** as text,
so a client that shows text still shows the refusal reason, and a client that
reads structured content can branch on ``code``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Sequence

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations

from . import __version__
from .client import AmbiguousResult, LogtoClient, LogtoError, LogtoHTTPError
from .confirm import (
    ConfirmationError,
    ConfirmationVerifier,
    canonical_digest,
    redacted_arguments,
)
from .gate import GateConfig, GateConfigError, classify
from .operations import Operation, OperationError, build_registry, REGISTRY
from .settings import Settings

logger = logging.getLogger("logto_ops.server")

SERVER_NAME = "logto-ops"

INSTRUCTIONS = """\
Read and write access to this deployment's Logto tenants through the Management API.

What this server can do: manage applications (including Machine-to-machine apps and
their client secrets), users, roles, scopes, connectors, sign-in experience,
organizations and API resources, and read audit logs.

What it cannot do: delete anything. Delete operations are not exposed.

Write operations are two-phase. Call one with dry_run=true to receive a plan and a
confirmation_token, then call it again with the same arguments and that token to
apply. A write without a matching token does not execute. Changing any argument
after the plan invalidates the token, so re-plan rather than reuse.

Destructive-but-not-delete actions are enabled: suspending users, replacing a
user's password, replacing sign-in experience fields, and minting client secrets
with full-tenant Management API authority if you assign that role. The plan states
when a role grants the Management API 'all' scope.
"""


def _annotations(effect: str) -> ToolAnnotations:
    # Advisory only. The gate, not this metadata, authorises anything.
    if effect == "read":
        return ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    )


def _json_result(payload: Any, *, is_error: bool = False) -> Any:
    """Return a CallToolResult-shaped value.

    The SDK's MCPServer accepts a list of content blocks or a string; returning a
    dict-shaped result here keeps structured errors visible to clients that read
    ``structuredContent``.
    """
    from mcp.types import CallToolResult, TextContent

    body = json.dumps(payload, indent=2, default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        structured_content=payload if isinstance(payload, dict) else {"result": payload},
        is_error=is_error,
    )


def build_server(
    *,
    settings: Settings,
    gate: GateConfig,
    verifier: ConfirmationVerifier,
    client_factory: Callable[[Settings], LogtoClient] | None = None,
) -> MCPServer:
    """Build the MCP server.

    ``client_factory`` exists so tests can inject a fake client without a network.
    Production passes ``None`` and gets the real client, constructed lazily on the
    first call that needs it — ``initialize``/``tools/list`` therefore never touch
    the credential.
    """
    registry = build_registry()
    factory = client_factory

    _client_cache: dict[str, LogtoClient | Any] = {}

    def get_client() -> Any:
        if "client" not in _client_cache:
            _client_cache["client"] = factory(settings) if factory else LogtoClient(settings)
        return _client_cache["client"]

    async def dispatch(operation_name: str, arguments: dict[str, Any]) -> Any:
        operation = registry[operation_name]
        try:
            effect = classify(operation.name)
        except GateConfigError as exc:  # pragma: no cover - registry is checked at startup
            return _json_result({"error": {"code": "not_permitted", "message": str(exc)}}, is_error=True)

        if effect == "write" and not gate.serves_writes:
            return _json_result(
                {
                    "error": {
                        "code": "write_not_permitted",
                        "message": f"this server is configured read-only (mode={gate.mode})",
                    }
                },
                is_error=True,
            )

        supplied = {k: v for k, v in arguments.items() if v is not None}
        unknown = operation.unknown_arguments(supplied)
        if unknown:
            return _json_result(
                {
                    "error": {
                        "code": "unknown_argument",
                        "message": f"unknown argument(s): {', '.join(unknown)}",
                        "accepted": sorted(operation.accepted),
                    }
                },
                is_error=True,
            )

        dry_run = supplied.pop("dry_run", False)
        token = supplied.pop("confirmation_token", "")

        if effect == "write":
            plan = {
                "plan": {
                    "operation": operation.name,
                    "effect": effect,
                    "arguments": redacted_arguments(supplied),
                    "digest": canonical_digest(operation.name, supplied)[:16],
                    "target": settings.base_url,
                }
            }
            if dry_run or not gate.writes_need_confirmation:
                plan["plan"]["will_execute"] = False
                if gate.writes_need_confirmation:
                    plan["confirmation_token"] = verifier.mint(operation.name, supplied)
                    plan["next_step"] = (
                        "repeat this call with the same arguments plus confirmation_token to apply"
                    )
                return _json_result(plan)
            try:
                verifier.verify(operation.name, supplied, token)
            except ConfirmationError as exc:
                plan["error"] = {"code": exc.code, "message": str(exc)}
                if exc.code == "confirmation_required":
                    plan["confirmation_token"] = verifier.mint(operation.name, supplied)
                if exc.code in ("confirmation_required", "confirmation_mismatch", "confirmation_expired"):
                    plan["next_step"] = "re-run with the confirmation_token from this response"
                return _json_result(plan, is_error=True)

        assert operation.handler is not None
        try:
            result = operation.handler(get_client(), supplied)
        except AmbiguousResult as exc:
            return _json_result({"error": {"code": "ambiguous", "message": str(exc)}}, is_error=True)
        except LogtoHTTPError as exc:
            return _json_result(
                {
                    "error": {
                        "code": "provider_error",
                        "http_status": exc.status,
                        "provider_code": exc.provider_code,
                        "message": str(exc),
                    }
                },
                is_error=True,
            )
        except LogtoError as exc:
            return _json_result({"error": {"code": "transport_error", "message": str(exc)}}, is_error=True)
        except OperationError as exc:
            return _json_result({"error": {"code": "invalid_request", "message": str(exc)}}, is_error=True)

        payload = result.payload
        logger.info("operation=%s effect=%s ok", operation.name, effect)
        return _json_result(payload)

    tools = [_build_tool(operation, dispatch) for operation in REGISTRY]

    return MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        tools=tools,
        middleware=[reject_undeclared_arguments(registry)],
    )


def reject_undeclared_arguments(registry: dict[str, Operation]) -> Any:
    """Refuse unknown tools and undeclared arguments before the SDK parses them.

    Necessary because the SDK's argument model is derived from the generated
    handler signature and **silently drops** keys it does not know. Without this,
    ``create_m2m_application(name=..., type="Traditional")`` would quietly create
    the type this server always creates and the caller would believe otherwise.
    Running at the transport layer also means the refusal happens before the
    credential is touched.
    """

    async def middleware(ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)
        params = getattr(ctx, "params", None)
        params = params if isinstance(params, dict) else None
        if params is None:
            return await call_next(ctx)
        name = params.get("name")
        operation = registry.get(name) if isinstance(name, str) else None
        if operation is None:
            return _json_result(
                {"error": {"code": "unknown_tool", "message": f"unknown tool: {name!r}"}},
                is_error=True,
            )
        arguments = params.get("arguments")
        if isinstance(arguments, dict):
            # dry_run/confirmation_token are gate controls, not operation arguments.
            declared = set(operation.accepted) | {"dry_run", "confirmation_token"}
            unknown = sorted(set(arguments) - declared)
            if unknown:
                return _json_result(
                    {
                        "error": {
                            "code": "unknown_argument",
                            "message": f"unknown argument(s): {', '.join(unknown)}",
                            "accepted": sorted(declared),
                        }
                    },
                    is_error=True,
                )
        return await call_next(ctx)

    return middleware


def _parameter_names(operation: Operation) -> list[str]:
    names = sorted(operation.accepted)
    if operation.effect == "write":
        names = sorted(set(names) | {"confirmation_token", "dry_run"})
    return names


def _build_tool(operation: Operation, dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]]) -> Tool:
    """Turn one operation into an SDK ``Tool`` with an explicit signature.

    The generated signature matters: the SDK builds a pydantic model from it and
    invokes the handler as ``fn(**validated)``. A ``**kwargs`` handler is
    rejected at call time. Each parameter is annotated ``Any`` so the SDK does
    not coerce ``"10"`` to ``10`` — the operation's own validators stay the only
    type authority.
    """
    params = _parameter_names(operation)

    async def _call(**kwargs: Any) -> Any:
        return await dispatch(operation.name, kwargs)

    annotations = {name: Any for name in params}
    annotations["return"] = Any
    defaults = {name: None for name in params}
    _call.__signature__ = _make_signature(_call, params, defaults)  # type: ignore[attr-defined]
    _call.__annotations__ = {**annotations, "kwargs": Any}

    description = operation.description
    if operation.effect == "write":
        description += (
            "\n\nWrite operation: requires a confirmation_token from a prior dry_run of the "
            "same arguments."
        )

    tool = Tool.from_function(
        _call,
        name=operation.name,
        description=description,
        annotations=_annotations(operation.effect),
    )
    # The SDK derives `additionalProperties` from the generated signature, which
    # yields `true` (any extra key allowed). We know our own contract — unknown
    # keys are refused — so advertise that.
    if isinstance(tool.parameters, dict):
        tool.parameters["additionalProperties"] = False
    # ...and make it true where it is actually enforced. The SDK builds its
    # argument model from the signature and *drops* unknown keys before calling
    # the function, so a schema that merely says `additionalProperties: false`
    # would still let `create_m2m_application(type="Traditional")` succeed while
    # creating a MachineToMachine app. Pydantic refuses at validation instead,
    # which is the fail-closed behaviour this server needs.
    arg_model = tool.fn_metadata.arg_model
    arg_model.model_config["extra"] = "forbid"
    # Pydantic compiles and caches the core schema at class creation, so the
    # config change above has no effect until the schema is rebuilt.
    arg_model.model_rebuild(force=True)
    return tool


def _make_signature(fn: Callable[..., Any], params: Sequence[str], defaults: dict[str, Any]):
    import inspect

    parameters = [
        inspect.Parameter(
            name, kind=inspect.Parameter.KEYWORD_ONLY, default=defaults[name], annotation=Any
        )
        for name in params
    ]
    return inspect.Signature(parameters, return_annotation=Any)
