"""Tests for the operation registry and the write handlers that matter most.

The fake client records requests and can return canned responses. These are
behaviour tests of *our* request construction and refusals, not of Logto.
"""
from __future__ import annotations

import pytest

from logto_ops.gate import READ_OPERATIONS, WRITE_OPERATIONS
from logto_ops.operations import REGISTRY, OperationError, build_registry


class FakeClient:
    """Records (method, path, body) and replays queued responses."""

    def __init__(self, responses: dict[tuple[str, str], object] | None = None):
        self.calls: list[tuple[str, str, object]] = []
        self.responses = responses or {}
        self._settings = type("S", (), {"app_id": "app-self", "base_url": "https://logto.test"})()

    def request(self, method, path, *, params=None, json_body=None, operation, idempotent=True):
        self.calls.append((method, path, json_body))
        key = (method, path)
        if key in self.responses:
            return self.responses[key]
        return {"id": "created-id"}

    def list_page(self, path, *, page=1, page_size=None, params=None, operation):
        self.calls.append(("GET", path, None))
        return self.responses.get(("GET", path), [])


def ops():
    return build_registry()


def test_registry_matches_the_gate_classification():
    names = {op.name for op in REGISTRY}
    assert names == set(READ_OPERATIONS) | set(WRITE_OPERATIONS)
    for op in REGISTRY:
        assert op.effect in ("read", "write")


def test_registry_has_no_delete_operations():
    assert not [op for op in REGISTRY if op.name.startswith("delete_")]


def test_unknown_arguments_are_reported_not_dropped():
    op = ops()["list_applications"]
    assert op.unknown_arguments({"page": 1, "bogus": True}) == ["bogus"]
    # The middleware-only arguments are not user arguments.
    assert op.unknown_arguments({"page": 1, "confirmation_token": "t", "dry_run": True}) == []


def test_create_m2m_application_posts_a_machine_to_machine_app():
    client = FakeClient()
    result = ops()["create_m2m_application"].handler(client, {"name": "vlobel-scheduler"})
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "/applications")
    assert body == {"name": "vlobel-scheduler", "type": "MachineToMachine"}
    assert result.payload["id"] == "created-id"


def test_create_m2m_application_assigns_roles_after_creation():
    client = FakeClient(
        {("POST", "/applications"): {"id": "new-app", "type": "MachineToMachine", "name": "x"}}
    )
    result = ops()["create_m2m_application"].handler(
        client, {"name": "x", "role_ids": ["r1", "r2"]}
    )
    posts = [c for c in client.calls if c[0] == "POST" and c[1].endswith("/roles")]
    assert posts == [
        ("POST", "/applications/new-app/roles", {"roleIds": ["r1"]}),
        ("POST", "/applications/new-app/roles", {"roleIds": ["r2"]}),
    ]
    assert result.payload["role_ids"] == ["r1", "r2"]


def test_granting_the_management_api_role_is_allowed_but_warned():
    # The user asked for full authority, so this must work - but the plan has to
    # say that it creates a tenant administrator.
    client = FakeClient(
        {
            ("POST", "/applications"): {"id": "new-app", "type": "MachineToMachine"},
            ("GET", "/roles/r-mgmt/scopes"): [
                {"id": "management-api-all", "name": "all", "resourceId": "management-api"}
            ],
            ("GET", "/resources/management-api/scopes"): [
                {"id": "management-api-all", "name": "all"}
            ],
        }
    )
    result = ops()["create_m2m_application"].handler(
        client, {"name": "admin-app", "role_ids": ["r-mgmt"]}
    )
    assert any("full-tenant administrator" in w for w in result.payload["warnings"])


def test_a_plain_role_produces_no_admin_warning():
    client = FakeClient(
        {
            ("POST", "/applications"): {"id": "new-app", "type": "MachineToMachine"},
            ("GET", "/roles/r-plain/scopes"): [
                {"id": "s1", "name": "read", "resourceId": "some-resource"}
            ],
        }
    )
    result = ops()["create_m2m_application"].handler(
        client, {"name": "app", "role_ids": ["r-plain"]}
    )
    assert result.payload["warnings"] == []


def test_create_application_secret_returns_the_value_once():
    client = FakeClient(
        {("POST", "/applications/app-1/secrets"): {"name": "ci-secret", "value": "s3cret-value"}}
    )
    result = ops()["create_application_secret"].handler(
        client, {"application_id": "app-1", "name": "ci-secret"}
    )
    assert client.calls[0] == ("POST", "/applications/app-1/secrets", {"name": "ci-secret"})
    assert result.payload["value"] == "s3cret-value"
    assert "cannot be read again" in result.payload["note"]


