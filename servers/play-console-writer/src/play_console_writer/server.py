"""MCP stdio server entry point for the non-financial Play Console writer.

Startup contract
----------------
* ``PLAY_WRITER_ALLOWED_PACKAGES`` is mandatory. Without it the server exits
  before serving anything (fail closed).
* The release gate (``PLAY_WRITER_ENABLE_RELEASE_GATE``) and the artifact-upload
  gate (``PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD``) default to **off**, so the
  server as shipped cannot publish and cannot upload binaries.
* Credentials are resolved lazily on the first API call, so ``initialize`` and
  ``list_tools`` work with no credentials present.
* Every write tool is annotated with ``readOnlyHint=False``. Destructive tools
  (delete/deleteall) carry ``destructiveHint=True``; the rest carry
  ``destructiveHint=False``. The capability tool is ``readOnlyHint=True``.
* Policy, validation and upstream failures are returned as ``isError`` results
  via :class:`ToolError`, never as successful payloads.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations

from . import __version__
from .auth import CredentialError, load_credentials
from .capability import capability_report
from .client import GoogleWriteClient
from .edit_registry import EditRegistry
from .errors import LocalError, PolicyError, UpstreamError, WriteOutcomeUnknown
from .operations import TOOL_SPECS, WriteOperations
from .policy import ARTIFACT_UPLOAD_METHODS, RELEASE_GATE_METHODS
from .schema import SchemaError
from .schema import validate as validate_schema
from .settings import SettingsError, load_settings
from .validation import ValidationError

SERVER_NAME = "play-console-writer"

#: Tools that destroy or unrecoverably change stored state.
_DESTRUCTIVE_TOOLS = frozenset({
    "play_listing_delete",
    "play_listings_delete_all",
    "play_image_delete",
    "play_images_delete_all",
    "play_edit_delete",
})

#: Tools annotated read-only (no upstream mutation).
_READ_ONLY_TOOLS = frozenset({"play_describe_capability"})

# Python types for each JSON schema type used by TOOL_SPECS properties.
_PY_TYPE = {
    "string": "str",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "object": "dict",
    "array": "list",
}


class _StaticToken:
    """Test-only credential carrying a fixed bearer token.

    Only reachable when ``PLAY_WRITER_TEST_LOOPBACK_BASE`` and
    ``PLAY_WRITER_TEST_STATIC_TOKEN`` are both set; settings refuses the token
    without a loopback base. It exists so an end-to-end test can exercise the
    real stdio protocol against a fake upstream without any Google credential.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.valid = True
        self.expired = False

    def refresh(self, request=None) -> None:  # pragma: no cover - never needed
        return None


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _error_result(tool_name: str, message: str) -> Any:
    """A ``CallToolResult`` carrying an error, for transport-layer refusals."""
    from mcp.types import CallToolResult, TextContent

    text = _json_result({
        "operation": tool_name,
        "error": {"code": "invalid_argument", "message": message},
    })
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def schema_guard(schemas: dict[str, dict[str, Any]]) -> Any:
    """Middleware that validates ``tools/call`` arguments against the schemas.

    Necessary because the MCP SDK derives its argument model from the handler
    signature and **silently drops** any key not in that signature. A caller
    sending ``{"package_name": "...", "price": "9.99"}`` would otherwise have
    ``price`` vanish and the call succeed — the opposite of fail-closed, and
    exactly the shape a financial-argument injection would take.

    Running at the transport layer also means rejection happens before any tool
    code, before the package allowlist check, and before credential acquisition.
    """

    def _arguments(params: Any) -> Any:
        if isinstance(params, dict):
            return params.get("arguments")
        return getattr(params, "arguments", None)

    def _name(params: Any) -> Any:
        if isinstance(params, dict):
            return params.get("name")
        return getattr(params, "name", None)

    async def middleware(ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "method", None) == "tools/call":
            params = getattr(ctx, "params", None)
            name = _name(params)
            schema = schemas.get(name) if isinstance(name, str) else None
            if schema is not None:
                arguments = _arguments(params)
                if arguments is None:
                    arguments = {}
                try:
                    validate_schema(schema, arguments)
                except SchemaError as exc:
                    return _error_result(str(name), str(exc))
        return await call_next(ctx)

    return middleware


