"""Operation tests: real paths, argument validation, edit-ownership and gates."""
from __future__ import annotations

import base64

import pytest

from play_console_writer.edit_registry import EditRegistry
from play_console_writer.errors import LocalError
from play_console_writer.operations import TOOL_SPECS, WriteOperations
from play_console_writer.settings import Settings
from play_console_writer.validation import ValidationError

from conftest import FakeApiTransport, FakeToken

PKG = "com.example.app"
BASE = "/androidpublisher/v3/applications/com.example.app"


@pytest.fixture
def ops(settings, transport):
    from play_console_writer.client import GoogleWriteClient

    client = GoogleWriteClient(settings=settings, credentials=FakeToken(),
                               transport=transport)
    registry = EditRegistry()
    return WriteOperations(client=client, settings=settings, registry=registry), transport, registry


@pytest.fixture
def unlocked_ops(unlocked_settings, transport):
    from play_console_writer.client import GoogleWriteClient

    client = GoogleWriteClient(settings=unlocked_settings, credentials=FakeToken(),
                               transport=transport)
    registry = EditRegistry()
    return WriteOperations(client=client, settings=unlocked_settings,
                           registry=registry), transport, registry


def open_edit(ops, transport) -> str:
    result = ops.edit_insert(PKG)
    # Reset the recorder so subsequent assertions index from the caller's first
    # action, not the edit-insert POST.
    transport.requests.clear()
    return result["edit_id"]


# --------------------------------------------------------------------------
# Edit session plumbing
# --------------------------------------------------------------------------

def test_edit_insert_posts_edits_and_tracks_the_id(ops):
    operations, transport, registry = ops
    result = operations.edit_insert(PKG)
    verb, path, query, body = transport.last_write()
    assert verb == "POST"
    assert path == f"{BASE}/edits"
    assert result["edit_id"] == "edit-1"
    registry.require_owned(PKG, "edit-1")  # does not raise


def test_edit_insert_rejects_non_allowlisted_package(ops):
    operations, transport, _ = ops
    with pytest.raises(Exception):
        operations.edit_insert("com.other.app")
    assert transport.requests == []


def test_staged_operation_requires_an_edit_this_server_opened(ops):
    operations, transport, _ = ops
    with pytest.raises(LocalError, match="was not opened by this server"):
        operations.listing_update(PKG, "foreign-edit", "en-US", title="x")
    assert transport.writes() == []


def test_edit_delete_releases_the_edit(ops):
    operations, transport, registry = ops
    edit = open_edit(operations, transport)
    operations.edit_delete(PKG, edit)
    with pytest.raises(LocalError, match="was not opened by this server"):
        registry.require_owned(PKG, edit)


# --------------------------------------------------------------------------
# Listings
# --------------------------------------------------------------------------

