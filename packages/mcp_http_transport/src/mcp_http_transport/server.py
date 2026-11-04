"""Mount an existing ``MCPServer`` on authenticated MCP Streamable HTTP.

Why a module and not a Dockerfile? Because the two servers under
``servers/`` are deliberately single-transport today. The only change they need
to accept HTTP is a one-line call to :func:`build_streamable_http_app` in their
own entry point; everything transport-shaped (auth, host allowlist, path
normalisation, session limits, body limits) lives here so it is written and
tested once instead of twice.

Security posture of the produced application, stated precisely:

* The MCP endpoint is wrapped by the SDK's ``RequireAuthMiddleware`` because a
  ``TokenVerifier`` is supplied. Requests without a valid ``Authorization:
  Bearer <token>`` receive ``401`` and never reach tool dispatch. This is
  verified by tests against a running server, not asserted from reading code.
* DNS-rebinding protection is enabled with an explicit Host/Origin allowlist.
  It is **not** a security boundary on its own (a non-browser client sets
  ``Host`` freely); it is defence in depth for browser-origin callers. The
  boundary is the token plus the private network placement.
* Request bodies are bounded, sessions are bounded, and idle sessions expire.
* Every MCP response is identical to the stdio transport's: this module never
  touches tools, schemas, or annotations.

What it does *not* do: TLS, rate limiting, or per-caller identities. TLS is
terminated by the platform proxy; rate limiting belongs to the proxy; every
caller presenting the shared token is one identity by construction, which is
why the deployment recipe keeps the endpoint off the public internet.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import ENV_TOKEN, BearerTokenVerifier, load_expected_token

__all__ = [
    "ENV_TOKEN_FILE",
    "AppConfig",
    "AppConfigError",
    "ServeConfig",
    "build_streamable_http_app",
    "load_serve_config",
    "serve",
]

#: Optional file containing the bearer token. Docker/Kubernetes secrets are
#: mounted as files, and most secret stores (Doppler, Vault, Swarm secrets)
#: can write one. The file must not be world-readable.
ENV_TOKEN_FILE = "MCP_HTTP_AUTH_TOKEN_FILE"

DEFAULT_PATH = "/mcp"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_REQUIRED_SCOPE = "mcp:access"


class AppConfigError(RuntimeError):
    """The HTTP application cannot be built with the given configuration."""


def _read_token_from_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise AppConfigError(
            f"{ENV_TOKEN_FILE} points at {path!r}, which cannot be read ({exc.strerror})"
        ) from exc


def resolve_token(env: dict[str, str] | None = None) -> str:
    """Resolve the expected token from the environment or token file.

    Precedence: an explicit ``MCP_HTTP_AUTH_TOKEN`` wins over the file. Both
    missing is a startup error. Nothing is ever logged or echoed.
    """
    source = os.environ if env is None else env
    if source.get(ENV_TOKEN):
        return load_expected_token(source)
    path = source.get(ENV_TOKEN_FILE)
    if not path:
        # Canonical fail-closed message naming the missing variable.
        load_expected_token(source)
        raise AssertionError("unreachable")  # pragma: no cover
    token = _read_token_from_file(path)
    if not token:
        raise AppConfigError(
            f"{ENV_TOKEN_FILE} ({path!r}) is empty; refusing to start an "
            "unauthenticated endpoint."
        )
    return load_expected_token({ENV_TOKEN: token})


@dataclass(frozen=True, repr=False)
class AppConfig:
    """Everything needed to build the HTTP application.

    Frozen and ``repr=False`` because it holds the expected token.
    """

    expected_token: str
    path: str = DEFAULT_PATH
    required_scope: str = DEFAULT_REQUIRED_SCOPE
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_origins: tuple[str, ...] = ()
    json_response: bool = False
    stateless_http: bool = False
    max_sessions: int = 100
    session_idle_timeout: float = 300.0
    max_request_body_size: int = 1_048_576
    normalise_prefixed_paths: bool = True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"AppConfig(path={self.path!r}, allowed_hosts={self.allowed_hosts!r},"
            f" json_response={self.json_response}, stateless_http={self.stateless_http},"
            f" max_sessions={self.max_sessions}, token=<redacted>)"
        )

    __str__ = __repr__


class PrefixNormalisingASGIApp:
    """Re-root a request whose path was stripped by a front proxy.

    Coolify's Traefik front end and Kubernetes ingress controllers commonly
    mount an app at a prefix and strip it before forwarding, which can leave
    the request arriving as ``""`` (empty path) with no root path recorded.
    The MCP session manager treats a non-``/mcp`` path as a 404 with no body,
    which is indistinguishable from a broken deployment.

    This shim rewrites the scope so the SDK's route always matches, and leaves
    ``root_path`` empty so generated URLs do not carry a double prefix.
    Requests that already arrive at the right path pass through untouched.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self.path = path

    @property
    def state(self):
        """Expose the wrapped app's ``state`` so wiring stays introspectable."""
        return self.app.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") in {"http", "websocket"}:
            raw_path = scope.get("path") or ""
            # Only a fully stripped endpoint is rewritten. A suffix match or an
            # unrelated root_path must never make a different URL serve MCP.
            if raw_path in ("", "/"):
                scope = dict(scope)
                scope["path"] = self.path
                scope["root_path"] = ""
        await self.app(scope, receive, send)


