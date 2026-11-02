"""Transport tests: named methods, fixed host, and no unsafe retries."""
from __future__ import annotations

import httpx
import pytest

from play_console_writer.client import API_ROOT, METHOD_SPECS, GoogleWriteClient
from play_console_writer.errors import LocalError, PolicyError, UpstreamError, WriteOutcomeUnknown
from play_console_writer.settings import Settings

from conftest import FakeApiTransport, FakeToken

PKG = "com.example.app"


def make(settings=None, transport=None, **kwargs):
    settings = settings or Settings(packages=frozenset({PKG}))
    return GoogleWriteClient(
        settings=settings,
        credentials=kwargs.pop("credentials", FakeToken()),
        transport=transport or FakeApiTransport(),
        # Use the real Google root: the injected transport intercepts before any
        # socket opens, so no request ever leaves the process. Using the real
        # host also means these tests exercise the production host-pinning path.
        api_root=kwargs.pop("api_root", API_ROOT),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Named-method contract: there is no generic request
# --------------------------------------------------------------------------

def test_unknown_method_id_is_refused_before_network():
    transport = FakeApiTransport()
    client = make(transport=transport)
    with pytest.raises(PolicyError, match="no generic request capability"):
        client.request("orders.refund", params={"packageName": PKG, "orderId": "x"})
    assert transport.requests == []


@pytest.mark.parametrize("method_id", [
    "orders.refund",
    "monetization.subscriptions.patch",
    "inappproducts.delete",
    "users.create",
    "grants.patch",
    "purchases.products.consume",
    "externaltransactions.createexternaltransaction",
])
def test_financial_method_id_cannot_even_be_looked_up(method_id):
    client = make()
    assert method_id not in METHOD_SPECS
    with pytest.raises(PolicyError):
        client.request(method_id, params={"packageName": PKG})


def test_request_signature_takes_no_path_and_no_verb():
    """A caller cannot supply a URL or an HTTP verb: only a method id."""
    import inspect

    params = inspect.signature(GoogleWriteClient.request).parameters
    assert "path" not in params
    assert "verb" not in params
    assert "url" not in params
    assert "method_id" in params


# --------------------------------------------------------------------------
# SSRF / host pinning
# --------------------------------------------------------------------------

def test_default_root_is_the_fixed_google_host():
    client = make(api_root=API_ROOT)
    assert client._api_root == "https://androidpublisher.googleapis.com"


@pytest.mark.parametrize("root", [
    "https://evil.example.com",
    "https://androidpublisher.googleapis.com.evil.test",
    "https://10.0.0.5",
    "https://metadata.google.internal",
])
def test_non_google_host_is_refused(root):
    with pytest.raises(LocalError, match="only the fixed Google host"):
        make(api_root=root)


def test_plain_http_to_google_is_refused():
    with pytest.raises(LocalError, match="https"):
        make(api_root="http://androidpublisher.googleapis.com")


def test_loopback_is_permitted_for_tests_only():
    client = make(api_root="http://127.0.0.1:9")
    assert client._api_root == "http://127.0.0.1:9"


def test_api_root_must_not_carry_a_path():
    with pytest.raises(LocalError, match="must not contain a path"):
        make(api_root="https://androidpublisher.googleapis.com/evil")


# --------------------------------------------------------------------------
# Path rendering
# --------------------------------------------------------------------------

def test_renders_literal_upstream_path():
    transport = FakeApiTransport()
    client = make(transport=transport)
    client.request("edits.listings.update",
                   params={"packageName": PKG, "editId": "e1", "language": "en-US"},
                   body={"title": "x"})
    verb, path, query, body = transport.last_write()
    assert verb == "PUT"
    assert path == "/androidpublisher/v3/applications/com.example.app/edits/e1/listings/en-US"
    assert query == {}
    assert body == {"title": "x"}


@pytest.mark.parametrize("bad", ["../secret", "a/b", "a?b", "a#b", "a%b", "a:b"])
def test_path_parameter_cannot_escape_its_segment(bad):
    transport = FakeApiTransport()
    client = make(transport=transport)
    with pytest.raises(LocalError):
        client.request("edits.listings.get",
                       params={"packageName": PKG, "editId": "e1", "language": bad})
    assert transport.requests == []


def test_missing_path_parameter_is_refused():
    client = make()
    with pytest.raises(LocalError, match="missing path parameter"):
        client.request("edits.listings.get", params={"packageName": PKG, "editId": "e1"})


# --------------------------------------------------------------------------
# Retry discipline
# --------------------------------------------------------------------------

def test_read_is_retried_on_5xx():
    transport = FakeApiTransport()
    transport.overrides["/edits/e1/listings"] = (500, {"error": {"message": "boom"}})
    client = make(transport=transport, sleep=lambda _s: None, clock=_fake_clock())
    with pytest.raises(UpstreamError):
        client.request("edits.listings.list", params={"packageName": PKG, "editId": "e1"})
    assert len(transport.requests) == 3


def test_write_is_never_retried_on_5xx():
    """A staged write that returns 500 must not be re-sent."""
    transport = FakeApiTransport()
    transport.overrides["/listings/en-US"] = (500, {"error": {"message": "boom"}})
    client = make(transport=transport)
    with pytest.raises(UpstreamError):
        client.request("edits.listings.update",
                       params={"packageName": PKG, "editId": "e1", "language": "en-US"},
                       body={"title": "x"})
    assert len(transport.writes()) == 1


def test_write_transport_failure_is_reported_as_outcome_unknown():
    transport = FakeApiTransport()
    transport.raise_on_write = httpx.ConnectTimeout("timed out")
    client = make(transport=transport)
    with pytest.raises(WriteOutcomeUnknown) as info:
        client.request("edits.listings.update",
                       params={"packageName": PKG, "editId": "e1", "language": "en-US"},
                       body={"title": "x"})
    assert info.value.outcome_unknown is True
    assert "OUTCOME UNKNOWN" in str(info.value)
    assert len(transport.writes()) == 1


def test_write_transport_failure_does_not_leak_the_exception_text():
    transport = FakeApiTransport()
    transport.raise_on_write = httpx.ConnectError("Bearer ya29.SECRET leak")
    client = make(transport=transport)
    with pytest.raises(WriteOutcomeUnknown) as info:
        client.request("edits.listings.update",
                       params={"packageName": PKG, "editId": "e1", "language": "en-US"},
                       body={"title": "x"})
    assert "ya29.SECRET" not in str(info.value)


# --------------------------------------------------------------------------
# Media uploads
# --------------------------------------------------------------------------

def test_media_upload_sets_upload_type_and_raw_body():
    transport = FakeApiTransport()
    client = make(transport=transport)
    client.request("edits.bundles.upload",
                   params={"packageName": PKG, "editId": "e1"},
                   media=b"PK\x03\x04payload",
                   media_content_type="application/octet-stream")
    verb, path, query, body = transport.last_write()
    assert verb == "POST"
    assert path.endswith("/edits/e1/bundles")
    assert query == {"uploadType": "media"}
    assert body == b"PK\x03\x04payload"


def test_json_write_never_gets_upload_type():
    transport = FakeApiTransport()
    client = make(transport=transport)
    client.request("edits.insert", params={"packageName": PKG}, body={})
    assert "uploadType" not in transport.last_write()[2]


# --------------------------------------------------------------------------
# Error redaction
# --------------------------------------------------------------------------

def test_upstream_error_body_is_redacted():
    transport = FakeApiTransport()
    transport.overrides["/listings"] = (
        403,
        {"error": {"code": 403, "message": "denied for ya29.SUPERSECRET",
                   "status": "PERMISSION_DENIED"}},
    )
    client = make(transport=transport)
    with pytest.raises(UpstreamError) as info:
        client.request("edits.listings.list", params={"packageName": PKG, "editId": "e1"})
    assert "ya29.SUPERSECRET" not in str(info.value)
    assert "PERMISSION_DENIED" in str(info.value)


def test_last_request_records_method_id_without_body():
    transport = FakeApiTransport()
    client = make(transport=transport)
    client.request("edits.listings.update",
                   params={"packageName": PKG, "editId": "e1", "language": "en-US"},
                   body={"title": "secret-title"})
    recorded = client._last_request
    assert recorded["method_id"] == "edits.listings.update"
    assert recorded["verb"] == "PUT"
    assert "secret-title" not in repr(recorded)


def test_missing_credentials_is_a_local_error():
    client = GoogleWriteClient(settings=Settings(packages=frozenset({PKG})),
                               transport=FakeApiTransport())
    with pytest.raises(LocalError, match="no Google credentials"):
        client.request("edits.insert", params={"packageName": PKG}, body={})


class _fake_clock:
    """Monotonic clock that advances 1s per read, so retry budgets terminate."""

    def __init__(self):
        self._t = 0.0

    def __call__(self) -> float:
        self._t += 1.0
        return self._t