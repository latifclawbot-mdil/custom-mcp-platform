"""Two-phase confirmation for write operations.

The approval model
------------------
MCP has no approval primitive. Tool annotations are advisory and a client can
call any advertised tool at any time, so "approval-gated" has to be enforced by
the server or it is not enforced at all.

This module implements the smallest thing that is actually a gate:

1. A write call without ``confirmation_token`` does not execute. It returns the
   **plan** — the resolved target, the effect, and a digest of the exact
   arguments — plus a ``confirmation_token`` derived from that digest.
2. A second call carrying that token executes, but only if the arguments hash to
   exactly the same digest and the token has not expired.

Consequences worth stating plainly:

* The token is **stateless**: an HMAC over the canonical argument digest, an
  issue timestamp, and a server-held secret. There is no token store to expire,
  no row to leak, and no way to replay a token against different arguments.
* Because it binds the arguments, the human approves *what will happen*, not
  "the next write". Changing the target, the type, or a scope between the plan
  and the apply invalidates the token.
* It is not a human check. It converts an irreversible single call into a
  reviewed two-step, and the review happens in the client's transcript. A model
  can produce both halves itself if its client lets it; only a client-side
  approval prompt makes the human part real. That limitation is reported, not
  papered over.

Nothing here is a substitute for the effect classification in :mod:`.gate`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "ConfirmationError",
    "ConfirmationVerifier",
    "DEFAULT_TTL_SECONDS",
    "ENV_CONFIRM_SECRET",
    "ENV_CONFIRM_SECRET_FILE",
    "canonical_digest",
    "load_confirm_secret",
    "redacted_arguments",
]

#: Environment variable holding the HMAC key. Generated into the deployment's
#: secret store; never derived from something an attacker can read from a
#: response, because a ciphertext-echo signature scheme would be worthless.
ENV_CONFIRM_SECRET = "LOGTO_MCP_CONFIRM_SECRET"

#: Preferred source: a mounted file, like every other secret this server takes.
#: An environment value is still accepted (so existing deployments keep
#: working), but a file keeps the key out of `docker inspect` and out of a
#: dumped process environment. If both are set the file wins.
ENV_CONFIRM_SECRET_FILE = "LOGTO_MCP_CONFIRM_SECRET_FILE"

DEFAULT_TTL_SECONDS = 900

_MIN_SECRET_BYTES = 32

#: Argument names whose values are secrets. They are included in the digest (so
#: the token binds them) but redacted from the returned plan. ``get_application``
#: -style reads never carry these; the writer operations do.
SENSITIVE_ARGUMENTS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "client_secret",
        "description",  # can hold operator commentary, still redacted in plans
    }
)


class ConfirmationError(RuntimeError):
    """A write was attempted without a usable confirmation.

    ``code`` lets the MCP layer return a structured refusal rather than a stack
    trace. The message never contains a token or a secret.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def load_confirm_secret(env: Mapping[str, str] | None = None) -> bytes:
    """Resolve the HMAC key, failing closed if it is missing or too short.

    Precedence: ``LOGTO_MCP_CONFIRM_SECRET_FILE`` wins over the inline
    environment value. A file that is unreadable, empty, multi-line, or
    group/world-readable is an error rather than a fallback to the env value:
    silently using a different key would invalidate every outstanding
    confirmation token, and silently ignoring a bad file would hide a
    misconfigured mount.
    """
    source = os.environ if env is None else env
    path = (source.get(ENV_CONFIRM_SECRET_FILE) or "").strip()
    if path:
        raw = _read_secret_file(path)
    else:
        raw = source.get(ENV_CONFIRM_SECRET) or ""
    if not raw:
        raise ConfirmationError(
            f"neither {ENV_CONFIRM_SECRET_FILE} nor {ENV_CONFIRM_SECRET} is set; the "
            "server cannot verify confirmations and will not serve writes without it",
            code="confirm_secret_missing",
        )
    key = raw.encode("utf-8")
    if len(key) < _MIN_SECRET_BYTES:
        raise ConfirmationError(
            f"{ENV_CONFIRM_SECRET} must be at least {_MIN_SECRET_BYTES} bytes",
            code="confirm_secret_too_short",
        )
    return key


