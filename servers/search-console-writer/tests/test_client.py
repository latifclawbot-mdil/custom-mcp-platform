"""Write-operation behaviour: exact request construction, and no retry.

The single most important behavioural property of a write client is that it
does **not** automatically retry an effect that may already have been applied.
These tests use a recording session that can be told to raise at the transport
layer, and assert the session was called exactly once.
"""

from __future__ import annotations

import pytest

from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.client import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    GscWriteClient,
)
from search_console_writer.errors import (
    AmbiguousOutcomeError,
    GscError,
    InvalidArgumentError,
    SiteNotAllowedError,
)

ALLOWED = "sc-domain:example.com"
ALLOWED_PREFIX = "https://www.example.com/"


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class RecordingSession:
    """Records each call. Can raise, to model a transport-layer failure."""

    def __init__(self, responses=None, raise_exc=None):
        self.calls = []
        self._responses = list(responses or [])
        self._raise = raise_exc

    def request(self, method, url, json=None, timeout=None, **kw):
        self.calls.append({"method": method, "url": url, "json": json, "timeout": timeout})
        if self._raise is not None:
            raise self._raise
        return self._responses.pop(0) if self._responses else FakeResponse({})


def client(session, allowlist=None):
    return GscWriteClient(
        allowlist=allowlist or PropertyAllowlist([ALLOWED, ALLOWED_PREFIX]),
        session=session,
    )


class TestAddSite:
    def test_uses_put_on_the_sites_endpoint(self):
        session = RecordingSession()
        out = client(session).add_site(site_url=ALLOWED)
        call = session.calls[0]
        assert call["method"] == "PUT"
        assert call["url"] == f"{BASE_URL}/sites/sc-domain%3Aexample.com"
        assert call["json"] is None
        assert out["ok"] is True
        assert out["operation"] == "sites.add"
        assert out["applied"] is True

    def test_property_is_percent_encoded_as_one_segment(self):
        session = RecordingSession()
        client(session).add_site(site_url=ALLOWED_PREFIX)
        url = session.calls[0]["url"]
        assert "/sites/https%3A%2F%2Fwww.example.com%2F" in url
        segment = url.split("/sites/", 1)[1]
        assert "/" not in segment
        assert ":" not in segment

    def test_non_allowlisted_property_is_refused_before_any_request(self):
        session = RecordingSession()
        with pytest.raises(SiteNotAllowedError):
            client(session).add_site(site_url="sc-domain:not-mine.example")
        assert session.calls == []

    def test_the_normalised_property_is_what_is_sent(self):
        session = RecordingSession()
        client(session).add_site(site_url="SC-DOMAIN:example.com")
        assert session.calls[0]["url"].endswith("/sites/sc-domain%3Aexample.com")


class TestDeleteSite:
    def test_uses_delete_on_the_sites_endpoint(self):
        session = RecordingSession()
        out = client(session).delete_site(site_url=ALLOWED)
        call = session.calls[0]
        assert call["method"] == "DELETE"
        assert call["url"] == f"{BASE_URL}/sites/sc-domain%3Aexample.com"
        assert call["json"] is None
        assert out["operation"] == "sites.delete"
        assert out["applied"] is True

    def test_refuses_a_non_allowlisted_property(self):
        session = RecordingSession()
        with pytest.raises(SiteNotAllowedError):
            client(session).delete_site(site_url="sc-domain:not-mine.example")
        assert session.calls == []


class TestSubmitSitemap:
    def test_uses_put_on_the_feedpath_endpoint(self):
        session = RecordingSession()
        sitemap = "https://www.example.com/sitemap.xml"
        out = client(session).submit_sitemap(site_url=ALLOWED, sitemap_url=sitemap)
        call = session.calls[0]
        assert call["method"] == "PUT"
        assert call["url"] == (
            f"{BASE_URL}/sites/sc-domain%3Aexample.com/sitemaps/"
            "https%3A%2F%2Fwww.example.com%2Fsitemap.xml"
        )
        assert call["json"] is None
        assert out["operation"] == "sitemaps.submit"
        assert out["applied"] is True

    def test_sitemap_outside_the_property_is_refused_before_any_request(self):
        session = RecordingSession()
        with pytest.raises(InvalidArgumentError):
            client(session).submit_sitemap(
                site_url=ALLOWED, sitemap_url="https://evil.example/sitemap.xml"
            )
        assert session.calls == []

    def test_non_allowlisted_property_is_refused_first(self):
        session = RecordingSession()
        with pytest.raises(SiteNotAllowedError):
            client(session).submit_sitemap(
                site_url="sc-domain:not-mine.example",
                sitemap_url="https://not-mine.example/sitemap.xml",
            )
        assert session.calls == []


