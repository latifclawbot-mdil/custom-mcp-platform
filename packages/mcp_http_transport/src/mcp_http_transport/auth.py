"""Static bearer-token authentication for the HTTP transport.

Design notes, and why each choice was made:

* The token is compared with :func:`hmac.compare_digest`, so a wrong token
  takes the same time to reject as a right one. A string ``==`` would leak the
  matching prefix length.
* The expected token is read from the process environment once at startup. It
  is never logged, never included in an error message, and never returned in a
  response body. Error messages quote environment *names*, not values.
* There is no default token. An unset/short token is a startup error, not a
  permissive mode: a server that expects no token is a server that is open.
* The verifier is intentionally not an OAuth authorization server. Upstream
  deployments front this endpoint with the platform's own TLS-terminating
  proxy, and the token is a shared secret between LiteLLM and this container.
  Anything stronger (JWT/OAuth) belongs in a gateway, not here.
"""

from __future__ import annotations

import hmac
import os
import secrets
from typing import Any

#: Environment variable holding the expected bearer token.
ENV_TOKEN = "MCP_HTTP_AUTH_TOKEN"

#: Environment variables bounding accepted token length, for tests and operators.
ENV_MIN_TOKEN_LENGTH = "MCP_HTTP_MIN_TOKEN_LENGTH"
ENV_MAX_TOKEN_LENGTH = "MCP_HTTP_MAX_TOKEN_LENGTH"

DEFAULT_MIN_TOKEN_LENGTH = 16
DEFAULT_MAX_TOKEN_LENGTH = 512


class AuthConfigError(RuntimeError):
    """The server cannot start with the given authentication configuration.

    The message never contains token material.
    """


def generate_token(nbytes: int = 32) -> str:
    """Generate a high-entropy URL-safe token for an operator to store in a secret."""
    return secrets.token_urlsafe(nbytes)





def load_expected_token(env: dict[str, str] | None = None) -> str:
    """Return the expected bearer token, or raise :class:`AuthConfigError`.

    Fails closed on: missing, empty, whitespace-only, too short, too long, or
    containing control characters. A token with a trailing newline is the
    classic result of ``echo "$TOKEN" > file`` and would silently never match,
    so it is rejected loudly instead of being stripped silently.
    """
    source = os.environ if env is None else env

    # Bounds are read from the caller-supplied mapping first: an explicit dict
    # (tests, programmatic use) must be the whole configuration, otherwise a
    # test could pass or fail depending on the ambient environment.
    def bound(name: str, default: int) -> int:
        raw = source.get(name, os.environ.get(name))
        if raw is None or raw.strip() == "":
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise AuthConfigError(f"{name} must be an integer") from exc
        if value < 0:
            raise AuthConfigError(f"{name} must not be negative")
        return value

    source = os.environ if env is None else env
    raw = source.get(ENV_TOKEN)
    if raw is None or raw == "":
        raise AuthConfigError(
            f"{ENV_TOKEN} is required: set it to the shared bearer token this "
            "server must see. Refusing to start an unauthenticated endpoint."
        )
    if raw != raw.strip():
        raise AuthConfigError(
            f"{ENV_TOKEN} has leading or trailing whitespace; this usually means "
            "it was written with `echo` rather than `printf`."
        )

    minimum = bound(ENV_MIN_TOKEN_LENGTH, DEFAULT_MIN_TOKEN_LENGTH)
    maximum = bound(ENV_MAX_TOKEN_LENGTH, DEFAULT_MAX_TOKEN_LENGTH)
    if minimum > maximum:
        raise AuthConfigError(
            f"{ENV_MIN_TOKEN_LENGTH} ({minimum}) exceeds {ENV_MAX_TOKEN_LENGTH} ({maximum})"
        )
    if len(raw) < minimum:
        raise AuthConfigError(
            f"{ENV_TOKEN} is shorter than the {minimum}-character minimum "
            "configured by "
            f"{ENV_MIN_TOKEN_LENGTH}; generate one with "
            "`python -m mcp_http_transport.generate_token`."
        )
    if len(raw) > maximum:
        raise AuthConfigError(
            f"{ENV_TOKEN} is longer than the {maximum}-character maximum "
            f"configured by {ENV_MAX_TOKEN_LENGTH}."
        )
    if any(character.isspace() or ord(character) < 0x20 for character in raw):
        raise AuthConfigError(
            f"{ENV_TOKEN} contains whitespace or control characters, which "
            "cannot survive an HTTP header."
        )
    return raw


class BearerTokenVerifier:
    """SDK ``TokenVerifier`` implementation for a single shared secret.

    Implements :class:`mcp.server.auth.provider.TokenVerifier` structurally
    (``async def verify_token(token) -> AccessToken | None``). Returning
    ``None`` is what makes the SDK's ``RequireAuthMiddleware`` answer with a
    ``401``/``403`` and a ``WWW-Authenticate`` header instead of running the
    MCP request, so an unauthenticated caller never reaches
    ``initialize``.
    """

    def __init__(self, expected_token: str, *, required_scope: str = "mcp:access") -> None:
        if not expected_token:
            raise AuthConfigError("expected_token must not be empty")
        self._expected = expected_token
        self.required_scope = required_scope
        # Counters for operators; contains no caller data.
        self.rejected = 0
        self.accepted = 0

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"BearerTokenVerifier(required_scope={self.required_scope!r}, "
            f"token=<redacted>, accepted={self.accepted}, rejected={self.rejected})"
        )

    __str__ = __repr__

    async def verify_token(self, token: str) -> Any | None:
        """Return an ``AccessToken`` when ``token`` matches, else ``None``."""
        if not isinstance(token, str) or not token:
            self.rejected += 1
            return None
        # hmac.compare_digest needs both sides to be ASCII-safe bytes; a
        # non-ASCII candidate can never match a token this server accepted at
        # startup (load_expected_token rejects non-ASCII-free control chars,
        # but unicode letters would pass it), so fall back to a rejection.
        try:
            match = hmac.compare_digest(token.encode("utf-8"), self._expected.encode("utf-8"))
        except (UnicodeEncodeError, TypeError):  # pragma: no cover - defensive
            self.rejected += 1
            return None
        if not match:
            self.rejected += 1
            return None

        self.accepted += 1
        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token=token,
            client_id="mcp-http-transport",
            scopes=[self.required_scope] if self.required_scope else [],
        )