"""Regression tests for independently reported findings F1-F6, plus the
financial-exclusion audit those findings imply.

Every test drives a realistic path: the client's real error adapter over a real
``httpx`` transport, the real MCP dispatcher, or the registry interlock. None of
them asserts an internal helper's shape in isolation.

F1  credential material leaked through error previews (PEM bodies, ``token=``
    pairs, and secret response headers used as the error reason).
F2  a successful mutation whose read-back failed was reported as a tool error,
    inviting a duplicate retry.
F3  re-opening an edit id overwrote its registry record, wiping stage history
    and resetting the committed guard.
F4  a null-valued unknown argument was dropped by the dispatcher *before* the
    schema re-check, bypassing the secondary guard.
F5  the client seam forwarded arbitrary query parameters to Google.
F6  upload validation accepted non-archives/non-images while claiming otherwise.

The final section audits the capability boundary: no tool, and no operation
reachable through a tool, can perform or stage a financial action. ``edits.commit``
is disabled because the API cannot enumerate what a shared edit contains, so the
absence of financial side effects cannot be established.
"""
from __future__ import annotations

import json as _json_for_secret_regression
import pytest as _pytest_for_secret_regression


@_pytest_for_secret_regression.mark.parametrize("key", ["client_secret", "private_key_id", "api_key", "password"])
def test_json_secret_fields_are_fully_redacted(key):
    from play_console_writer.errors import redact_upstream_error
    value = "fixture-sensitive-value-never-visible-3a54"
    raw = _json_for_secret_regression.dumps({"error": {"message": "rejected", "details": [{key: value}]}})
    result = str(redact_upstream_error(status=400, body=raw))
    assert value not in result


import base64
import io
import json
import zipfile

import httpx
import pytest

from play_console_writer import policy
from play_console_writer.client import METHOD_SPECS, GoogleWriteClient
from play_console_writer.edit_registry import MAX_TRACKED_EDITS, EditRegistry
from play_console_writer.errors import LocalError, PolicyError, UpstreamError
from play_console_writer.operations import TOOL_SPECS, WriteOperations
from play_console_writer.settings import Settings
from play_console_writer.validation import ValidationError

from conftest import FakeApiTransport, FakeToken

PKG = "com.example.app"
BASE = "/androidpublisher/v3/applications/com.example.app"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _client(handler, settings: Settings | None = None) -> GoogleWriteClient:
    return GoogleWriteClient(
        settings=settings or Settings(packages=frozenset({PKG})),
        credentials=FakeToken(),
        transport=httpx.MockTransport(handler),
    )


def _ops(settings: Settings, transport: FakeApiTransport):
    client = GoogleWriteClient(settings=settings, credentials=FakeToken(),
                               transport=transport)
    return WriteOperations(client=client, settings=settings,
                           registry=EditRegistry()), transport


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# F1: credential material must not survive an error preview
# ---------------------------------------------------------------------------

PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BASESECRETKEYBODY=\n"
    "-----END PRIVATE KEY-----"
)


def test_private_key_block_body_is_redacted_from_a_write_error():
    """A service-account key echoed in an error body must not reach the caller."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": 401, "status": "UNAUTHENTICATED",
                            "message": "denied", "details": PEM}},
        )

    client = _client(handler)
    with pytest.raises(UpstreamError) as info:
        client.request("reviews.reply", params={"packageName": PKG, "reviewId": "r1"},
                       body={"replyText": "hi"})

    message = str(info.value)
    assert "BASESECRETKEYBODY" not in message
    assert "MIIEvQ" not in message
    # The status/reason are still useful, so this is redaction, not blanking.
    assert "401" in message
    assert "denied" in message


def test_private_key_with_escaped_newlines_is_redacted_from_a_read_error():
    """The upstream JSON encodes newlines as ``\\n``; that form must be caught too."""
    escaped = json.dumps({"error": {"code": 403, "message": "forbidden",
                                    "debug": PEM}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=escaped)

    client = _client(handler)
    with pytest.raises(UpstreamError) as info:
        client.request("edits.get", params={"packageName": PKG, "editId": "e1"})

    assert "BASESECRETKEYBODY" not in str(info.value)


def test_plaintext_token_assignment_is_redacted():
    """``token=...`` in a non-JSON proxy/stack-trace body is still a credential."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, text="internal error trace: token=SECRETPLAINTEXTKEY12345 "
                      "host=internal.corp")

    client = _client(handler)
    with pytest.raises(UpstreamError) as info:
        client.request("edits.get", params={"packageName": PKG, "editId": "e1"})

    message = str(info.value)
    assert "SECRETPLAINTEXTKEY12345" not in message
    assert "HTTP 500" in message


