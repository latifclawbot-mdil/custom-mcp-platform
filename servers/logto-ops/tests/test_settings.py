"""Tests for startup configuration of the Logto ops server."""
from __future__ import annotations

import os
import stat

import pytest

from logto_ops.settings import (
    DEFAULT_API_INDICATOR,
    ENV_APP_ID,
    ENV_BASE_URL,
    ENV_CF_ID,
    ENV_CF_SECRET_FILE,
    ENV_SECRET_FILE,
    SettingsError,
    load_settings,
)


def write_secret(tmp_path, value: str = "s3cr3t-client-secret-value", mode: int = 0o600, name: str = "logto_m2m_secret"):
    path = tmp_path / name
    path.write_text(value, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def base_env(tmp_path, **overrides):
    env = {
        ENV_BASE_URL: "https://auth.example.org",
        ENV_APP_ID: "example-app-id-0123456789",
        ENV_SECRET_FILE: write_secret(tmp_path, name="m2m_secret"),
    }
    env.update(overrides)
    return env


def test_minimal_configuration_loads(tmp_path):
    settings = load_settings(base_env(tmp_path))
    assert settings.base_url == "https://auth.example.org"
    assert settings.api_root == "https://auth.example.org/api"
    assert settings.token_url == "https://auth.example.org/oidc/token"
    assert settings.api_indicator == DEFAULT_API_INDICATOR


def test_secret_never_appears_in_repr(tmp_path):
    settings = load_settings(base_env(tmp_path))
    assert "s3cr3t-client-secret-value" not in repr(settings)
    assert "s3cr3t-client-secret-value" not in str(settings)


def test_trailing_slash_is_normalised(tmp_path):
    settings = load_settings(base_env(tmp_path, **{ENV_BASE_URL: "https://auth.example.org/"}))
    assert settings.api_root == "https://auth.example.org/api"


def test_plaintext_base_url_is_refused(tmp_path):
    with pytest.raises(SettingsError):
        load_settings(base_env(tmp_path, **{ENV_BASE_URL: "http://auth.example.org"}))


def test_missing_secret_file_variable_is_refused(tmp_path):
    env = base_env(tmp_path)
    del env[ENV_SECRET_FILE]
    with pytest.raises(SettingsError):
        load_settings(env)


def test_world_readable_secret_file_is_refused(tmp_path):
    path = write_secret(tmp_path, mode=0o644, name="loose")
    with pytest.raises(SettingsError):
        load_settings(base_env(tmp_path, **{ENV_SECRET_FILE: path}))


def test_multi_line_secret_file_is_refused(tmp_path):
    path = write_secret(tmp_path, value="one\ntwo", name="multiline")
    with pytest.raises(SettingsError):
        load_settings(base_env(tmp_path, **{ENV_SECRET_FILE: path}))


def test_empty_secret_file_is_refused(tmp_path):
    path = write_secret(tmp_path, value="   ", name="blank")
    with pytest.raises(SettingsError):
        load_settings(base_env(tmp_path, **{ENV_SECRET_FILE: path}))


def test_cloudflare_access_needs_both_id_and_secret(tmp_path):
    path = write_secret(tmp_path, value="cf-service-token-secret", name="cf_secret")
    with pytest.raises(SettingsError):
        load_settings(base_env(tmp_path, **{ENV_CF_SECRET_FILE: path}))
    with pytest.raises(SettingsError):
        load_settings(base_env(tmp_path, **{ENV_CF_ID: "abc.access"}))


def test_cloudflare_access_pair_loads(tmp_path):
    path = write_secret(tmp_path, value="cf-service-token-secret", name="cf_secret")
    settings = load_settings(
        base_env(tmp_path, **{ENV_CF_ID: "abc.access", ENV_CF_SECRET_FILE: path})
    )
    assert settings.cf_access_client_id == "abc.access"
    assert settings.cf_access_client_secret == "cf-service-token-secret"
    assert "cf-service-token-secret" not in repr(settings)


def test_page_size_is_clamped_not_widened(tmp_path):
    settings = load_settings(base_env(tmp_path, LOGTO_MAX_PAGE_SIZE="50"))
    assert settings.clamp_page_size(None) == 50
    assert settings.clamp_page_size(10) == 10
    assert settings.clamp_page_size(5000) == 50


def test_page_size_rejects_non_integers(tmp_path):
    settings = load_settings(base_env(tmp_path))
    for bad in (0, -1, True, "10"):  # type: ignore[list-item]
        with pytest.raises(SettingsError):
            settings.clamp_page_size(bad)  # type: ignore[arg-type]


def test_secret_file_mode_0600_is_accepted(tmp_path):
    settings = load_settings(base_env(tmp_path))
    assert settings.app_secret == "s3cr3t-client-secret-value"
