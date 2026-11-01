"""Authenticated Streamable HTTP entry point for the Play Console MCP server.

This is a thin adapter: the MCP server object, its 16 tools, their schemas and
read-only annotations all come from :func:`play_console_mcp.server.build_server`
unchanged. The only difference from the stdio entry point is the transport and
the addition of a bearer-token requirement, both supplied by
``mcp_http_transport``.

Startup order is deliberate:

1. The package allowlist is validated first (the server's own fail-closed
   contract, unchanged).
2. The bearer token is resolved next; a missing token exits before a socket is
   opened.
3. Google credentials are still resolved lazily on the first API call, so
   ``initialize`` and ``tools/list`` work with no Google secret present — which
   is exactly what LiteLLM needs to discover tools before secret rotation.

Run with ``python -m play_console_mcp.http_main``. Configuration:

* ``MCP_HTTP_AUTH_TOKEN`` or ``MCP_HTTP_AUTH_TOKEN_FILE`` — required.
* ``MCP_HTTP_HOST`` / ``MCP_HTTP_PORT`` / ``MCP_HTTP_PATH`` — bind address.
* ``MCP_HTTP_ALLOWED_HOSTS`` — comma-separated Host allowlist for DNS-rebinding
  protection. Defaults to the bind host.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp_http_transport.auth import AuthConfigError
from mcp_http_transport.server import (
    DEFAULT_PATH,
    AppConfig,
    AppConfigError,
    ServeConfig,
    resolve_token,
    serve,
)

from .server import SERVER_NAME, build_server
from .settings import SettingsError, load_settings

logger = logging.getLogger("play_console_mcp.http_main")


def _allowed_hosts(bind_host: str) -> tuple[str, ...]:
    raw = os.environ.get("MCP_HTTP_ALLOWED_HOSTS", "")
    hosts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return hosts or (bind_host,)


def app_config(bind_host: str = "0.0.0.0") -> AppConfig:
    return AppConfig(
        # The expected token never enters a local that could be logged.
        expected_token=resolve_token(),
        path=os.environ.get("MCP_HTTP_PATH") or DEFAULT_PATH,
        allowed_hosts=_allowed_hosts(bind_host),
        max_sessions=int(os.environ.get("MCP_HTTP_MAX_SESSIONS") or 100),
        session_idle_timeout=float(os.environ.get("MCP_HTTP_SESSION_IDLE_TIMEOUT") or 300.0),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"{SERVER_NAME}: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    serve_config = ServeConfig.from_env()
    try:
        config = app_config(serve_config.host)
    except (AppConfigError, AuthConfigError) as exc:
        # Both are startup configuration errors: exit 2 with a one-line,
        # secret-free explanation rather than a traceback. Container
        # orchestrators key restart policy off this code.
        print(f"{SERVER_NAME}: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    server = build_server(settings)
    logger.info(
        "serving %s over authenticated streamable HTTP on %s:%s%s",
        SERVER_NAME,
        serve_config.host,
        serve_config.port,
        config.path,
    )
    serve(server, config, serve_config)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()