def _read_secret_file(path: str) -> str:
    """Read a key file, refusing one that is loose or malformed."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ConfirmationError(
            f"{ENV_CONFIRM_SECRET_FILE} points at {path!r}, which cannot be read "
            f"({exc.strerror})",
            code="confirm_secret_unreadable",
        ) from exc
    if st.st_mode & 0o077:
        raise ConfirmationError(
            f"{ENV_CONFIRM_SECRET_FILE} file {path!r} is accessible by group or others "
            f"(mode {oct(st.st_mode & 0o777)}); expected 0600 or stricter",
            code="confirm_secret_permissions",
        )
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read()
    except OSError as exc:
        raise ConfirmationError(
            f"{ENV_CONFIRM_SECRET_FILE} file {path!r} cannot be read ({exc.strerror})",
            code="confirm_secret_unreadable",
        ) from exc
    value = value.strip()
    if not value:
        raise ConfirmationError(
            f"{ENV_CONFIRM_SECRET_FILE} file {path!r} is empty",
            code="confirm_secret_empty",
        )
    if "\n" in value or "\r" in value:
        raise ConfirmationError(
            f"{ENV_CONFIRM_SECRET_FILE} file {path!r} contains more than one line",
            code="confirm_secret_malformed",
        )
    return value


def generate_confirm_secret(nbytes: int = 32) -> str:
    """Generate a key for the operator to place in the deployment secret."""
    return secrets.token_urlsafe(nbytes)


def _canonical(value: Any) -> str:
    """Stable JSON for hashing: sorted keys, no whitespace, explicit types."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def canonical_digest(operation: str, arguments: Mapping[str, Any]) -> str:
    """Digest of what the call will do.

    ``operation`` is part of the digest, so a token minted for ``update_user``
    cannot authorise ``create_application_secret`` even with identical
    arguments.

    The confirmation token itself is excluded, otherwise a token could not be
    verified against the arguments it was minted from.
    """
    payload = {
        "operation": operation,
        "arguments": {k: v for k, v in arguments.items() if k != "confirmation_token"},
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def redacted_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments as they may appear in a plan shown to the caller."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if key == "confirmation_token":
            continue
        if key in SENSITIVE_ARGUMENTS:
            out[key] = "<redacted>" if value not in (None, "") else value
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class ConfirmationVerifier:
    """Mint and verify confirmation tokens for one server process."""

    secret: bytes
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def __post_init__(self) -> None:
        if len(self.secret) < _MIN_SECRET_BYTES:
            raise ConfirmationError(
                f"confirmation secret must be at least {_MIN_SECRET_BYTES} bytes",
                code="confirm_secret_too_short",
            )
        if self.ttl_seconds <= 0:
            raise ConfirmationError(
                "confirmation ttl must be positive", code="confirm_ttl_invalid"
            )

    def _sign(self, digest: str, issued_at: int) -> str:
        message = f"{digest}.{issued_at}".encode("ascii")
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def mint(self, operation: str, arguments: Mapping[str, Any]) -> str:
        """Return the token to hand back with the plan."""
        digest = canonical_digest(operation, arguments)
        issued_at = int(time.time())
        return f"{issued_at}.{self._sign(digest, issued_at)}"

    def verify(
        self, operation: str, arguments: Mapping[str, Any], token: str, *, now: int | None = None
    ) -> None:
        """Raise :class:`ConfirmationError` unless ``token`` authorises this call."""
        if not token:
            raise ConfirmationError(
                "this operation is a write and requires a confirmation_token; call it "
                "with dry_run=true first and repeat the call with the returned token",
                code="confirmation_required",
            )
        issued_raw, _, signature = token.partition(".")
        if not issued_raw.isdigit() or not signature:
            raise ConfirmationError(
                "malformed confirmation_token", code="confirmation_malformed"
            )
        issued_at = int(issued_raw)
        expected = self._sign(canonical_digest(operation, arguments), issued_at)
        if not hmac.compare_digest(expected, signature):
            # Either the arguments changed after the plan, or the token came
            # from a different server/secret. Say both; do not say which.
            raise ConfirmationError(
                "confirmation_token does not match this operation's arguments "
                "(arguments changed since the plan, or the token was issued by "
                "another server)",
                code="confirmation_mismatch",
            )
        current = int(time.time()) if now is None else now
        age = current - issued_at
        if age > self.ttl_seconds:
            raise ConfirmationError(
                f"confirmation_token expired ({age}s old, limit {self.ttl_seconds}s); "
                "re-run the dry run",
                code="confirmation_expired",
            )
        if age < -60:
            raise ConfirmationError(
                "confirmation_token is from the future; check the server clock",
                code="confirmation_clock_skew",
            )
