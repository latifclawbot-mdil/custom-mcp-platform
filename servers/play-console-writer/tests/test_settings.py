"""Settings tests: fail-closed allowlist, gates, loopback-only override."""
from __future__ import annotations

import pytest

from play_console_writer.settings import (
    Settings,
    SettingsError,
    load_settings,
)

VALID = {"PLAY_WRITER_ALLOWED_PACKAGES": "com.example.app"}


def test_missing_allowlist_refuses_to_start():
    with pytest.raises(SettingsError, match="is required"):
        load_settings({})


def test_empty_allowlist_refuses_to_start():
    with pytest.raises(SettingsError, match="at least one package"):
        load_settings({"PLAY_WRITER_ALLOWED_PACKAGES": " , ,"})


def test_invalid_package_in_allowlist_is_rejected():
    with pytest.raises(SettingsError, match="invalid package"):
        load_settings({"PLAY_WRITER_ALLOWED_PACKAGES": "NotValid"})


def test_valid_allowlist_parses_multiple():
    settings = load_settings({"PLAY_WRITER_ALLOWED_PACKAGES": "com.a.app, com.b.app"})
    assert settings.packages == frozenset({"com.a.app", "com.b.app"})


def test_gates_default_off():
    settings = load_settings(VALID)
    assert settings.enable_release_gate is False
    assert settings.enable_artifact_upload is False


def test_gates_parse_truthy_values():
    settings = load_settings({
        **VALID,
        "PLAY_WRITER_ENABLE_RELEASE_GATE": "true",
        "PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD": "yes",
    })
    assert settings.enable_release_gate is True
    assert settings.enable_artifact_upload is True


def test_allowlist_enforced_at_call_time():
    settings = Settings(packages=frozenset({"com.example.app"}))
    assert settings.require_allowed_package("com.example.app") == "com.example.app"
    with pytest.raises(SettingsError, match="not in the allowlist"):
        settings.require_allowed_package("com.other.app")


def test_subject_requires_credentials_file():
    with pytest.raises(SettingsError, match="requires"):
        load_settings({**VALID, "PLAY_WRITER_SUBJECT": "user@example.com"})


def test_release_gate_helper_fails_closed():
    settings = Settings(packages=frozenset({"com.example.app"}))
    with pytest.raises(SettingsError, match="PLAY_WRITER_ENABLE_RELEASE_GATE"):
        settings.require_release_gate_enabled("commit")


def test_artifact_upload_helper_fails_closed():
    settings = Settings(packages=frozenset({"com.example.app"}))
    with pytest.raises(SettingsError, match="PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD"):
        settings.require_artifact_upload_enabled("upload")


def test_loopback_base_accepted():
    settings = load_settings({**VALID, "PLAY_WRITER_TEST_LOOPBACK_BASE": "http://127.0.0.1:1234"})
    assert settings.test_loopback_base == "http://127.0.0.1:1234"


@pytest.mark.parametrize("bad", [
    "https://evil.example.com",
    "https://androidpublisher.googleapis.com",
    "http://10.0.0.1:80",
])
def test_non_loopback_base_is_refused(bad):
    with pytest.raises(SettingsError, match="loopback"):
        load_settings({**VALID, "PLAY_WRITER_TEST_LOOPBACK_BASE": bad})


def test_static_token_requires_a_loopback_base():
    """A static token must never be usable against the real Google host."""
    with pytest.raises(SettingsError, match="only valid together with"):
        load_settings({**VALID, "PLAY_WRITER_TEST_STATIC_TOKEN": "abc"})


def test_static_token_accepted_with_loopback_base():
    settings = load_settings({
        **VALID,
        "PLAY_WRITER_TEST_LOOPBACK_BASE": "http://127.0.0.1:9",
        "PLAY_WRITER_TEST_STATIC_TOKEN": "abc",
    })
    assert settings.test_static_token == "abc"


def test_repr_hides_secrets():
    settings = Settings(packages=frozenset({"com.example.app"}),
                       credentials_path="/secret/path.json", subject="user@example.com")
    text = repr(settings)
    assert "/secret/path.json" not in text
    assert "user@example.com" not in text


def test_clamp_page_size_bounds():
    settings = Settings(packages=frozenset({"com.example.app"}), max_page_size=50)
    assert settings.clamp_page_size(None) == 50
    assert settings.clamp_page_size(10) == 10
    assert settings.clamp_page_size(1000) == 50
    with pytest.raises(SettingsError):
        settings.clamp_page_size(0)