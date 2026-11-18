"""Client tests: bounds, redirect refusal, token caching, ambiguity."""
from __future__ import annotations

import httpx
import pytest

from logto_ops.client import (
    AmbiguousResult,
    LogtoClient,
    LogtoHTTPError,
    LogtoTransportError,
)
from logto_ops.settings import load_settings


def make_settings(tmp_path, **overrides):
    secret = tmp_path / "m2m"
    secret.write_text("client-secret-value", encoding="utf-8")
    secret.chmod(0o600)
    env = {
        "LOGTO_BASE_URL": "https://logto.test",
        "LOGTO_M2M_APP_ID": "app-id",
        "LOGTO_M2M_SECRET_FILE": str(secret),
    }
    env.update(overrides)
    return load_settings(env)


def token_response(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600, "scope": "all"})

    return handler


def test_token_is_fetched_once_and_reused(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(200, json=[{"id": "a"}])

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    client.list_page("/applications", operation="list_applications")
    client.list_page("/applications", page=2, operation="list_applications")
    token_calls = [r for r in seen if r.url.path == "/oidc/token"]
    assert len(token_calls) == 1
    assert all(r.headers["authorization"] == "Bearer tok-1" for r in seen if r.url.path != "/oidc/token")


def test_token_request_uses_basic_auth_and_the_management_indicator(tmp_path):
    seen: list[httpx.Request] = []
    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(token_response(seen)))
    client.access_token()
    body = seen[0].content.decode()
    assert "grant_type=client_credentials" in body
    assert "resource=https%3A%2F%2Fdefault.logto.app%2Fapi" in body
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_cloudflare_access_headers_are_sent_when_configured(tmp_path):
    seen: list[httpx.Request] = []
    cf = tmp_path / "cf"
    cf.write_text("cf-secret", encoding="utf-8")
    cf.chmod(0o600)
    settings = make_settings(
        tmp_path,
        LOGTO_CF_ACCESS_CLIENT_ID="abc.access",
        LOGTO_CF_ACCESS_SECRET_FILE=str(cf),
    )
    client = LogtoClient(settings, transport=httpx.MockTransport(token_response(seen)))
    client.access_token()
    assert seen[0].headers["cf-access-client-id"] == "abc.access"
    assert seen[0].headers["cf-access-client-secret"] == "cf-secret"


def test_redirects_are_not_followed(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(LogtoHTTPError) as excinfo:
        client.request("GET", "/applications", operation="list_applications")
    assert excinfo.value.status == 302
    assert not any("evil.example" in str(r.url) for r in seen)


def test_oversized_response_is_refused(tmp_path):
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(200, json=[{"id": "x", "padding": "y" * 5000}])

    client = LogtoClient(settings, transport=httpx.MockTransport(handler))
    # 5000 bytes is under the 4 MiB default, so shrink the limit for the test.
    object.__setattr__(client._settings, "max_response_bytes", 1000)
    with pytest.raises(LogtoTransportError):
        client.list_page("/applications", operation="list_applications")


def test_provider_error_is_classified_without_echoing_the_body(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(
            422,
            json={"code": "entity.require_identifiers", "message": "user must have an identity"},
        )

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(LogtoHTTPError) as excinfo:
        client.request("POST", "/users", json_body={}, operation="create_user", idempotent=False)
    assert excinfo.value.status == 422
    assert excinfo.value.provider_code == "entity.require_identifiers"
    assert "tok-1" not in str(excinfo.value)


def test_401_invalidates_the_token_and_retries_once(tmp_path):
    state = {"tokens": 0, "uses": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/token":
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{state['tokens']}", "expires_in": 3600})
        state["uses"] += 1
        if state["uses"] == 1:
            return httpx.Response(401, json={"code": "auth.unauthorized"})
        return httpx.Response(200, json=[{"id": "a"}])

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    assert client.list_page("/applications", operation="list_applications") == [{"id": "a"}]
    assert state["tokens"] == 2


def test_repeated_401_does_not_loop(tmp_path):
    state = {"on_401": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        state["on_401"] += 1
        return httpx.Response(401, json={"code": "auth.unauthorized"})

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(LogtoHTTPError):
        client.list_page("/applications", operation="list_applications")
    assert state["on_401"] == 2


def test_timeout_on_a_create_is_reported_as_ambiguous(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        raise httpx.ReadTimeout("boom")

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(AmbiguousResult):
        client.request(
            "POST", "/applications", json_body={"name": "x", "type": "MachineToMachine"},
            operation="create_m2m_application", idempotent=False,
        )


def test_timeout_on_a_read_is_a_plain_transport_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        raise httpx.ReadTimeout("boom")

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(LogtoTransportError):
        client.list_page("/applications", operation="list_applications")


def test_page_size_is_capped_by_settings(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json=[])

    settings = make_settings(tmp_path, LOGTO_MAX_PAGE_SIZE="25")
    client = LogtoClient(settings, transport=httpx.MockTransport(handler))
    client.list_page("/applications", page_size=5000, operation="list_applications")
    data_calls = [r for r in seen if r.url.path.endswith("/applications")]
    assert data_calls[0].url.params["page_size"] == "25"


def test_token_endpoint_failure_reports_http_status_not_the_secret(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "bad creds"})

    client = LogtoClient(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(LogtoHTTPError) as excinfo:
        client.access_token()
    assert excinfo.value.status == 401
    assert excinfo.value.provider_code == "invalid_client"
    assert "client-secret-value" not in str(excinfo.value)
