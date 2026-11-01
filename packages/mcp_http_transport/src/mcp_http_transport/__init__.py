"""Reusable, isolated, authenticated HTTP (MCP Streamable HTTP) transport packaging.

This package adds exactly three things on top of an existing MCP
``MCPServer`` that already speaks stdio:

1. :func:`build_streamable_http_app` — mount the same server object on MCP
   Streamable HTTP at a private path, without changing any tool, schema, or
   annotation that the stdio transport exposes.
2. :class:`BearerTokenVerifier` — a constant-time, environment-local bearer
   token check implemented on top of the SDK's own ``TokenVerifier`` protocol,
   so the request is rejected by the SDK's ``RequireAuthMiddleware`` before any
   MCP session is created.
3. Path normalisation for hypervisors that mount the application at a prefix
   (Coolify/Traefik ``StripPrefix``-style), which would otherwise route every
   request to the ASGI app with an empty ``path`` and yield a 404.

The module deliberately contains no Google identifier, no secret material, and
no way to reach a credential store: its only input is an environment variable
holding the expected token, and its only output is an ASGI application.
"""

from __future__ import annotations

import os

from .auth import (
    ENV_MAX_TOKEN_LENGTH,
    ENV_MIN_TOKEN_LENGTH,
    ENV_TOKEN,
    AuthConfigError,
    BearerTokenVerifier,
    generate_token,
    load_expected_token,
)
from .server import (
    ENV_TOKEN_FILE,
    AppConfig,
    AppConfigError,
    ServeConfig,
    build_streamable_http_app,
    load_serve_config,
    serve,
)

__all__ = [
    "ENV_TOKEN",
    "ENV_TOKEN_FILE",
    "ENV_MIN_TOKEN_LENGTH",
    "ENV_MAX_TOKEN_LENGTH",
    "AuthConfigError",
    "BearerTokenVerifier",
    "generate_token",
    "load_expected_token",
    "AppConfig",
    "AppConfigError",
    "ServeConfig",
    "build_streamable_http_app",
    "load_serve_config",
    "serve",
]

__version__ = "0.1.0"


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag. Absent/unset keeps ``default``."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}