class TestDeleteSitemap:
    def test_uses_delete_on_the_feedpath_endpoint(self):
        session = RecordingSession()
        sitemap = "https://example.com/sitemap_index.xml"
        out = client(session).delete_sitemap(site_url=ALLOWED, sitemap_url=sitemap)
        call = session.calls[0]
        assert call["method"] == "DELETE"
        assert call["url"] == (
            f"{BASE_URL}/sites/sc-domain%3Aexample.com/sitemaps/"
            "https%3A%2F%2Fexample.com%2Fsitemap_index.xml"
        )
        assert out["operation"] == "sitemaps.delete"
        assert out["applied"] is True


class TestNoAutomaticRetry:
    """One call, one request. A failed or ambiguous write is never re-sent."""

    def test_transport_failure_is_sent_exactly_once(self):
        session = RecordingSession(raise_exc=ConnectionError("reset by peer"))
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert len(session.calls) == 1
        assert excinfo.value.code in ("network_error", "ambiguous_outcome")

    def test_server_error_is_not_retried(self):
        session = RecordingSession(responses=[FakeResponse(status=500, payload={"error": {}})])
        with pytest.raises(GscError):
            client(session).delete_site(site_url=ALLOWED)
        assert len(session.calls) == 1

    def test_rate_limited_write_is_not_retried(self):
        session = RecordingSession(
            responses=[FakeResponse(status=429, payload={"error": {"message": "quota"}})]
        )
        with pytest.raises(GscError) as excinfo:
            client(session).submit_sitemap(
                site_url=ALLOWED, sitemap_url="https://example.com/sitemap.xml"
            )
        assert len(session.calls) == 1
        assert excinfo.value.code == "rate_limited"


class TestTransportAmbiguity:
    """A transport failure on a mutating verb is an *ambiguous* outcome.

    For a non-idempotent semantics question the client cannot know whether the
    write landed, so it must say so rather than report a plain network error a
    caller might retry.
    """

    def test_transport_failure_is_reported_as_ambiguous(self):
        session = RecordingSession(raise_exc=TimeoutError("read timeout"))
        with pytest.raises(AmbiguousOutcomeError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert excinfo.value.code == "ambiguous_outcome"
        assert len(session.calls) == 1


class TestTimeout:
    def test_every_request_carries_the_bounded_timeout(self):
        session = RecordingSession()
        client(session).add_site(site_url=ALLOWED)
        assert session.calls[0]["timeout"] == REQUEST_TIMEOUT_SECONDS
        assert REQUEST_TIMEOUT_SECONDS == 30.0

    def test_timeout_is_configurable_but_single_valued(self):
        session = RecordingSession()
        GscWriteClient(
            allowlist=PropertyAllowlist([ALLOWED]), session=session, timeout=7.5
        ).add_site(site_url=ALLOWED)
        assert session.calls[0]["timeout"] == 7.5


class TestVerbGuard:
    def test_only_put_and_delete_are_constructible(self):
        session = RecordingSession()
        with pytest.raises(InvalidArgumentError):
            client(session)._request("POST", f"{BASE_URL}/sites/x")
        assert session.calls == []


class TestErrorMapping:
    def test_403_is_mapped_to_unauthorized_with_a_hint(self):
        session = RecordingSession(
            responses=[FakeResponse(status=403, payload={"error": {"message": "denied"}})]
        )
        with pytest.raises(GscError) as excinfo:
            client(session).add_site(site_url=ALLOWED)
        assert excinfo.value.code == "unauthorized"
        assert "user of the property" in excinfo.value.message

    def test_404_is_mapped(self):
        session = RecordingSession(responses=[FakeResponse(status=404, payload={})])
        with pytest.raises(GscError) as excinfo:
            client(session).delete_site(site_url=ALLOWED)
        assert excinfo.value.code == "not_found"

    def test_empty_success_body_is_accepted(self):
        # sites.add and sitemaps.submit return an empty body on success.
        session = RecordingSession(responses=[FakeResponse(status=200, payload=None)])
        out = client(session).add_site(site_url=ALLOWED)
        assert out["applied"] is True

    def test_no_content_success_is_accepted(self):
        session = RecordingSession(responses=[FakeResponse(status=204, payload=None)])
        out = client(session).delete_site(site_url=ALLOWED)
        assert out["applied"] is True


class TestHostOverrideSeam:
    def test_override_is_ignored_off_loopback(self, monkeypatch):
        from search_console_writer import client as client_mod

        monkeypatch.setenv(client_mod.BASE_OVERRIDE_ENV, "https://evil.example/webmasters/v3")
        assert client_mod.resolve_base() == BASE_URL

    def test_override_is_honoured_on_loopback(self, monkeypatch):
        from search_console_writer import client as client_mod

        monkeypatch.setenv(client_mod.BASE_OVERRIDE_ENV, "http://127.0.0.1:9/webmasters/v3")
        assert client_mod.resolve_base() == "http://127.0.0.1:9/webmasters/v3"