def _host_patterns(hosts: tuple[str, ...]) -> list[str]:
    """Expand bare hostnames to also match ``host:port`` Host headers.

    The SDK matches ``allowed_hosts`` entries either exactly or via a
    ``<host>:*`` wildcard-port pattern. A client always sends ``host:port`` for
    a non-default port, so an allowlist of bare hostnames would reject every
    real request with ``421 Invalid Host header`` — which is what a test
    caught. Both forms are registered: the plain entry covers the default-port
    case, the wildcard covers everything else.
    """
    patterns: list[str] = []
    for host in hosts:
        patterns.append(host)
        if not host.endswith(":*"):
            patterns.append(f"{host}:*")
    return patterns


def _origin_patterns(origins: tuple[str, ...]) -> list[str]:
    """Expand bare origins to also match an explicit port.

    ``Origin`` is only sent by browsers. Bare origins cover the default port;
    ``origin:*`` covers an explicit port. An empty allowlist means "no Origin
    header is accepted", which is the correct posture for a server that only
    machine clients call.
    """
    patterns: list[str] = []
    for origin in origins:
        patterns.append(origin)
        if not origin.endswith(":*"):
            patterns.append(f"{origin}:*")
    return patterns


def build_streamable_http_app(server: Any, config: AppConfig) -> ASGIApp:
    """Return a Starlette ASGI app exposing ``server`` on authenticated HTTP.

    ``server`` is an ``mcp.server.mcpserver.MCPServer``; it is mounted through
    :meth:`MCPServer.streamable_http_app`, so the tools, schemas, annotations
    and error envelopes are the same objects the stdio transport serves.
    """
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.transport_security import TransportSecuritySettings

    verifier = BearerTokenVerifier(config.expected_token, required_scope=config.required_scope)

    if not config.path.startswith("/"):
        raise AppConfigError(f"path must start with '/': {config.path!r}")
    if not config.allowed_hosts:
        raise AppConfigError(
            "allowed_hosts must not be empty; DNS-rebinding protection with an "
            "empty allowlist rejects every request"
        )

    # ``isinstance`` is unusable here: the SDK's ``TokenVerifier`` is a
    # non-runtime Protocol, so isinstance raises TypeError. Assert the
    # structural contract the SDK actually relies on instead.
    missing = [name for name in ("verify_token",) if not callable(getattr(verifier, name, None))]
    if missing:
        raise AppConfigError(f"token verifier is missing required method(s): {missing}")

    auth_settings = AuthSettings(
        # Not used beyond pydantic validation: no OAuth authorization server is
        # wired in, so no metadata endpoint is served and no client is
        # redirected here. It is set to the local bind address rather than to
        # any real hostname so a misconfiguration cannot advertise an
        # attacker-supplied URL.
        issuer_url="http://localhost",
        resource_server_url="http://localhost",
        required_scopes=[config.required_scope] if config.required_scope else None,
        # The shared secret is the only credential; there is no RFC 8707
        # resource indicator to validate against. Set explicitly so the SDK
        # does not emit its deprecation warning.
        validate_token_resource=False,
    )

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # Bare names are expanded to ``name:*`` so any port is accepted, while
        # still being an explicit closed list: a Host header that is not one of
        # these names is rejected with 421. No wildcard "*" entry is ever added.
        allowed_hosts=_host_patterns(config.allowed_hosts),
        allowed_origins=_origin_patterns(config.allowed_origins),
    )

    # ``MCPServer.__init__`` owns ``token_verifier`` and ``auth`` (both are
    # constructor-only in mcp 2.3.0); ``streamable_http_app()`` takes only the
    # transport options. Setting these three fields before the Starlette app is
    # built is what makes the SDK wrap the endpoint in ``RequireAuthMiddleware``
    # and serve protected-resource metadata — there is no public setter.
    #
    # ``settings.auth`` must be set too: ``streamable_http_app`` reads it to
    # decide whether to install the bearer middleware and to build the
    # ``WWW-Authenticate`` resource-metadata URL. Leaving it unset alongside a
    # verifier would silently produce an unauthenticated endpoint, which is
    # exactly the failure this package exists to prevent, so the assignment is
    # asserted below rather than assumed.
    #
    # This mutates the caller's server object, which is deliberate: a server's
    # ``main()`` builds it for this transport. A server that also served stdio
    # from the same object would still serve stdio unauthenticated, because the
    # stdio transport never reads these fields.
    settings = getattr(server, "settings", None)
    if settings is None or not hasattr(settings, "auth"):
        raise AppConfigError(
            "the supplied server object does not expose MCPServer.settings.auth; "
            "refusing to build an endpoint that may not be authenticated"
        )
    previous_auth = getattr(settings, "auth", None)
    settings.auth = auth_settings
    server._token_verifier = verifier
    server._auth_settings = auth_settings
    if getattr(server, "_token_verifier", None) is not verifier or getattr(settings, "auth", None) is not auth_settings:
        settings.auth = previous_auth  # pragma: no cover - defensive rollback
        raise AppConfigError("could not enable bearer authentication on this MCPServer")

    app = server.streamable_http_app(
        streamable_http_path=config.path,
        json_response=config.json_response,
        stateless_http=config.stateless_http,
        max_request_body_size=config.max_request_body_size,
        session_idle_timeout=config.session_idle_timeout,
        max_sessions=config.max_sessions,
        transport_security=transport_security,
        host=config.allowed_hosts[0],
    )

    # Exposed for tests and operators to assert which verifier is wired in.
    app.state.mcp_token_verifier = verifier
    app.state.mcp_app_config = config

    if config.normalise_prefixed_paths:
        return PrefixNormalisingASGIApp(app, config.path)
    return app