@pytest.mark.parametrize("header,secret", [
    ("set-cookie", "SIDCC=PRODSECRETCOOKIE987654; Secure; HttpOnly"),
    ("cookie", "SESS=PRODSECRETCOOKIE987654"),
    ("www-authenticate", 'Bearer error_description="SECRET_AUTH_DESC expired"'),
])
def test_secret_response_header_values_are_never_used_as_the_reason(header, secret):
    """The header value is credential material, not a diagnostic string."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={header: secret}, text="")

    client = _client(handler)
    with pytest.raises(UpstreamError) as info:
        client.request("edits.get", params={"packageName": PKG, "editId": "e1"})

    message = str(info.value)
    assert "PRODSECRETCOOKIE987654" not in message
    assert "SECRET_AUTH_DESC" not in message
    assert "403" in message


def test_normal_google_error_keeps_its_diagnostic_message():
    """Positive control: redaction must not destroy an ordinary API error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {
            "code": 403, "message": "The caller does not have permission",
            "status": "PERMISSION_DENIED"}})

    client = _client(handler)
    with pytest.raises(UpstreamError) as info:
        client.request("edits.get", params={"packageName": PKG, "editId": "e1"})

    message = str(info.value)
    assert "PERMISSION_DENIED" in message
    assert "does not have permission" in message


# ---------------------------------------------------------------------------
# F2: a successful mutation with a failed read-back is "applied, unverified"
# ---------------------------------------------------------------------------

