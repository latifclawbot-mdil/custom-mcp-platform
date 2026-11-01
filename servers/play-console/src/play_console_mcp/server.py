"""MCP stdio server entry point.

Read-only Google Play Console access over the Android Publisher API v3.

Startup contract
----------------
* ``PLAY_CONSOLE_ALLOWED_PACKAGES`` is mandatory. Without it the server exits
  before serving anything (fail closed).
* Credentials are resolved lazily on the first API call, so ``initialize`` and
  ``list_tools`` work with no credentials present — useful for LiteLLM tool
  discovery.
* Every registered tool is annotated ``readOnlyHint=True`` /
  ``destructiveHint=False``. Policy and upstream failures are returned as
  ``isError`` results via :class:`ToolError`, never as successful payloads.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .auth import CredentialError, load_credentials
from .errors import LocalError, UpstreamError
from .google_client import GoogleApiClient
from .operations import TOOL_SPECS, Operations
from .settings import SettingsError, load_settings
from .validation import ValidationError

SERVER_NAME = "play-console-readonly"

_READ_ONLY = ToolAnnotations(
    title="Read-only Play Console query",
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# Python types for each JSON schema type used by TOOL_SPECS properties.
_PY_TYPE = {"string": "str", "integer": "int", "boolean": "bool"}


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def build_server(settings=None, *, client=None, transport=None,
                 credentials=None, credential_config=None) -> MCPServer:
    """Construct the MCP server.

    ``settings``/``client``/``transport``/``credentials`` are injectable for
    tests; in production everything is derived from the environment and there
    is no path to a fake upstream — the wire always goes to Google.
    """
    if settings is None:
        settings = load_settings()

    if client is None:
        if credential_config is None and credentials is None:
            credential_config = load_credentials(
                credentials_path=settings.credentials_path,
                subject=settings.subject,
                lazy=True,
            )
        client = GoogleApiClient(
            settings=settings,
            credentials=credentials,
            credential_config=credential_config,
            transport=transport,
        )
        if settings.user_project:
            client._client.headers["x-goog-user-project"] = settings.user_project

    ops = Operations(client=client, settings=settings)
    server = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=(
            "Read-only Google Play Console access. Package names are restricted "
            "to a server-side allowlist and must be supplied explicitly; this "
            "server cannot enumerate the apps in an account. No mutating "
            "operation (publish, edit, commit, upload) is available."
        ),
    )

    for tool_name, spec in TOOL_SPECS.items():
        _register(server, tool_name, spec, ops, client)

    return server


def _register(server: MCPServer, tool_name: str, spec: dict[str, Any],
              ops: Operations, client: GoogleApiClient) -> None:
    """Register one tool with an explicit, introspectable signature.

    The MCP SDK derives the advertised JSON schema from the handler signature,
    so each handler is generated with the exact parameter names of its spec
    (``None`` defaults make a parameter optional) and dispatch happens through a
    single closure.
    """
    properties: dict[str, Any] = spec["schema"]["properties"]
    required: set[str] = set(spec["schema"].get("required", []))

    params: list[str] = []
    for name, prop in properties.items():
        annotation = _PY_TYPE.get(prop.get("type", "string"), "Any")
        if name in required:
            params.append(f"{name}: {annotation}")
        else:
            params.append(f"{name}: {annotation} | None = None")
    signature = ", ".join(params)
    call_args = ", ".join(f"{name}={name}" for name in properties)

    namespace: dict[str, Any] = {
        "ops": ops,
        "client": client,
        "tool_name": tool_name,
        "json_result": _json_result,
        "ToolError": ToolError,
        "SettingsError": SettingsError,
        "LocalError": LocalError,
        "UpstreamError": UpstreamError,
        "CredentialError": CredentialError,
        "ValidationError": ValidationError,
        "Any": Any,
    }

    dispatcher = _make_dispatcher(tool_name, ops, client)
    namespace["dispatch"] = dispatcher

    source = f"""
async def handler({signature}) -> str:
    return dispatch({call_args})
"""
    exec(source, namespace)  # noqa: S102 - generated from a trusted static spec
    handler = namespace["handler"]
    handler.__name__ = tool_name
    handler.__doc__ = spec["description"]

    server.add_tool(
        handler,
        name=tool_name,
        description=spec["description"],
        annotations=_READ_ONLY,
    )


def _make_dispatcher(tool_name: str, ops: Operations, client: GoogleApiClient):
    """Build the shared body: call the operation, shape results, map errors."""

    def dispatch(**kwargs: Any) -> str:
        handler = getattr(ops, TOOL_SPECS[tool_name]["attr"])
        # Drop unset optional parameters so operation defaults apply.
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        try:
            data = handler(**cleaned)
        except (SettingsError, ValidationError) as exc:
            # Allowlist refusal or malformed identifier: caller-fixable policy
            # rejection, returned as an error result rather than data.
            raise ToolError(f"rejected by policy: {exc}") from None
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
                "request": _last_request(client),
                "data": data,
            }
        )

    return dispatch


def _last_request(client: GoogleApiClient) -> dict[str, Any]:
    """Echo of the upstream request for auditability, with tokens masked.

    Purchase tokens are bearer-like: anyone holding one can query Play's
    purchase state for it, so they are masked even though the caller supplied
    them (MCP clients routinely log tool results). Pagination tokens that the
    caller passes as query parameters get the same treatment — the echo must
    never replay token material, whether it lives in a path segment or a query
    value.
    """
    recorded = getattr(client, "_last_request", None)
    if isinstance(recorded, dict):
        result = dict(recorded)
        masked = False

        path = str(recorded.get("path", ""))
        masked_path = _mask_token_segments(path)
        if masked_path != path:
            result["path"] = masked_path
            masked = True

        query = recorded.get("query")
        if isinstance(query, dict):
            masked_query: dict[str, Any] = {}
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
    return {"method": "GET", "root": "https://androidpublisher.googleapis.com"}


_TOKEN_SEGMENT = re.compile(r"(/tokens/)[^/]+")
_TOKEN_QUERY_KEYS = frozenset({
    "token", "page_token", "pagetoken", "access_token", "accesstoken",
    "id_token", "refresh_token",
})


def _mask_token_segments(path: str) -> str:
    return _TOKEN_SEGMENT.sub(r"\1***", path)


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