def test_update_sign_in_experience_preserves_unnamed_fields():
    current = {
        "signIn": {"methods": ["email"]},
        "mfa": {"factors": []},
        "color": {"primaryColor": "#000"},
        "captchaPolicy": {"enabled": True},
    }
    client = FakeClient(
        {
            ("GET", "/sign-in-exp"): current,
            ("PUT", "/sign-in-exp"): {"ok": True},
        }
    )
    ops()["update_sign_in_experience"].handler(client, {"mfa": {"factors": ["Totp"]}})
    put_body = [c for c in client.calls if c[0] == "PUT"][0][2]
    # The whole object is sent, and nothing unnamed was dropped.
    assert put_body["color"] == {"primaryColor": "#000"}
    assert put_body["captchaPolicy"] == {"enabled": True}
    assert put_body["mfa"] == {"factors": ["Totp"]}


def test_update_sign_in_experience_refuses_a_blind_put_when_the_read_fails():
    class Broken(FakeClient):
        def request(self, method, path, **kwargs):
            if method == "GET":
                raise OperationError("nope")
            return super().request(method, path, **kwargs)

    with pytest.raises(OperationError):
        ops()["update_sign_in_experience"].handler(Broken(), {"mfa": {"factors": []}})


def test_update_sign_in_experience_only_accepts_security_relevant_fields():
    # `branding`/`customCss`/`customUiAssets` are read-visible but deliberately
    # not updatable through this server: they are asset uploads, not settings,
    # and a body containing them would need the whole asset pipeline.
    op = ops()["update_sign_in_experience"]
    assert "mfa" in op.accepted
    assert "branding" not in op.accepted
    assert "nonsense" not in op.accepted


def test_create_user_requires_an_identifier():
    with pytest.raises(OperationError):
        ops()["create_user"].handler(FakeClient(), {"name": "No Identifier"})


def test_create_user_sends_primary_email_mapping():
    client = FakeClient({("POST", "/users"): {"id": "u1", "primaryEmail": "a@b.c"}})
    ops()["create_user"].handler(client, {"primary_email": "a@b.c", "password": "pw"})
    assert client.calls[0][2] == {"primaryEmail": "a@b.c", "password": "pw"}


def test_update_user_password_never_echoes_the_password():
    client = FakeClient({("PATCH", "/users/u1/password"): {}})
    result = ops()["update_user_password"].handler(client, {"user_id": "u1", "password": "hunter2"})
    assert "hunter2" not in result.as_text()


def test_set_user_suspended_uses_the_dedicated_endpoint():
    client = FakeClient({("PATCH", "/users/u1/is-suspended"): {"id": "u1", "isSuspended": True}})
    ops()["set_user_suspended"].handler(client, {"user_id": "u1", "suspended": True})
    assert client.calls[0] == ("PATCH", "/users/u1/is-suspended", {"isSuspended": True})


def test_set_user_suspended_requires_an_explicit_boolean():
    with pytest.raises(OperationError):
        ops()["set_user_suspended"].handler(FakeClient(), {"user_id": "u1"})


def test_add_organization_members_uses_the_idempotent_array_form():
    client = FakeClient({("POST", "/organizations/o1/users"): {"ok": True}})
    ops()["add_organization_members"].handler(client, {"organization_id": "o1", "user_ids": ["u1", "u2"]})
    assert client.calls[0] == ("POST", "/organizations/o1/users", {"userIds": ["u1", "u2"]})


def test_page_size_must_be_a_positive_integer():
    with pytest.raises(OperationError):
        ops()["list_applications"].handler(FakeClient(), {"page_size": 0})
    with pytest.raises(OperationError):
        ops()["list_applications"].handler(FakeClient(), {"page_size": "10"})


def test_application_type_filter_is_validated():
    with pytest.raises(OperationError):
        ops()["list_applications"].handler(FakeClient(), {"type": "machineToMachine"})


def test_health_reports_roles_without_the_secret():
    client = FakeClient(
        {
            ("GET", "/applications/app-self"): {"id": "app-self", "type": "MachineToMachine"},
            ("GET", "/applications/app-self/roles"): [
                {"id": "r1", "name": "Logto Management API access"}
            ],
        }
    )
    result = ops()["logto_health"].handler(client, {})
    assert result.payload["role_names"] == ["Logto Management API access"]
    assert "role_names" in result.as_text()


def test_assign_role_scopes_reports_the_additive_limitation():
    client = FakeClient({("POST", "/roles/r1/scopes"): {"ok": True}})
    result = ops()["assign_role_scopes"].handler(client, {"role_id": "r1", "scope_ids": ["s1"]})
    assert result.payload["warnings"]
    assert "cannot be removed" in result.payload["warnings"][0]