def _annotations(tool_name: str, method_id: str | None) -> ToolAnnotations:
    if tool_name in _READ_ONLY_TOOLS:
        return ToolAnnotations(
            title="Describe Play writer capability",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    destructive = tool_name in _DESTRUCTIVE_TOOLS
    # Idempotent unless the underlying method repeats an effect on retry.
    idempotent = method_id not in {"edits.insert", "edits.tracks.create"}
    return ToolAnnotations(
        title=tool_name.replace("_", " "),
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=True,
    )


def build_server(settings=None, *, client=None, transport=None,
                 credentials=None, credential_config=None,
                 registry: EditRegistry | None = None) -> MCPServer:
    """Construct the MCP server.

    ``settings``/``client``/``transport``/``credentials`` are injectable for
    tests; in production everything is derived from the environment and there is
    no path to a fake upstream — the wire always goes to Google.
    """
    if settings is None:
        settings = load_settings()

    if client is None:
        if settings.test_static_token:
            # End-to-end test path: a loopback fake that accepts a fixed token.
            # Settings refuses to load a static token without a loopback base,
            # so this branch cannot point at Google.
            credentials = _StaticToken(settings.test_static_token)
        elif credential_config is None and credentials is None:
            credential_config = load_credentials(
                credentials_path=settings.credentials_path,
                subject=settings.subject,
                lazy=True,
            )
        client = GoogleWriteClient(
            settings=settings,
            credentials=credentials,
            credential_config=credential_config,
            transport=transport,
            **({"api_root": settings.test_loopback_base}
               if settings.test_loopback_base else {}),
        )
        if settings.user_project:
            client._client.headers["x-goog-user-project"] = settings.user_project

    ops = WriteOperations(client=client, settings=settings, registry=registry)

    tools: list[Tool] = []
    for tool_name, spec in TOOL_SPECS.items():
        tools.append(_build_tool(tool_name, spec, ops, client))
    tools.append(_build_capability_tool(settings))

    schemas = {name: spec["schema"] for name, spec in TOOL_SPECS.items()}
    schemas["play_describe_capability"] = {
        "type": "object", "properties": {}, "required": [],
        "additionalProperties": False,
    }

    return MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=(
            "Non-financial Google Play Console writes. Package names are "
            "restricted to a mandatory server-side allowlist. There is NO "
            "generic API call tool: only explicitly named operations exist, and "
            "no financial method (refunds, price/tax setters, purchase "
            "acknowledgement/consumption/cancellation, activate/deactivate/"
            "archive, product deletion, users/grants) is reachable. Staged edits "
            "cannot publish unless the release gate is enabled; call "
            "play_describe_capability to see exactly what is and is not "
            "supported."
        ),
        tools=tools,
        middleware=[schema_guard(schemas)],
    )


def _build_capability_tool(settings) -> Tool:
    """Build the read-only capability-description tool."""

    async def play_describe_capability() -> str:
        return _json_result(
            {
                "operation": "play_describe_capability",
                "data": capability_report(
                    release_gate_enabled=settings.enable_release_gate,
                    artifact_upload_enabled=settings.enable_artifact_upload,
                ),
            }
        )

    play_describe_capability.__name__ = "play_describe_capability"
    play_describe_capability.__doc__ = (
        "Describe this server's write surface: implemented methods, gates, the "
        "financial methods excluded, and the non-financial operations that are "
        "deliberately not supported."
    )
    tool = Tool.from_function(
        play_describe_capability,
        name="play_describe_capability",
        description=play_describe_capability.__doc__,
        annotations=_annotations("play_describe_capability", None),
        structured_output=False,
    )
    tool.parameters = {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False}
    return tool


def _build_tool(tool_name: str, spec: dict[str, Any],
                ops: WriteOperations, client: GoogleWriteClient) -> Tool:
    """Build one write tool with an explicit, introspectable signature.

    The MCP SDK derives the advertised JSON schema from the handler signature,
    so each handler is generated with the exact parameter names of its spec
    (``None`` defaults make a parameter optional) and dispatch happens through a
    single closure. The generated handler also re-validates its arguments
    against the same schema, because the SDK advertises but does not enforce
    ``additionalProperties: false``.

    The advertised schema is then replaced with the registry's explicit one, so
    the enums, bounds and ``additionalProperties: false`` a caller sees are the
    constraints the handler actually enforces — not a looser signature-derived
    approximation.
    """
    tool_schema = spec["schema"]
    properties: dict[str, Any] = tool_schema["properties"]
    required: set[str] = set(tool_schema.get("required", []))

    params: list[str] = []
    for name, prop in properties.items():
        annotation = _PY_TYPE.get(prop.get("type", "string"), "Any")
        if name in required:
            params.append(f"{name}: {annotation}")
        else:
            params.append(f"{name}: {annotation} | None = None")
    signature = ", ".join(params)
    # Build the argument mapping as a dict literal of ``"name": name`` pairs.
    call_args = ", ".join(f"{name!r}: {name}" for name in properties)

    namespace: dict[str, Any] = {
        "ops": ops,
        "client": client,
        "tool_schema": tool_schema,
        "validate_schema": validate_schema,
        "json_result": _json_result,
        "ToolError": ToolError,
        "SettingsError": SettingsError,
        "PolicyError": PolicyError,
        "LocalError": LocalError,
        "UpstreamError": UpstreamError,
        "WriteOutcomeUnknown": WriteOutcomeUnknown,
        "CredentialError": CredentialError,
        "ValidationError": ValidationError,
        "SchemaError": SchemaError,
        "Any": Any,
    }

    dispatcher = _make_dispatcher(tool_name, ops, client, spec)
    namespace["dispatch"] = dispatcher

    source = f"""
async def handler({signature}) -> str:
    return dispatch({{{call_args}}})
"""
    exec(source, namespace)  # noqa: S102 - generated from a trusted static spec
    handler = namespace["handler"]
    handler.__name__ = tool_name
    handler.__doc__ = spec["description"]

    tool = Tool.from_function(
        handler,
        name=tool_name,
        description=spec["description"],
        annotations=_annotations(tool_name, spec.get("method_id")),
        # Unstructured text output: the JSON envelope is already a string, so
        # the SDK must not try to model it a second time.
        structured_output=False,
    )
    tool.parameters = tool_schema
    return tool