def test_listing_update_puts_and_reads_back(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    result = operations.listing_update(PKG, edit, "en-US", title="New", short_description="s")
    # The write is the PUT; the last recorded request is the read-back GET.
    writes = transport.writes()
    assert writes[0][0] == "PUT"
    assert writes[0][1] == f"{BASE}/edits/edit-1/listings/en-US"
    assert writes[0][3] == {"title": "New", "shortDescription": "s"}
    assert transport.requests[-1][0] == "GET"
    assert transport.requests[-1][1] == f"{BASE}/edits/edit-1/listings/en-US"
    assert "read_back" in result and "listing" in result["read_back"]


def test_listing_update_requires_at_least_one_field(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="at least one field"):
        operations.listing_update(PKG, edit, "en-US")


def test_listing_patch_uses_patch_verb(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    operations.listing_patch(PKG, edit, "en-US", full_description="f")
    verb = transport.writes()[0][0]
    assert verb == "PATCH"


def test_listing_rejects_control_characters(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="control characters"):
        operations.listing_update(PKG, edit, "en-US", title="bad\x00title")


def test_listing_video_must_be_https(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="https"):
        operations.listing_update(PKG, edit, "en-US", video="http://insecure.example")


def test_listing_delete_and_deleteall(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    operations.listing_delete(PKG, edit, "en-US")
    operations.listings_delete_all(PKG, edit)
    writes = transport.writes()
    assert (writes[0][0], writes[0][1]) == ("DELETE", f"{BASE}/edits/edit-1/listings/en-US")
    assert (writes[1][0], writes[1][1]) == ("DELETE", f"{BASE}/edits/edit-1/listings")


# --------------------------------------------------------------------------
# Details
# --------------------------------------------------------------------------

def test_details_update_puts_contact_info(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    operations.details_update(PKG, edit, contact_email="dev@example.com",
                              contact_website="https://example.com",
                              default_language="en-US")
    verb, path, query, body = transport.writes()[0]
    assert verb == "PUT"
    assert path == f"{BASE}/edits/edit-1/details"
    assert body == {"contactEmail": "dev@example.com",
                    "contactWebsite": "https://example.com",
                    "defaultLanguage": "en-US"}


def test_details_rejects_non_email(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="@"):
        operations.details_update(PKG, edit, contact_email="not-an-email")


# --------------------------------------------------------------------------
# Testers
# --------------------------------------------------------------------------

def test_testers_update_sends_google_groups(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    result = operations.testers_update(PKG, edit, "beta", ["qa@example.com"])
    verb, path, query, body = transport.writes()[0]
    assert verb == "PUT"
    assert path == f"{BASE}/edits/edit-1/testers/beta"
    assert body == {"googleGroups": ["qa@example.com"]}
    assert "api_limitation" in result


def test_testers_rejects_unknown_track(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="track must be one of"):
        operations.testers_update(PKG, edit, "not-a-track", ["qa@example.com"])


def test_testers_rejects_non_email_group(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="email"):
        operations.testers_update(PKG, edit, "beta", ["not-an-email"])


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------

def test_data_safety_posts_csv(ops):
    operations, transport, _ = ops
    operations.data_safety_update(PKG, "key,value\n1,2")
    verb, path, query, body = transport.writes()[0]
    assert verb == "POST"
    assert path == f"{BASE}/dataSafety"
    assert body == {"safetyLabels": "key,value\n1,2"}


def test_device_tier_config_create_rejects_unknown_keys(ops):
    operations, transport, _ = ops
    with pytest.raises(ValidationError, match="unsupported keys"):
        operations.device_tier_config_create(PKG, {"price": "9.99"})
    assert transport.writes() == []


def test_device_tier_config_create_posts_and_reads_back(ops):
    operations, transport, _ = ops
    result = operations.device_tier_config_create(
        PKG, {"deviceGroups": [{"name": "g"}]}, allow_unknown_devices=True)
    verb, path, query, body = transport.writes()[0]
    assert verb == "POST"
    assert path == f"{BASE}/deviceTierConfigs"
    assert query == {"allowUnknownDevices": "true"}
    assert "read_back" in result


# --------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------

def test_review_reply_posts_text(ops):
    operations, transport, _ = ops
    operations.review_reply(PKG, "rev-1", "thanks for the feedback")
    verb, path, query, body = transport.writes()[0]
    assert verb == "POST"
    assert path == f"{BASE}/reviews/rev-1:reply"
    assert body == {"replyText": "thanks for the feedback"}


def test_review_reply_refuses_empty(ops):
    operations, transport, _ = ops
    with pytest.raises(ValidationError, match="must not be empty"):
        operations.review_reply(PKG, "rev-1", "   ")


def test_review_reply_respects_configured_max_length(ops):
    operations, transport, _ = ops
    with pytest.raises(ValidationError, match="characters"):
        operations.review_reply(PKG, "rev-1", "x" * 400)


# --------------------------------------------------------------------------
# Release gate
# --------------------------------------------------------------------------

def test_track_update_refused_when_release_gate_is_off(ops):
    operations, transport, _ = ops
    with pytest.raises(Exception, match="release gate"):
        operations.track_update(PKG, "edit-1", "production", version_codes=[42])
    assert transport.writes() == []


def test_track_update_puts_release_definition_when_enabled(unlocked_ops):
    operations, transport, registry = unlocked_ops
    edit = open_edit(operations, transport)
    operations.track_update(PKG, edit, "production", version_codes=[42],
                            status="inProgress", user_fraction=0.2,
                            release_notes={"en-US": "notes"})
    verb, path, query, body = transport.writes()[0]
    assert verb == "PUT"
    assert path == f"{BASE}/edits/{edit}/tracks/production"
    assert body["releases"][0]["versionCodes"] == ["42"]
    assert body["releases"][0]["userFraction"] == 0.2
    assert body["releases"][0]["releaseNotes"] == [{"language": "en-US", "text": "notes"}]


@pytest.mark.parametrize("track", ["wear:production", "automotive:beta"])
def test_track_update_allows_documented_prefixed_tracks(unlocked_ops, track):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    operations.track_update(PKG, edit, track, version_codes=[42], status="draft")
    assert transport.writes()[0][1].endswith("/tracks/" + track)


def test_track_update_rejects_out_of_range_user_fraction(unlocked_ops):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        operations.track_update(PKG, edit, "production", user_fraction=1.0)


def test_commit_operation_does_not_exist(unlocked_ops):
    """Commit is disabled: there is no operation, even with both gates on.

    The Android Publisher API exposes no endpoint that lists what is staged in
    an edit. A shared edit id can therefore carry a financial change staged by
    another actor, and this server cannot establish otherwise. With the user's
    financial exclusion in force, the only fail-closed choice is no commit.
    """
    operations, transport, _ = unlocked_ops
    assert not hasattr(operations, "edit_commit")


def test_no_operation_can_send_a_commit_request(unlocked_ops):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    # Nothing in the public operation surface stages a commit, and the client
    # registry has no spec for it, so even a direct method id is refused.
    from play_console_writer.client import METHOD_SPECS

    assert "edits.commit" not in METHOD_SPECS
    operations.track_update(PKG, edit, "production", version_codes=[42],
                            status="draft")
    assert not any(w[1].endswith(":commit") for w in transport.requests)


# --------------------------------------------------------------------------
# Artifact uploads (opt-in)
# --------------------------------------------------------------------------

def test_image_upload_refused_when_gate_off(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    with pytest.raises(Exception, match="PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD"):
        operations.image_upload(PKG, edit, "en-US", "icon",
                                base64.b64encode(b"\x89PNG\r\n\x1a\nx").decode())


def test_image_upload_posts_media_when_enabled(unlocked_ops):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    operations.image_upload(PKG, edit, "en-US", "icon", png)
    verb, path, query, body = transport.writes()[0]
    assert verb == "POST"
    assert path == f"{BASE}/edits/{edit}/listings/en-US/icon"
    assert query["uploadType"] == "media"
    assert body.startswith(b"\x89PNG")


def test_image_upload_rejects_unknown_image_type(unlocked_ops):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="image_type must be one of"):
        operations.image_upload(PKG, edit, "en-US", "not-a-type", "AAAA")


def test_bundle_upload_requires_zip_magic(unlocked_ops):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    with pytest.raises(ValidationError, match="AAB"):
        operations.bundle_upload(PKG, edit, base64.b64encode(b"not a zip").decode())


def test_bundle_upload_accepts_zip(unlocked_ops):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    import io
    import zipfile
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("base/manifest/AndroidManifest.xml", b"manifest")
    operations.bundle_upload(PKG, edit, base64.b64encode(archive.getvalue()).decode())
    verb, path, query, body = transport.writes()[0]
    assert path == f"{BASE}/edits/{edit}/bundles"


def test_image_upload_rejects_oversized_payload(unlocked_ops):
    operations, transport, _ = unlocked_ops
    edit = open_edit(operations, transport)
    # 9 MiB > the 8 MiB image cap.
    oversized = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (9 * 1024 * 1024)).decode()
    with pytest.raises(ValidationError, match="upload limit"):
        operations.image_upload(PKG, edit, "en-US", "icon", oversized)
    assert transport.writes() == []


def test_image_delete_and_delete_all(ops):
    operations, transport, _ = ops
    edit = open_edit(operations, transport)
    operations.image_delete(PKG, edit, "en-US", "phoneScreenshots", "img-1")
    operations.images_delete_all(PKG, edit, "en-US", "phoneScreenshots")
    writes = transport.writes()
    assert writes[0][1] == f"{BASE}/edits/edit-1/listings/en-US/phoneScreenshots/img-1"
    assert writes[1][1] == f"{BASE}/edits/edit-1/listings/en-US/phoneScreenshots"


# --------------------------------------------------------------------------
# Coverage: every declared tool has an attr that exists
# --------------------------------------------------------------------------

def test_every_tool_spec_attr_exists():
    operations, _, _ = _dummy()
    for name, spec in TOOL_SPECS.items():
        assert hasattr(operations, spec["attr"]), name


def _dummy():
    from play_console_writer.client import GoogleWriteClient

    settings = Settings(packages=frozenset({PKG}))
    client = GoogleWriteClient(settings=settings, credentials=FakeToken(),
                               transport=FakeApiTransport())
    return WriteOperations(client=client, settings=settings), None, None