def test_listing_update_read_back_failure_is_unverified_not_a_tool_error(settings, transport):
    operations, transport = _ops(settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()
    transport.read_overrides = {
        "/listings/en-US": (404, {"error": {"code": 404, "message": "not found"}}),
    }

    result = operations.listing_update(PKG, "edit-1", "en-US", title="New title")

    assert result["outcome"] == "applied_unverified"
    assert result["verification"]["verified"] is False
    assert result["verification"]["error"]
    assert "do not" in result["verification"]["guidance"].lower()
    assert result["read_back"]["listing"] is None
    # Exactly one PUT: the write was sent once and never re-sent by the server.
    writes = transport.writes()
    assert [(verb, path) for verb, path, _q, _b in writes] == [
        ("PUT", f"{BASE}/edits/edit-1/listings/en-US")]


def test_details_update_read_back_failure_is_unverified(unlocked_settings, transport):
    operations, transport = _ops(unlocked_settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()
    transport.read_overrides = {
        "/details": (403, {"error": {"code": 403, "message": "denied"}}),
    }

    result = operations.details_update(PKG, "edit-1", contact_email="dev@example.com")

    assert result["outcome"] == "applied_unverified"
    assert result["verification"]["verified"] is False
    assert len(transport.writes()) == 1


def test_testers_update_read_back_failure_is_unverified(settings, transport):
    operations, transport = _ops(settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()
    transport.read_overrides = {
        "/testers/beta": (500, {"error": {"code": 500, "message": "boom"}}),
    }

    result = operations.testers_update(PKG, "edit-1", "beta", ["qa@example.com"])

    assert result["outcome"] == "applied_unverified"
    assert result["verification"]["verified"] is False
    assert len(transport.writes()) == 1


def test_device_tier_read_back_failure_is_unverified(settings, transport):
    operations, transport = _ops(settings, transport)
    transport.read_overrides = {
        "/deviceTierConfigs/7": (404, {"error": {"code": 404, "message": "gone"}}),
    }

    result = operations.device_tier_config_create(PKG, {"deviceGroups": [{"name": "g"}]})

    assert result["outcome"] == "applied_unverified"
    assert result["verification"]["verified"] is False
    assert len(transport.writes()) == 1


def test_successful_read_back_is_marked_verified_and_compared(settings, transport):
    operations, transport = _ops(settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()

    result = operations.listing_update(PKG, "edit-1", "en-US", title="Example")

    assert result["outcome"] == "applied"
    assert result["verification"]["verified"] is True
    assert result["verification"]["matches_intent"] is True
    assert result["read_back"]["listing"]["title"] == "Example"


def test_read_back_value_mismatch_is_reported_as_unverified(settings, transport):
    """A 200 read-back that does not show the intended value is not 'verified'."""
    operations, transport = _ops(settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()
    # Default fake listing title is "Example"; we wrote "Different".
    result = operations.listing_update(PKG, "edit-1", "en-US", title="Different")

    assert result["outcome"] == "applied_unverified"
    assert result["verification"]["verified"] is False
    assert result["verification"]["reason"] == "read_back_differs"
    assert result["verification"]["mismatches"]["title"] == {
        "intended": "Different", "observed": "Example"}


# ---------------------------------------------------------------------------
# F3: registry id collisions must fail closed, never rewrite history
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self) -> None:
        self._t = 0.0

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def __call__(self) -> float:
        return self._t


def test_reopening_a_committed_edit_id_is_refused_and_keeps_history():
    registry = EditRegistry()
    registry.open_edit(PKG, "edit-1")
    registry.record_staged(PKG, "edit-1", "edits.listings.update")
    registry.mark_committed(PKG, "edit-1")

    with pytest.raises(LocalError, match="already tracked"):
        registry.open_edit(PKG, "edit-1")

    # The committed guard and the stage history both survive the attempt.
    assert registry.staged_methods(PKG, "edit-1") == ["edits.listings.update"]
    with pytest.raises(LocalError, match="already committed"):
        registry.require_owned(PKG, "edit-1")


def test_reopening_a_live_edit_id_is_refused(settings, transport):
    operations, transport = _ops(settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()

    with pytest.raises(LocalError, match="already tracked"):
        operations.edit_insert(PKG)
    # No second edit insert was sent: the collision was refused locally.
    assert transport.writes() == []


def test_registry_refuses_to_evict_a_live_edit_at_capacity():
    registry = EditRegistry(clock=_Clock())
    for index in range(MAX_TRACKED_EDITS):
        registry.open_edit(PKG, f"e{index}")

    with pytest.raises(LocalError, match="maximum"):
        registry.open_edit(PKG, "overflow")

    # The oldest edit is still owned: ownership was not silently dropped.
    registry.require_owned(PKG, "e0")


def test_registry_reclaims_expired_slots_instead_of_refusing_forever():
    clock = _Clock()
    registry = EditRegistry(ttl_seconds=60, clock=clock)
    for index in range(MAX_TRACKED_EDITS):
        registry.open_edit(PKG, f"e{index}")
    clock.advance(61)

    # Expired entries are dead (Google discarded them); their slots are reusable.
    registry.open_edit(PKG, "fresh")


# ---------------------------------------------------------------------------
# F4: the dispatcher must refuse unknown arguments, including null ones
# ---------------------------------------------------------------------------

def test_dispatcher_refuses_an_unknown_null_argument(settings, transport):
    from play_console_writer.server import _make_dispatcher

    operations, transport = _ops(settings, transport)
    client = operations._client
    dispatch = _make_dispatcher("play_edit_insert", operations, client,
                                TOOL_SPECS["play_edit_insert"])

    with pytest.raises(Exception) as info:
        dispatch({"package_name": PKG, "price": None})

    assert "not a recognised argument" in str(info.value)
    assert transport.requests == []


def test_dispatcher_still_accepts_a_declared_optional_null(settings, transport):
    """An explicit null for a declared optional field means 'leave it alone'."""
    from play_console_writer.server import _make_dispatcher

    operations, transport = _ops(settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()
    client = operations._client
    dispatch = _make_dispatcher("play_listing_update", operations, client,
                                TOOL_SPECS["play_listing_update"])

    payload = json.loads(dispatch({
        "package_name": PKG, "edit_id": "edit-1", "language": "en-US",
        "title": None, "full_description": "Body",
    }))

    assert payload["operation"] == "play_listing_update"
    assert transport.writes()[0][3] == {"fullDescription": "Body"}


# ---------------------------------------------------------------------------
# F5: no arbitrary query parameters at the client seam
# ---------------------------------------------------------------------------

def test_client_refuses_an_undeclared_query_parameter(settings, transport):
    client = GoogleWriteClient(settings=settings, credentials=FakeToken(),
                               transport=transport)
    with pytest.raises(PolicyError, match="does not accept query parameter"):
        client.request("edits.get", params={
            "packageName": PKG, "editId": "e1",
            "changesNotSentForReview": "true",
        })
    assert transport.requests == []


def test_only_the_declared_query_parameters_exist_in_the_registry():
    declared = {mid: spec.query_params for mid, spec in METHOD_SPECS.items()}
    non_empty = {mid: params for mid, params in declared.items() if params}
    assert non_empty == {
        "applications.deviceTierConfigs.create": frozenset({"allowUnknownDevices"})
    }


def test_declared_query_parameter_still_reaches_the_wire(settings, transport):
    operations, transport = _ops(settings, transport)
    operations.device_tier_config_create(
        PKG, {"deviceGroups": [{"name": "g"}]}, allow_unknown_devices=True)
    query = transport.writes()[0][2]
    assert query == {"allowUnknownDevices": "true"}


# ---------------------------------------------------------------------------
# F6: upload validation must reject what it says it rejects
# ---------------------------------------------------------------------------

def test_apk_upload_rejects_a_payload_that_is_not_a_zip(unlocked_settings, transport):
    operations, transport = _ops(unlocked_settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()

    with pytest.raises(ValidationError, match="zip"):
        operations.apk_upload(PKG, "edit-1", _b64(b"PKjunknotazip"))

    assert transport.writes() == []


def test_bundle_upload_rejects_a_zip_without_an_aab_manifest(unlocked_settings, transport):
    operations, transport = _ops(unlocked_settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()
    payload = _zip_bytes({"base/dex/classes.dex": b"dex"})

    with pytest.raises(ValidationError, match="base/manifest/AndroidManifest.xml"):
        operations.bundle_upload(PKG, "edit-1", _b64(payload))

    assert transport.writes() == []


def test_apk_and_bundle_accept_real_archives(unlocked_settings, transport):
    operations, transport = _ops(unlocked_settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()

    apk = _zip_bytes({"AndroidManifest.xml": b"<manifest/>",
                      "classes.dex": b"dex"})
    aab = _zip_bytes({"base/manifest/AndroidManifest.xml": b"<manifest/>",
                      "base/dex/classes.dex": b"dex"})

    operations.apk_upload(PKG, "edit-1", _b64(apk))
    operations.bundle_upload(PKG, "edit-1", _b64(aab))

    media_writes = [(verb, path) for verb, path, _q, _b in transport.writes()]
    assert media_writes == [("POST", f"{BASE}/edits/edit-1/apks"),
                            ("POST", f"{BASE}/edits/edit-1/bundles")]


def test_image_upload_rejects_non_image_bytes(unlocked_settings, transport):
    operations, transport = _ops(unlocked_settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()

    with pytest.raises(ValidationError, match="PNG or JPEG"):
        operations.image_upload(PKG, "edit-1", "en-US", "icon", _b64(b"not-an-image"))

    assert transport.writes() == []


def test_image_upload_accepts_png_bytes(unlocked_settings, transport):
    operations, transport = _ops(unlocked_settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    operations.image_upload(PKG, "edit-1", "en-US", "icon", _b64(png))

    assert transport.writes()[0][1] == f"{BASE}/edits/edit-1/listings/en-US/icon"


# ---------------------------------------------------------------------------
# Financial-exclusion audit (the reason F5/F4/F2 fixes are not merely hygiene)
# ---------------------------------------------------------------------------

_FINANCIAL_PREFIXES = (
    "inappproducts.", "monetization.", "orders.", "purchases.",
    "users.", "grants.", "externaltransactions.",
)


def test_no_tool_maps_to_a_financial_method():
    for tool_name, spec in TOOL_SPECS.items():
        method_id = spec["method_id"]
        assert method_id not in policy.FINANCIAL_METHODS, tool_name
        assert not method_id.startswith(_FINANCIAL_PREFIXES), (tool_name, method_id)


def test_no_tool_id_suggests_a_financial_operation():
    financial_words = ("refund", "price", "purchase", "billing", "monetiz",
                       "grant", "order", "tax", "currency", "payment", "sku")
    for tool_name in TOOL_SPECS:
        assert not any(word in tool_name for word in financial_words), tool_name


def test_commit_is_disabled_because_shared_staged_state_cannot_be_proven_clean():
    """The API cannot enumerate an edit's contents, so commit cannot be offered.

    A commit applies everything staged in the edit, including anything another
    actor staged with the same id. Without a "what is staged" endpoint this
    server cannot establish the absence of a financial change, so the tool is
    absent and the method is unreachable even by method id.
    """
    assert "play_edit_commit" not in TOOL_SPECS
    assert "edits.commit" not in METHOD_SPECS
    assert "edits.commit" not in policy.ALLOWED_WRITE_METHODS
    assert policy.is_disabled("edits.commit")

    with pytest.raises(PolicyError) as info:
        _client(lambda request: httpx.Response(200, json={})).request(
            "edits.commit", params={"packageName": PKG, "editId": "e1"})
    assert "disabled" in str(info.value).lower()
    assert "financial" in str(info.value).lower()


def test_capability_report_names_commit_as_disabled_with_a_reason():
    from play_console_writer.capability import capability_report

    report = capability_report(release_gate_enabled=True,
                               artifact_upload_enabled=True)
    disabled = {entry["method_id"]: entry["reason"]
                for entry in report["disabled_methods"]}
    assert "edits.commit" in disabled
    assert "financial" in disabled["edits.commit"].lower()
    assert report["commit_available"] is False


def test_no_staged_write_body_carries_a_financial_field(unlocked_settings, transport):
    """Drive every staged write and scan what actually reaches the wire."""
    operations, transport = _ops(unlocked_settings, transport)
    operations.edit_insert(PKG)
    transport.requests.clear()

    operations.listing_update(PKG, "edit-1", "en-US", title="t",
                              short_description="s", full_description="f")
    operations.details_update(PKG, "edit-1", contact_email="dev@example.com",
                              contact_phone="+10000000000",
                              contact_website="https://example.com",
                              default_language="en-US")
    operations.testers_update(PKG, "edit-1", "beta", ["qa@example.com"])
    operations.data_safety_update(PKG, "k,v\n1,2")
    operations.device_tier_config_create(PKG, {"deviceGroups": [{"name": "g"}]})
    operations.track_update(PKG, "edit-1", "production", version_codes=[42],
                            status="draft", release_name="r")
    operations.review_reply(PKG, "rev-1", "thanks")
    operations.image_upload(PKG, "edit-1", "en-US", "icon",
                            _b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8))
    operations.bundle_upload(PKG, "edit-1",
                             _b64(_zip_bytes({"base/manifest/AndroidManifest.xml": b"m"})))
    operations.apk_upload(PKG, "edit-1",
                          _b64(_zip_bytes({"AndroidManifest.xml": b"m"})))
    operations.deobfuscation_file_upload(PKG, "edit-1", 42, "proguard",
                                         _b64(b"com.example -> a:\n    int x -> a\n"))

    forbidden = {
        "price", "prices", "tax", "taxRate", "currencyCode", "currency",
        "billingPeriod", "basePlanId", "offerId", "purchaseOptionId",
        "amountMicros", "money", "sku", "productId", "regionalPrices",
    }

    def keys_of(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from keys_of(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys_of(item)

    for verb, path, query, body in transport.requests:
        if verb == "GET":
            continue
        for key in keys_of(body):
            assert key not in forbidden, (verb, path, key)
        for key in query:
            assert key not in forbidden, (verb, path, key)