def _make_dispatcher(tool_name: str, ops: WriteOperations,
                     client: GoogleWriteClient, spec: dict[str, Any]):
    """Build the shared body: validate args, call the operation, map errors."""

    def dispatch(kwargs: dict[str, Any]) -> str:
        # Refuse an unknown *key* before dropping unset optionals. Dropping
        # ``None`` first would let ``{"price": None}`` vanish and the call
        # succeed as if the argument had been honoured — the opposite of
        # fail-closed. A declared optional may still be explicitly null.
        declared = spec["schema"].get("properties") or {}
        unknown = [key for key in kwargs if key not in declared]
        if unknown:
            raise ToolError(
                f"invalid arguments: {', '.join(sorted(unknown))} is not a "
                "recognised argument for this tool"
            )

        # Drop unset optional parameters so operation defaults apply.
        cleaned = {k: v for k, v in kwargs.items() if v is not None}

        # Re-validate against the declared schema: the SDK advertises but does
        # not enforce additionalProperties:false, so an unknown argument would
        # otherwise reach the handler.
        try:
            validate_schema(spec["schema"], cleaned)
        except SchemaError as exc:
            raise ToolError(f"invalid arguments: {exc}") from None

        handler = getattr(ops, spec["attr"])
        try:
            data = handler(**cleaned)
        except (SettingsError, ValidationError, SchemaError) as exc:
            raise ToolError(f"rejected by policy: {exc}") from None
        except PolicyError as exc:
            raise ToolError(str(exc)) from None
        except WriteOutcomeUnknown as exc:
            raise ToolError(str(exc)) from None
        except LocalError as exc:
            raise ToolError(str(exc)) from None
        except UpstreamError as exc:
            raise ToolError(str(exc)) from None
        except CredentialError as exc:
            raise ToolError(str(exc)) from None
        except TypeError as exc:
            raise ToolError(f"invalid arguments: {exc}") from None

        return _json_result(
            {
                "operation": tool_name,
                "method_id": spec.get("method_id"),
                "request": _last_request(client),
                "data": data,
            }
        )

    return dispatch


def _last_request(client: GoogleWriteClient) -> dict[str, Any]:
    """Echo of the upstream request for auditability, with tokens masked.

    Purchase tokens and pagination tokens are bearer-like: anyone holding one
    can query or act on Play state for it, so they are masked even though the
    caller supplied them (MCP clients routinely log tool results).

    The *write* request is preferred over the read-back GET that usually follows
    it, so the echo names the mutation the caller asked for.
    """
    recorded = getattr(client, "_last_write_request", None)
    if not isinstance(recorded, dict):
        recorded = getattr(client, "_last_request", None)
    if not isinstance(recorded, dict):
        return {"root": "https://androidpublisher.googleapis.com"}
    result = dict(recorded)
    query = result.get("query")
    if isinstance(query, dict):
        masked_query: dict[str, Any] = {}
        masked = False
        for key, value in query.items():
            if str(key).lower() in _TOKEN_QUERY_KEYS:
                masked_query[key] = "***"
                masked = True
            else:
                masked_query[key] = value
        result["query"] = masked_query
        if masked:
            result["token_masked"] = True
    return result


_TOKEN_QUERY_KEYS = frozenset({
    "token", "page_token", "pagetoken", "access_token", "accesstoken",
    "id_token", "refresh_token",
})


def main() -> None:
    """Console entry point: build from the environment and serve over stdio."""
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"{SERVER_NAME}: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    server = build_server(settings)
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    main()