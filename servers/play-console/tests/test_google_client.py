"""RED tests: Google API client - real request paths, auth, errors, no writes."""
from __future__ import annotations

import pytest

from play_console_mcp.errors import UpstreamError
from play_console_mcp.google_client import GoogleApiClient

PKG = "com.example.app"
BASE = "/androidpublisher/v3/applications/com.example.app"


def test_list_reviews_hits_real_path_and_returns_payload(mocked_api, client):
    route = mocked_api.get(f"{BASE}/reviews").mock(
        return_value=__import__("httpx").Response(200, json={"reviews": [{"reviewId": "r1"}]})
    )
    out = client.get("/androidpublisher/v3/applications/{packageName}/reviews",
                     params={"packageName": PKG, "maxResults": 50})
    assert out == {"reviews": [{"reviewId": "r1"}]}
    req = route.calls.last.request
    assert req.method == "GET"
    assert req.url.path == f"{BASE}/reviews"
    assert req.url.params["maxResults"] == "50"


def test_client_sends_bearer_token(mocked_api, client):
    route = mocked_api.get(f"{BASE}/reviews").mock(
        return_value=__import__("httpx").Response(200, json={"reviews": []})
    )
    client.get("/androidpublisher/v3/applications/{packageName}/reviews",
               params={"packageName": PKG})
    assert route.calls.last.request.headers["authorization"] == "Bearer fake-access-token"


def test_client_encodes_path_parameters(mocked_api, client):
    route = mocked_api.get(f"{BASE}/subscriptions/foo").mock(
        return_value=__import__("httpx").Response(200, json={"productId": "foo"})
    )
    client.get("/androidpublisher/v3/applications/{packageName}/subscriptions/{productId}",
               params={"packageName": PKG, "productId": "foo"})
    assert route.calls.last.request.url.path == f"{BASE}/subscriptions/foo"


def test_client_rejects_write_verb_before_any_network_call(mocked_api, client):
    with pytest.raises(Exception, match="read-only"):
        client.request("POST", "/androidpublisher/v3/applications/{packageName}/edits",
                       params={"packageName": PKG})
    assert len(mocked_api.calls) == 0


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
def test_all_write_verbs_blocked(mocked_api, client, verb):
    with pytest.raises(Exception, match="read-only"):
        client.request(verb, "/androidpublisher/v3/x", params={})
    assert len(mocked_api.calls) == 0


def test_upstream_403_raises_redacted_error(mocked_api, client):
    mocked_api.get(f"{BASE}/reviews").mock(
        return_value=__import__("httpx").Response(
            403,
            json={"error": {"code": 403, "message": "denied",
                            "status": "PERMISSION_DENIED",
                            "details": [{"token": "ya29.SECRET"}]}},
        )
    )
    with pytest.raises(UpstreamError) as ei:
        client.get("/androidpublisher/v3/applications/{packageName}/reviews",
                   params={"packageName": PKG})
    assert "ya29.SECRET" not in str(ei.value)
    assert ei.value.status == 403


def test_upstream_401_maps_to_auth_error(mocked_api, client):
    mocked_api.get(f"{BASE}/reviews").mock(
        return_value=__import__("httpx").Response(
            401, json={"error": {"code": 401, "message": "Invalid Credentials"}}
        )
    )
    with pytest.raises(UpstreamError) as ei:
        client.get("/androidpublisher/v3/applications/{packageName}/reviews",
                   params={"packageName": PKG})
    assert ei.value.status == 401


def test_client_applies_deadline(mocked_api, client):
    route = mocked_api.get(f"{BASE}/reviews").mock(
        return_value=__import__("httpx").Response(200, json={"reviews": []})
    )
    client.get("/androidpublisher/v3/applications/{packageName}/reviews",
               params={"packageName": PKG}, timeout=3.25)
    assert route.calls.last.request.extensions["timeout"]["read"] == pytest.approx(3.25)


def test_client_retries_transient_5xx_then_succeeds(mocked_api, client):
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        import httpx
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"reviews": []})

    mocked_api.get(f"{BASE}/reviews").mock(side_effect=responder)
    out = client.get("/androidpublisher/v3/applications/{packageName}/reviews",
                     params={"packageName": PKG})
    assert out == {"reviews": []}
    assert calls["n"] == 2


def test_client_does_not_retry_400(mocked_api, client):
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        import httpx
        return httpx.Response(400, json={"error": {"message": "bad"}})

    mocked_api.get(f"{BASE}/reviews").mock(side_effect=responder)
    with pytest.raises(UpstreamError):
        client.get("/androidpublisher/v3/applications/{packageName}/reviews",
                   params={"packageName": PKG})
    assert calls["n"] == 1


def test_client_redacts_upstream_body_in_non_json_response(mocked_api, client):
    mocked_api.get(f"{BASE}/reviews").mock(
        return_value=__import__("httpx").Response(
            502, html="<html>proxy token=ya29.SECRETHTML</html>"
        )
    )
    with pytest.raises(UpstreamError) as ei:
        client.get("/androidpublisher/v3/applications/{packageName}/reviews",
                   params={"packageName": PKG})
    assert "ya29.SECRETHTML" not in str(ei.value)