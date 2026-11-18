"""Operation registry: every tool this server serves, with its effect.

The registry is the authorization boundary. Each operation declares:

* a name (also the MCP tool name);
* whether it reads or writes (see :mod:`.gate`);
* the accepted argument names — an unknown argument is a call-time error, never
  silently dropped by the SDK;
* a handler that performs the effect through :class:`~.client.LogtoClient`.

Write handlers are two-phase. Middleware validates and gates; handlers execute.
The confirmation token is minted and verified in the middleware
(:mod:`.server`) so a handler can never run without having passed the gate.

Scope of this server, as specified:

* both Logto control planes' objects — applications (including M2M), users,
  roles, role/scope assignment, connectors, sign-in experience, organizations,
  audit-log reads;
* no delete operations at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .client import LogtoClient
from .confirm import redacted_arguments
from .gate import Effect

__all__ = [
    "Operation",
    "OperationError",
    "REGISTRY",
    "ToolResult",
    "build_registry",
]

#: Maximum items returned to the model in one page. Reading 100 applications is
#: useful; pasting a whole tenant into a context window is not.
MAX_ITEMS_IN_RESULT = 100


class OperationError(RuntimeError):
    """A well-formed request that the operation itself refuses.

    Used for policy refusals (e.g. "this role is the Management API role") and
    for invalid argument combinations. The message is safe to show.
    """


@dataclass(frozen=True)
class ToolResult:
    """What a handler returns; rendered to MCP content by the server module."""

    payload: Any

    def as_text(self) -> str:
        import json

        return json.dumps(self.payload, indent=2, sort_keys=False, default=str)


@dataclass(frozen=True)
class Operation:
    name: str
    effect: Effect
    description: str
    accepted: frozenset[str]
    #: True when the effect is a create. The client then reports a transport
    #: failure as ambiguous instead of "nothing happened".
    non_idempotent: bool = False
    handler: Callable[..., ToolResult] | None = None

    def unknown_arguments(self, arguments: Mapping[str, Any]) -> list[str]:
        return sorted(k for k in arguments if k not in self.accepted and k != "confirmation_token" and k != "dry_run")


# Handlers ------------------------------------------------------------------

def _require_id(arguments: Mapping[str, Any], field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise OperationError(f"{field} must be a non-empty string")
    return value.strip()


def _require_str(arguments: Mapping[str, Any], field: str, *, max_length: int = 256) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise OperationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise OperationError(f"{field} must be at most {max_length} characters")
    return value


def _optional_str(arguments: Mapping[str, Any], field: str, *, max_length: int = 2048) -> str | None:
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperationError(f"{field} must be a string")
    if len(value) > max_length:
        raise OperationError(f"{field} must be at most {max_length} characters")
    return value


def _optional_bool(arguments: Mapping[str, Any], field: str) -> bool | None:
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise OperationError(f"{field} must be a boolean")
    return value


def _optional_int(arguments: Mapping[str, Any], field: str, *, minimum: int = 1) -> int | None:
    value = arguments.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise OperationError(f"{field} must be an integer")
    if value < minimum:
        raise OperationError(f"{field} must be >= {minimum}")
    return value


def _string_list(arguments: Mapping[str, Any], field: str, *, allow_empty: bool = True) -> list[str]:
    value = arguments.get(field)
    if value is None:
        if allow_empty:
            return []
        raise OperationError(f"{field} is required")
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise OperationError(f"{field} must be a list of non-empty strings")
    if not value and not allow_empty:
        raise OperationError(f"{field} must not be empty")
    return [v.strip() for v in value]


APPLICATION_TYPES: frozenset[str] = frozenset(
    {"Native", "SPA", "Traditional", "MachineToMachine", "Protected", "SAML"}
)


def _client(settings: Any) -> LogtoClient:
    return LogtoClient(settings)


# -- reads -----------------------------------------------------------------


def list_applications(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    app_type = _optional_str(arguments, "type")
    if app_type is not None and app_type not in APPLICATION_TYPES:
        raise OperationError(f"type must be one of {sorted(APPLICATION_TYPES)}")
    payload = client.list_page(
        "/applications",
        page=_optional_int(arguments, "page") or 1,
        page_size=_optional_int(arguments, "page_size"),
        params={"types": app_type} if app_type else None,
        operation="list_applications",
    )
    return ToolResult(payload)


def get_application(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    app_id = _require_id(arguments, "application_id")
    return ToolResult(client.request("GET", f"/applications/{app_id}", operation="get_application"))


def list_application_roles(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    app_id = _require_id(arguments, "application_id")
    return ToolResult(
        client.list_page(f"/applications/{app_id}/roles", page_size=100, operation="list_application_roles")
    )


def list_application_secrets(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    # Logto never returns secret material from this endpoint: it lists names and
    # creation times only. Included so an operator can see what exists.
    app_id = _require_id(arguments, "application_id")
    return ToolResult(client.request("GET", f"/applications/{app_id}/secrets", operation="list_application_secrets"))


def list_users(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        client.list_page(
            "/users",
            page=_optional_int(arguments, "page") or 1,
            page_size=_optional_int(arguments, "page_size"),
            params={
                "search": _optional_str(arguments, "search"),
                "isSuspended": _optional_bool(arguments, "is_suspended"),
            },
            operation="list_users",
        )
    )


def get_user(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    user_id = _require_id(arguments, "user_id")
    return ToolResult(client.request("GET", f"/users/{user_id}", operation="get_user"))


def list_user_roles(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    user_id = _require_id(arguments, "user_id")
    return ToolResult(client.list_page(f"/users/{user_id}/roles", page_size=100, operation="list_user_roles"))


def list_roles(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    role_type = _optional_str(arguments, "type")
    if role_type is not None and role_type not in ("User", "MachineToMachine"):
        raise OperationError("type must be 'User' or 'MachineToMachine'")
    return ToolResult(
        client.list_page(
            "/roles",
            page=_optional_int(arguments, "page") or 1,
            page_size=_optional_int(arguments, "page_size"),
            params={"type": role_type, "search": _optional_str(arguments, "search")},
            operation="list_roles",
        )
    )


def get_role(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    role_id = _require_id(arguments, "role_id")
    return ToolResult(client.request("GET", f"/roles/{role_id}", operation="get_role"))


def list_role_scopes(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    role_id = _require_id(arguments, "role_id")
    return ToolResult(client.list_page(f"/roles/{role_id}/scopes", page_size=100, operation="list_role_scopes"))


def list_role_applications(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    role_id = _require_id(arguments, "role_id")
    return ToolResult(client.list_page(f"/roles/{role_id}/applications", page_size=100, operation="list_role_applications"))


def list_connectors(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(client.request("GET", "/connectors", operation="list_connectors"))


def get_connector(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    connector_id = _require_id(arguments, "connector_id")
    return ToolResult(client.request("GET", f"/connectors/{connector_id}", operation="get_connector"))


def get_sign_in_experience(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(client.request("GET", "/sign-in-exp", operation="get_sign_in_experience"))


def list_logs(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        client.list_page(
            "/logs",
            page=_optional_int(arguments, "page") or 1,
            page_size=_optional_int(arguments, "page_size"),
            params={
                "userId": _optional_str(arguments, "user_id"),
                "applicationId": _optional_str(arguments, "application_id"),
                "logType": _optional_str(arguments, "log_type"),
            },
            operation="list_logs",
        )
    )


def get_log(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    log_id = _require_id(arguments, "log_id")
    return ToolResult(client.request("GET", f"/logs/{log_id}", operation="get_log"))


def list_organizations(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        client.list_page(
            "/organizations",
            page=_optional_int(arguments, "page") or 1,
            page_size=_optional_int(arguments, "page_size"),
            params={"q": _optional_str(arguments, "search")},
            operation="list_organizations",
        )
    )


def get_organization(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    organization_id = _require_id(arguments, "organization_id")
    return ToolResult(client.request("GET", f"/organizations/{organization_id}", operation="get_organization"))


def list_organization_roles(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        client.list_page(
            "/organization-roles",
            page=_optional_int(arguments, "page") or 1,
            page_size=_optional_int(arguments, "page_size"),
            operation="list_organization_roles",
        )
    )


def list_resources(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        client.list_page(
            "/resources",
            page=_optional_int(arguments, "page") or 1,
            page_size=_optional_int(arguments, "page_size"),
            operation="list_resources",
        )
    )


def get_resource(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    resource_id = _require_id(arguments, "resource_id")
    return ToolResult(client.request("GET", f"/resources/{resource_id}", operation="get_resource"))


def list_resource_scopes(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    resource_id = _require_id(arguments, "resource_id")
    return ToolResult(
        client.list_page(f"/resources/{resource_id}/scopes", page_size=100, operation="list_resource_scopes")
    )


# -- writes ----------------------------------------------------------------

#: Roles whose scope set includes the Logto Management API `all` scope. Assigning
#: one of these to a new M2M application mints a full-tenant administrator. The
#: server allows it (the operator asked for everything) but names the risk in the
#: plan so the confirmation is informed.
def _management_scope_ids(client: LogtoClient) -> set[str]:
    scopes = client.list_page("/resources/management-api/scopes", page_size=100, operation="list_resource_scopes")
    return {s.get("id") for s in scopes if isinstance(s.get("id"), str)}


def _role_is_management_admin(client: LogtoClient, role_id: str) -> bool:
    role_scopes = client.list_page(f"/roles/{role_id}/scopes", page_size=100, operation="list_role_scopes")
    if any(s.get("name") == "all" and s.get("resourceId") == "management-api" for s in role_scopes):
        return True
    # Fall back to id membership for older responses that omit resourceId.
    ids = _management_scope_ids(client)
    return any(s.get("id") in ids for s in role_scopes)


def create_m2m_application(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    name = _require_str(arguments, "name")
    description = _optional_str(arguments, "description")
    role_ids = _string_list(arguments, "role_ids")

    body: dict[str, Any] = {"name": name, "type": "MachineToMachine"}
    if description is not None:
        body["description"] = description

    created = client.request(
        "POST", "/applications", json_body=body, operation="create_m2m_application", idempotent=False
    )
    app_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(app_id, str):
        raise OperationError("Logto did not return an application id")

    warnings: list[str] = []
    assigned: list[str] = []
    for role_id in role_ids:
        if _role_is_management_admin(client, role_id):
            warnings.append(
                f"role {role_id} grants the Logto Management API 'all' scope: this application "
                "will be a full-tenant administrator"
            )
        client.request(
            "POST",
            f"/applications/{app_id}/roles",
            json_body={"roleIds": [role_id]},
            operation="assign_application_roles",
            idempotent=False,
        )
        assigned.append(role_id)

    return ToolResult({"id": app_id, "type": created.get("type"), "name": created.get("name"),
                       "role_ids": assigned, "warnings": warnings})


def create_application_secret(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    app_id = _require_id(arguments, "application_id")
    name = _require_str(arguments, "name", max_length=128)
    secret = client.request(
        "POST",
        f"/applications/{app_id}/secrets",
        json_body={"name": name},
        operation="create_application_secret",
        idempotent=False,
    )
    value = secret.get("value") if isinstance(secret, dict) else None
    # The value is returned exactly once by Logto. It is handed to the caller so
    # it can be stored; it is NOT written to logs. Callers should treat the tool
    # result as the only copy.
    return ToolResult(
        {
            "name": secret.get("name") if isinstance(secret, dict) else name,
            "value": value,
            "note": "Logto shows this secret once. Store it now; it cannot be read again.",
        }
    )


def assign_application_roles(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    app_id = _require_id(arguments, "application_id")
    role_ids = _string_list(arguments, "role_ids", allow_empty=False)
    warnings = [
        f"role {role_id} grants the Logto Management API 'all' scope"
        for role_id in role_ids
        if _role_is_management_admin(client, role_id)
    ]
    client.request(
        "POST",
        f"/applications/{app_id}/roles",
        json_body={"roleIds": role_ids},
        operation="assign_application_roles",
        idempotent=False,
    )
    return ToolResult({"application_id": app_id, "role_ids": role_ids, "warnings": warnings})


def update_application(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    app_id = _require_id(arguments, "application_id")
    body: dict[str, Any] = {}
    for field in ("name", "description"):
        if field in arguments:
            body[field] = _optional_str(arguments, field)
    if arguments.get("custom_client_metadata") is not None:
        metadata = arguments["custom_client_metadata"]
        if not isinstance(metadata, dict):
            raise OperationError("custom_client_metadata must be an object")
        body["customClientMetadata"] = metadata
    if not body:
        raise OperationError("nothing to update: provide name, description or custom_client_metadata")
    return ToolResult(
        client.request("PATCH", f"/applications/{app_id}", json_body=body, operation="update_application")
    )


def create_role(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    name = _require_str(arguments, "name", max_length=128)
    role_type = _require_str(arguments, "type")
    if role_type not in ("User", "MachineToMachine"):
        raise OperationError("type must be 'User' or 'MachineToMachine'")
    description = _optional_str(arguments, "description")
    body: dict[str, Any] = {"name": name, "type": role_type}
    if description is not None:
        body["description"] = description
    created = client.request("POST", "/roles", json_body=body, operation="create_role", idempotent=False)
    scope_ids = _string_list(arguments, "scope_ids")
    warnings: list[str] = []
    if scope_ids:
        client.request(
            "POST",
            f"/roles/{created.get('id')}/scopes",
            json_body={"scopeIds": scope_ids},
            operation="assign_role_scopes",
            idempotent=False,
        )
        warnings.append(
            "Logto never removes scope assignments from a role: an over-granted scope cannot be "
            "un-assigned through this server's surface; delete the role in the Console instead."
        )
    return ToolResult({"id": created.get("id"), "name": created.get("name"),
                       "type": created.get("type"), "scope_ids": scope_ids, "warnings": warnings})


def update_role(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    role_id = _require_id(arguments, "role_id")
    body: dict[str, Any] = {}
    for field in ("name", "description"):
        if field in arguments:
            body[field] = _optional_str(arguments, field)
    if not body:
        raise OperationError("nothing to update: provide name or description")
    return ToolResult(client.request("PATCH", f"/roles/{role_id}", json_body=body, operation="update_role"))


def assign_role_scopes(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    role_id = _require_id(arguments, "role_id")
    scope_ids = _string_list(arguments, "scope_ids", allow_empty=False)
    client.request(
        "POST", f"/roles/{role_id}/scopes", json_body={"scopeIds": scope_ids},
        operation="assign_role_scopes", idempotent=False,
    )
    return ToolResult(
        {
            "role_id": role_id,
            "scope_ids": scope_ids,
            "warnings": [
                "scope assignments are additive and cannot be removed through this server; "
                "removing a scope requires the Logto Console or a delete call this server does not expose"
            ],
        }
    )


SIGN_IN_EXPERIENCE_FIELDS: frozenset[str] = frozenset(
    {
        "signUp",
        "signIn",
        "socialSignIn",
        "passwordPolicy",
        "mfa",
        "adaptiveMfa",
        "passkeySignIn",
        "singleSignOnEnabled",
        "usernamePolicy",
        "sentinelPolicy",
        "verificationCodePolicy",
        "supportEmail",
        "termsOfUseUrl",
        "privacyPolicyUrl",
        "signUpProfileFields",
        "socialSignInConnectorTargets",
        "forgotPasswordMethods",
        "signInMode",
        "agreeToTermsPolicy",
        "emailBlocklistPolicy",
        "passwordExpiration",
        "trustedDevice",
        "unknownSessionRedirectUrl",
    }
)


def update_sign_in_experience(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    """Update sign-in experience fields.

    Deliberately field-by-field rather than a raw passthrough. Logto replaces the
    whole object on PUT, so a naive passthrough plus a missed field silently
    resets sign-in behaviour for every user in the tenant. Here the current
    object is read, only the named fields are replaced, and the remainder is put
    back unchanged.
    """
    body: dict[str, Any] = {}
    for field in SIGN_IN_EXPERIENCE_FIELDS:
        if field in arguments and arguments[field] is not None:
            body[field] = arguments[field]
    if not body:
        raise OperationError(
            "nothing to update: provide at least one of " + ", ".join(sorted(SIGN_IN_EXPERIENCE_FIELDS))
        )
    current = client.request("GET", "/sign-in-exp", operation="get_sign_in_experience")
    if not isinstance(current, dict):
        raise OperationError("could not read the current sign-in experience; refusing a blind PUT")
    merged = {**current, **body}
    return ToolResult(
        client.request("PUT", "/sign-in-exp", json_body=merged, operation="update_sign_in_experience")
    )


CONNECTOR_CONFIG_FIELDS: frozenset[str] = frozenset({"config", "metadata", "syncProfile", "name"})


def update_connector(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    connector_id = _require_id(arguments, "connector_id")
    body: dict[str, Any] = {}
    for field in ("config", "metadata", "syncProfile"):
        if field in arguments and arguments[field] is not None:
            body[field] = arguments[field]
    if not body:
        raise OperationError("nothing to update: provide config, metadata or syncProfile")
    return ToolResult(
        client.request("PATCH", f"/connectors/{connector_id}", json_body=body, operation="update_connector")
    )


def refresh_connector_token(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    connector_id = _require_id(arguments, "connector_id")
    # Logto's PUT (not PATCH) on a connector is "re-fetch/refresh its token".
    return ToolResult(
        client.request("PUT", f"/connectors/{connector_id}", json_body={},
                       operation="refresh_connector_token", idempotent=False)
    )


def create_user(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    body: dict[str, Any] = {}
    username = _optional_str(arguments, "username")
    if username:
        body["username"] = username
    primary_email = _optional_str(arguments, "primary_email")
    primary_phone = _optional_str(arguments, "primary_phone")
    if primary_email:
        body["primaryEmail"] = primary_email
    if primary_phone:
        body["primaryPhone"] = primary_phone
    password = _optional_str(arguments, "password", max_length=256)
    if password:
        body["password"] = password
    name = _optional_str(arguments, "name")
    if name:
        body["name"] = name
    if not body.get("username") and not body.get("primaryEmail") and not body.get("primaryPhone"):
        raise OperationError("at least one identifier is required: username, primary_email or primary_phone")
    created = client.request("POST", "/users", json_body=body, operation="create_user", idempotent=False)
    return ToolResult({k: v for k, v in created.items() if k != "password"} if isinstance(created, dict) else created)


def update_user(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    user_id = _require_id(arguments, "user_id")
    body: dict[str, Any] = {}
    for field, api_field in (
        ("name", "name"),
        ("avatar", "avatar"),
        ("username", "username"),
        ("primary_email", "primaryEmail"),
        ("primary_phone", "primaryPhone"),
    ):
        if field in arguments:
            body[api_field] = _optional_str(arguments, field)
    if "custom_data" in arguments:
        if not isinstance(arguments["custom_data"], dict):
            raise OperationError("custom_data must be an object")
        body["customData"] = arguments["custom_data"]
    if not body:
        raise OperationError("nothing to update")
    return ToolResult(client.request("PATCH", f"/users/{user_id}", json_body=body, operation="update_user"))


def set_user_suspended(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    user_id = _require_id(arguments, "user_id")
    suspended = _optional_bool(arguments, "suspended")
    if suspended is None:
        raise OperationError("suspended is required (true or false)")
    return ToolResult(
        client.request(
            "PATCH",
            f"/users/{user_id}/is-suspended",
            json_body={"isSuspended": suspended},
            operation="set_user_suspended",
        )
    )


def update_user_password(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    user_id = _require_id(arguments, "user_id")
    password = _optional_str(arguments, "password", max_length=256)
    if not password:
        raise OperationError("password is required")
    client.request(
        "PATCH", f"/users/{user_id}/password", json_body={"password": password},
        operation="update_user_password",
    )
    return ToolResult({"user_id": user_id, "password": "<redacted>", "updated": True})


def assign_user_roles(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    user_id = _require_id(arguments, "user_id")
    role_ids = _string_list(arguments, "role_ids", allow_empty=False)
    client.request(
        "POST", f"/users/{user_id}/roles", json_body={"roleIds": role_ids},
        operation="assign_user_roles", idempotent=False,
    )
    return ToolResult({"user_id": user_id, "role_ids": role_ids})


def create_organization(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    name = _require_str(arguments, "name", max_length=128)
    body: dict[str, Any] = {"name": name}
    description = _optional_str(arguments, "description")
    if description is not None:
        body["description"] = description
    if arguments.get("custom_data") is not None:
        if not isinstance(arguments["custom_data"], dict):
            raise OperationError("custom_data must be an object")
        body["customData"] = arguments["custom_data"]
    created = client.request(
        "POST", "/organizations", json_body=body, operation="create_organization", idempotent=False
    )
    return ToolResult(
        {
            **({"id": created.get("id")} if isinstance(created, dict) else {}),
            "note": "organization creation is not idempotent; on an ambiguous failure, "
                    "list organizations before retrying",
        }
    )


def update_organization(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    organization_id = _require_id(arguments, "organization_id")
    body: dict[str, Any] = {}
    for field, api_field in (("name", "name"), ("description", "description")):
        if field in arguments:
            body[api_field] = _optional_str(arguments, field)
    if arguments.get("custom_data") is not None:
        if not isinstance(arguments["custom_data"], dict):
            raise OperationError("custom_data must be an object")
        body["customData"] = arguments["custom_data"]
    if not body:
        raise OperationError("nothing to update")
    return ToolResult(
        client.request("PATCH", f"/organizations/{organization_id}", json_body=body,
                       operation="update_organization")
    )


def add_organization_members(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    organization_id = _require_id(arguments, "organization_id")
    user_ids = _string_list(arguments, "user_ids", allow_empty=False)
    # Logto documents the array form as "add the missing ones"; it is the
    # idempotent option, so a retry cannot duplicate membership.
    return ToolResult(
        client.request(
            "POST",
            f"/organizations/{organization_id}/users",
            json_body={"userIds": user_ids},
            operation="add_organization_members",
        )
    )


def logto_health(client: LogtoClient, arguments: Mapping[str, Any]) -> ToolResult:
    """Prove the credential works, without printing it."""
    me = client.request("GET", f"/applications/{client._settings.app_id}", operation="get_application")
    roles = client.list_page(
        f"/applications/{client._settings.app_id}/roles", page_size=100, operation="list_application_roles"
    )
    return ToolResult(
        {
            "base_url": client._settings.base_url,
            "app_id": client._settings.app_id,
            "app_type": me.get("type") if isinstance(me, dict) else None,
            "role_names": [r.get("name") for r in roles],
        }
    )


# Registry -----------------------------------------------------------------

def _op(name: str, effect: Effect, description: str, accepted: list[str], handler: Callable[..., ToolResult],
        non_idempotent: bool = False) -> Operation:
    return Operation(
        name=name,
        effect=effect,
        description=description,
        accepted=frozenset(accepted),
        non_idempotent=non_idempotent,
        handler=handler,
    )


REGISTRY: tuple[Operation, ...] = (
    _op("logto_health", "read", "Verify the M2M credential: returns the app type and its role names.",
        [], logto_health),
    # Applications
    _op("list_applications", "read",
        "List OIDC applications. Optional type filter: Native, SPA, Traditional, MachineToMachine, Protected, SAML.",
        ["page", "page_size", "type"], list_applications),
    _op("get_application", "read", "Get one application by id.", ["application_id"], get_application),
    _op("list_application_roles", "read", "List the roles assigned to an application.", ["application_id"],
        list_application_roles),
    _op("list_application_secrets", "read",
        "List secret NAMES for an application. Logto never returns secret values here.",
        ["application_id"], list_application_secrets),
    _op("create_m2m_application", "write",
        "Create a Machine-to-machine application and optionally assign M2M roles. "
        "Returns the application id; create a secret separately.",
        ["name", "description", "role_ids"], create_m2m_application, non_idempotent=True),
    _op("create_application_secret", "write",
        "Add a named client secret to an application. The value is returned ONCE.",
        ["application_id", "name"], create_application_secret, non_idempotent=True),
    _op("assign_application_roles", "write", "Assign M2M roles to an application (additive).",
        ["application_id", "role_ids"], assign_application_roles, non_idempotent=True),
    _op("update_application", "write",
        "Update an application's name, description or customClientMetadata.",
        ["application_id", "name", "description", "custom_client_metadata"], update_application),
    # Roles and scopes
    _op("list_roles", "read", "List roles. Optional type: User or MachineToMachine.", ["page", "page_size", "type", "search"],
        list_roles),
    _op("get_role", "read", "Get one role by id.", ["role_id"], get_role),
    _op("list_role_scopes", "read", "List the API scopes assigned to a role.", ["role_id"], list_role_scopes),
    _op("list_role_applications", "read", "List the applications holding a role.", ["role_id"],
        list_role_applications),
    _op("create_role", "write", "Create a role and optionally assign API scopes to it.",
        ["name", "type", "description", "scope_ids"], create_role, non_idempotent=True),
    _op("update_role", "write", "Update a role's name or description.",
        ["role_id", "name", "description"], update_role),
    _op("assign_role_scopes", "write",
        "Add API scopes to a role. Additive: Logto cannot remove a scope from a role via this API.",
        ["role_id", "scope_ids"], assign_role_scopes, non_idempotent=True),
    # Users
    _op("list_users", "read", "List users, optionally filtered by search text or suspension.",
        ["page", "page_size", "search", "is_suspended"], list_users),
    _op("get_user", "read", "Get one user by id.", ["user_id"], get_user),
    _op("list_user_roles", "read", "List a user's roles.", ["user_id"], list_user_roles),
    _op("create_user", "write",
        "Create a user. Requires at least one identifier (username, primary_email, primary_phone).",
        ["username", "primary_email", "primary_phone", "password", "name"], create_user, non_idempotent=True),
    _op("update_user", "write",
        "Update a user's profile fields or customData.",
        ["user_id", "name", "avatar", "username", "primary_email", "primary_phone", "custom_data"], update_user),
    _op("set_user_suspended", "write", "Suspend or unsuspend a user. This does not delete anything.",
        ["user_id", "suspended"], set_user_suspended),
    _op("update_user_password", "write", "Set a user's password.",
        ["user_id", "password"], update_user_password),
    _op("assign_user_roles", "write", "Assign roles to a user (additive).",
        ["user_id", "role_ids"], assign_user_roles, non_idempotent=True),
    # Connectors and sign-in experience
    _op("list_connectors", "read", "List configured connectors (email/SMS/social/SSO).", [], list_connectors),
    _op("get_connector", "read", "Get one connector by id. Its config may contain provider secrets.",
        ["connector_id"], get_connector),
    _op("update_connector", "write", "Update a connector's config or metadata.",
        ["connector_id", "config", "metadata", "syncProfile"], update_connector),
    _op("refresh_connector_token", "write",
        "Re-fetch a connector's upstream token (Logto PUT on the connector).",
        ["connector_id"], refresh_connector_token, non_idempotent=True),
    _op("get_sign_in_experience", "read", "Read the tenant's sign-in experience settings.", [],
        get_sign_in_experience),
    _op("update_sign_in_experience", "write",
        "Update named sign-in-experience fields. Reads the current object first and preserves "
        "every field not named (Logto replaces the whole object on PUT).",
        sorted(SIGN_IN_EXPERIENCE_FIELDS), update_sign_in_experience),
    # Organizations
    _op("list_organizations", "read", "List organizations.",
        ["page", "page_size", "search"], list_organizations),
    _op("get_organization", "read", "Get one organization by id.", ["organization_id"], get_organization),
    _op("list_organization_roles", "read", "List organization roles.", ["page", "page_size"],
        list_organization_roles),
    _op("create_organization", "write", "Create an organization.",
        ["name", "description", "custom_data"], create_organization, non_idempotent=True),
    _op("update_organization", "write", "Update an organization's name, description or customData.",
        ["organization_id", "name", "description", "custom_data"], update_organization),
    _op("add_organization_members", "write", "Add users to an organization (idempotent for existing members).",
        ["organization_id", "user_ids"], add_organization_members),
    # Resources (read-only; creating a resource is an API-design action, not ops)
    _op("list_resources", "read", "List API resources and their indicators.", ["page", "page_size"],
        list_resources),
    _op("get_resource", "read", "Get one API resource by id.", ["resource_id"], get_resource),
    _op("list_resource_scopes", "read", "List the scopes defined on an API resource.",
        ["resource_id", "page_size"], list_resource_scopes),
    # Audit
    _op("list_logs", "read", "List audit logs, optionally filtered by user, application or log type.",
        ["page", "page_size", "user_id", "application_id", "log_type"], list_logs),
    _op("get_log", "read", "Get one audit log entry.", ["log_id"], get_log),
)

_BY_NAME: dict[str, Operation] = {op.name: op for op in REGISTRY}


def build_registry() -> dict[str, Operation]:
    """Return the operation map. Refuses to build an inconsistent registry."""
    if len(_BY_NAME) != len(REGISTRY):
        raise OperationError("duplicate operation name in registry")
    for op in REGISTRY:
        if op.handler is None:
            raise OperationError(f"{op.name} has no handler")
        if op.name.startswith("delete_"):
            raise OperationError(f"{op.name}: delete operations are not part of this server")
    return dict(_BY_NAME)


def plan_fingerprint(operation: str, arguments: Mapping[str, Any]) -> str:
    """Stable short id for an operation+arguments pair, for logs and plans."""
    import json

    payload = {"op": operation, "args": redacted_arguments(arguments)}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return digest[:16]
