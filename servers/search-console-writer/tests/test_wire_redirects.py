"""Wire-level regressions against real loopback fake upstreams.

These tests use real ``requests`` sessions and real HTTP servers, so they
exercise the transport behaviour that a fake ``Response`` object cannot: when a
redirect is *followed*, a second server actually receives the re-sent ``PUT`` /
``DELETE``. The assertion that the second server recorded **zero** requests is
therefore evidence about the wire, not about a mock.

Positive/negative pairs are deliberate: a 200 must still work (proving the
client is not simply refusing everything), and a 500 must still map to
``upstream_error`` with ``may_have_applied`` (proving the redirect rule did not
break the ambiguity contract).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_harness import FakeWriteUpstream  # noqa: E402

from search_console_writer.allowlist import PropertyAllowlist  # noqa: E402
from search_console_writer.client import (  # noqa: E402
    BASE_OVERRIDE_ENV,
    GscWriteClient,
)
from search_console_writer.errors import GscError  # noqa: E402

ALLOWED = "sc-domain:example.com"
SITEMAP = "https://www.example.com/sitemap.xml"


@pytest.fixture
def servers(monkeypatch):
    """Two real loopback servers; the client is pointed at the first."""
    with FakeWriteUpstream() as primary, FakeWriteUpstream() as secondary:
        monkeypatch.setenv(BASE_OVERRIDE_ENV, primary.base_url() + "/webmasters/v3")
        yield primary, secondary


def client() -> GscWriteClient:
    # A real requests.Session, not a recorder: redirect handling is the
    # property under test.
    return GscWriteClient(
        allowlist=PropertyAllowlist([ALLOWED]),
        session=requests.Session(),
        timeout=5.0,
    )


# --------------------------------------------------------------------------
# Negative: a redirect must not be followed to the second host
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_not_followed_to_another_host(servers, status):
    primary, secondary = servers
    primary.status = status
    primary.redirect_location = secondary.base_url() + "/webmasters/v3/sites/sc-domain%3Aexample.com"

    with pytest.raises(GscError) as excinfo:
        client().add_site(site_url=ALLOWED)

    # The mutation was sent once, to the configured base, and nowhere else.
    assert len(primary.requests) == 1
    assert secondary.requests == []
    assert excinfo.value.code == "unexpected_redirect"


def test_a_delete_redirect_does_not_re_send_the_delete(servers):
    primary, secondary = servers
    primary.status = 307
    primary.redirect_location = secondary.base_url() + "/webmasters/v3/sites/x"

    with pytest.raises(GscError):
        client().delete_site(site_url=ALLOWED)

    assert len(primary.requests) == 1
    assert secondary.requests == []


def test_a_redirect_on_a_sitemap_submit_is_refused(servers):
    primary, secondary = servers
    primary.status = 308
    primary.redirect_location = secondary.base_url() + "/elsewhere"

    with pytest.raises(GscError):
        client().submit_sitemap(site_url=ALLOWED, sitemap_url=SITEMAP)

    assert len(primary.requests) == 1
    assert secondary.requests == []


# --------------------------------------------------------------------------
# Positive: ordinary paths still work
# --------------------------------------------------------------------------


def test_a_200_still_succeeds_and_sends_exactly_one_request(servers):
    primary, secondary = servers
    primary.status = 200

    out = client().add_site(site_url=ALLOWED)

    assert out["ok"] is True
    assert out["applied"] is True
    assert len(primary.requests) == 1
    assert primary.requests[0].method == "PUT"
    assert secondary.requests == []


def test_a_204_still_succeeds(servers):
    primary, _ = servers
    primary.status = 204

    out = client().delete_site(site_url=ALLOWED)

    assert out["applied"] is True
    assert len(primary.requests) == 1


def test_a_500_still_maps_to_an_ambiguous_upstream_error(servers):
    primary, _ = servers
    primary.status = 500
    primary.error_message = "backend failure"

    with pytest.raises(GscError) as excinfo:
        client().submit_sitemap(site_url=ALLOWED, sitemap_url=SITEMAP)

    assert excinfo.value.code == "upstream_error"
    assert excinfo.value.details["may_have_applied"] == "true"
    assert len(primary.requests) == 1


def test_a_403_over_the_wire_is_unauthorized_with_a_hint(servers):
    primary, _ = servers
    primary.status = 403
    primary.error_message = "denied"

    with pytest.raises(GscError) as excinfo:
        client().add_site(site_url=ALLOWED)

    assert excinfo.value.code == "unauthorized"
    assert "user of the property" in excinfo.value.message


# --------------------------------------------------------------------------
# Bounds observed on the wire
# --------------------------------------------------------------------------


def test_a_hostile_retry_after_does_not_escape_the_bounds(servers):
    primary, _ = servers
    primary.status = 429
    primary.error_message = "quota"
    primary.extra_headers = {"Retry-After": "7" * 50_000}

    with pytest.raises(GscError) as excinfo:
        client().add_site(site_url=ALLOWED)

    assert excinfo.value.code == "rate_limited"
    # A malformed value is dropped rather than reflected.
    assert "retry_after" not in excinfo.value.details


def test_a_well_formed_retry_after_is_preserved_on_the_wire(servers):
    primary, _ = servers
    primary.status = 429
    primary.error_message = "quota"
    primary.extra_headers = {"Retry-After": "60"}

    with pytest.raises(GscError) as excinfo:
        client().add_site(site_url=ALLOWED)

    assert excinfo.value.details["retry_after"] == "60"


def test_a_hostile_error_body_is_bounded_on_the_wire(servers):
    primary, _ = servers
    primary.status = 400
    primary.error_message = "z" * 50_000

    with pytest.raises(GscError) as excinfo:
        client().add_site(site_url=ALLOWED)

    assert len(excinfo.value.message) <= 4096