@dataclass(frozen=True)
class ServeConfig:
    """Process-level configuration for :func:`serve`."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ServeConfig":
        source = os.environ if env is None else env
        return cls(
            host=source.get("MCP_HTTP_HOST") or DEFAULT_HOST,
            port=int(source.get("MCP_HTTP_PORT") or DEFAULT_PORT),
            log_level=(source.get("MCP_HTTP_LOG_LEVEL") or "INFO").upper(),
        )


def load_serve_config(env: dict[str, str] | None = None) -> ServeConfig:
    return ServeConfig.from_env(env)


def serve(server: Any, config: AppConfig, serve_config: ServeConfig | None = None) -> None:
    """Run ``server`` on Streamable HTTP until interrupted.

    Synchronous, mirroring ``MCPServer.run("stdio")`` so server entry points
    read the same either way.
    """
    import uvicorn

    if serve_config is None:
        serve_config = load_serve_config()

    logging.basicConfig(
        level=getattr(logging, serve_config.log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_streamable_http_app(server, config)
    uvicorn.run(
        app,
        host=serve_config.host,
        port=serve_config.port,
        log_level=serve_config.log_level.lower(),
        # The proxy in front of this container terminates TLS and is the only
        # thing that should be able to reach it; do not trust forwarded headers
        # for anything security-relevant.
        forwarded_allow_ips="",
        server_header=False,
        date_header=False,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-http-transport",
        description="Serve an MCP server over authenticated Streamable HTTP.",
    )
    parser.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST") or DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT") or DEFAULT_PORT))
    parser.add_argument("--path", default=os.environ.get("MCP_HTTP_PATH") or DEFAULT_PATH)
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="Allowed Host header value; repeatable. Defaults to the bind host.",
    )
    return parser.parse_args(argv)