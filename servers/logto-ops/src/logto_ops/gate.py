"""Policy gate for a write-capable Logto Management API MCP server.

This module decides whether an operation may run, and refuses at *startup* if
the deployment is not configured for the mode it is about to serve:

``read``
    Every operation classified read-only. No high-authority tool is served.

``apply`` (default)
    Mutations are allowed but only through the confirmation gate: a call must
    carry the ``confirmation_token`` returned by the previous dry run of the
    same operation with the same non-secret arguments.

``admin``
    Mutations may run without a confirmation token. This exists so an operator
    can deliberately choose a fully autonomous server; it is refused unless
    ``LOGTO_MCP_ALLOW_UNCONFIRMED_WRITES=true`` is also set, so it cannot be
    reached by a typo in a mode string.

Delete operations are not in any mode's surface: they are denied by
:data:`DENIED_OPERATIONS` before the mode is even consulted.

The classification below is the authorization boundary. MCP tool annotations
(``readOnlyHint`` and friends) are advisory metadata for clients and are not
used here for any decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

__all__ = [
    "DENIED_OPERATIONS",
    "Effect",
    "GateConfig",
    "GateConfigError",
    "Mode",
    "READ_OPERATIONS",
    "WRITE_OPERATIONS",
    "classify",
    "load_gate",
]

Mode = Literal["read", "apply", "admin"]
Effect = Literal["read", "write"]

ENV_MODE = "LOGTO_MCP_MODE"
ENV_ALLOW_UNCONFIRMED = "LOGTO_MCP_ALLOW_UNCONFIRMED_WRITES"
ENV_ALLOW_DELETES = "LOGTO_MCP_ALLOW_DELETES"

VALID_MODES: tuple[Mode, ...] = ("read", "apply", "admin")

#: Operations this server implements and classifies as reads.
READ_OPERATIONS: frozenset[str] = frozenset(
    {
        "logto_health",
        "list_applications",
        "get_application",
        "list_application_roles",
        "list_application_secrets",
        "list_users",
        "get_user",
        "list_user_roles",
        "list_roles",
        "get_role",
        "list_role_scopes",
        "list_role_applications",
        "list_connectors",
        "get_connector",
        "get_sign_in_experience",
        "list_logs",
        "get_log",
        "list_organizations",
        "get_organization",
        "list_organization_roles",
        "list_resources",
        "get_resource",
        "list_resource_scopes",
    }
)

#: Operations this server implements and classifies as writes. Every one of
#: these requires a confirmation token in ``apply`` mode.
WRITE_OPERATIONS: frozenset[str] = frozenset(
    {
        "create_m2m_application",
        "create_application_secret",
        "assign_application_roles",
        "update_application",
        "create_role",
        "update_role",
        "assign_role_scopes",
        "update_sign_in_experience",
        "update_connector",
        "refresh_connector_token",
        "create_user",
        "update_user",
        "set_user_suspended",
        "update_user_password",
        "assign_user_roles",
        "create_organization",
        "update_organization",
        "add_organization_members",
    }
)

#: Known Logto Management API delete operations. None of them is implemented or
#: reachable. The ``delete_*`` prefix rule in :func:`classify` is the second,
#: deliberately redundant, line of defence: a future handler named
#: ``delete_something`` would be denied even if someone forgot this set.
DENIED_OPERATIONS: frozenset[str] = frozenset(
    {
        "delete_application",
        "delete_application_secret",
        "delete_application_secret_by_name",
        "delete_role",
        "delete_user",
        "delete_user_identities",
        "delete_organization",
        "delete_connector",
        "delete_resource",
        "delete_resource_scope",
        "delete_organization_role",
        "delete_hook",
        "delete_domain",
    }
)

DENIED_PREFIXES: tuple[str, ...] = ("delete_",)


class GateConfigError(RuntimeError):
    """The server is configured for an effect it must not perform.

    Raised at startup, never per request. The message names environment
    variables and operation classes only.
    """


@dataclass(frozen=True)
class GateConfig:
    """Resolved gate configuration for one running process."""

    mode: Mode
    allow_unconfirmed_writes: bool
    allow_deletes: bool

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise GateConfigError(
                f"{ENV_MODE} must be one of {', '.join(VALID_MODES)}; got {self.mode!r}"
            )
        if self.mode == "admin" and not self.allow_unconfirmed_writes:
            raise GateConfigError(
                f"{ENV_MODE}=admin serves unconfirmed writes and requires "
                f"{ENV_ALLOW_UNCONFIRMED}=true. Refusing to start."
            )
        if self.allow_deletes:
            raise GateConfigError(
                f"{ENV_ALLOW_DELETES}=true is not supported: this server implements no "
                "delete operation. Remove the variable; the setting is intentionally "
                "not wired to anything."
            )

    @property
    def serves_writes(self) -> bool:
        return self.mode in ("apply", "admin")

    @property
    def writes_need_confirmation(self) -> bool:
        return self.mode == "apply"


def load_gate(env: Mapping[str, str] | None = None) -> GateConfig:
    """Build :class:`GateConfig` from the environment, failing closed."""
    source = os.environ if env is None else env
    mode = (source.get(ENV_MODE) or "apply").strip().lower()
    allow_unconfirmed = (source.get(ENV_ALLOW_UNCONFIRMED) or "").strip().lower() == "true"
    allow_deletes = (source.get(ENV_ALLOW_DELETES) or "").strip().lower() == "true"
    return GateConfig(
        mode=mode,  # type: ignore[arg-type]
        allow_unconfirmed_writes=allow_unconfirmed,
        allow_deletes=allow_deletes,
    )


def _matches_denied_prefix(operation: str) -> bool:
    return any(operation.startswith(prefix) for prefix in DENIED_PREFIXES)


def classify(operation: str) -> Effect:
    """Return the effect of ``operation``, or refuse to classify it.

    An operation that is in neither declared set is an error, not an implicit
    read: a handler added without classification must not silently become
    either readable or writable.
    """
    if operation in DENIED_OPERATIONS or _matches_denied_prefix(operation):
        raise GateConfigError(f"{operation!r} is a denied operation")
    if operation in READ_OPERATIONS:
        return "read"
    if operation in WRITE_OPERATIONS:
        return "write"
    raise GateConfigError(
        f"{operation!r} is not classified as a read or write operation; refusing to serve it"
    )


def assert_servable(operations: Iterable[str], gate: GateConfig) -> None:
    """Startup check: every implemented operation must be servable in this mode."""
    problems: list[str] = []
    for operation in operations:
        try:
            effect = classify(operation)
        except GateConfigError as exc:
            problems.append(str(exc))
            continue
        if effect == "write" and not gate.serves_writes:
            problems.append(
                f"{operation!r} is a write operation but {ENV_MODE}={gate.mode}"
            )
    if problems:
        raise GateConfigError(
            "refusing to start with this operation set: " + "; ".join(sorted(problems))
        )
