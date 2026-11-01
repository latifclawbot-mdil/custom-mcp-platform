"""RED tests: settings + package allowlist (fail closed)."""
from __future__ import annotations

import pytest

from play_console_mcp.settings import Settings, SettingsError, load_settings

BASE = {
    "PLAY_CONSOLE_ALLOWED_PACKAGES": "com.example.app,com.example.other",
}


def test_load_settings_parses_allowlist():
    s = load_settings(env=dict(BASE))
    assert s.packages == frozenset({"com.example.app", "com.example.other"})


def test_load_settings_fails_closed_when_allowlist_missing():
    with pytest.raises(SettingsError, match="PLAY_CONSOLE_ALLOWED_PACKAGES"):
        load_settings(env={})


def test_load_settings_fails_closed_when_allowlist_empty():
    with pytest.raises(SettingsError, match="allowlist"):
        load_settings(env={"PLAY_CONSOLE_ALLOWED_PACKAGES": "  ,  "})


def test_load_settings_rejects_invalid_package_in_allowlist():
    with pytest.raises(SettingsError, match="invalid package"):
        load_settings(env={"PLAY_CONSOLE_ALLOWED_PACKAGES": "com.example.app,../etc"})
    with pytest.raises(SettingsError, match="invalid package"):
        load_settings(env={"PLAY_CONSOLE_ALLOWED_PACKAGES": "com.example.app/com"})
    with pytest.raises(SettingsError, match="invalid package"):
        load_settings(env={"PLAY_CONSOLE_ALLOWED_PACKAGES": "com.exa mple.app"})


def test_settings_require_allowed_package_rejects_unlisted():
    s = Settings(packages=frozenset({"com.example.app"}))
    assert s.require_allowed_package("com.example.app") == "com.example.app"
    with pytest.raises(SettingsError, match="not in the allowlist"):
        s.require_allowed_package("com.evil.app")


def test_settings_require_allowed_package_rejects_injection():
    s = Settings(packages=frozenset({"com.example.app"}))
    for bad in ["../../admin", "com.example.app/..", "com.example.app?x=1",
                "com.example.app#f", "com.example.app:foo", "com.example.app%2f"]:
        with pytest.raises(SettingsError):
            s.require_allowed_package(bad)


def test_settings_bounds_page_size():
    s = Settings(packages=frozenset({"com.example.app"}), max_page_size=50)
    assert s.clamp_page_size(None) == 50
    assert s.clamp_page_size(10) == 10
    assert s.clamp_page_size(10_000) == 50
    with pytest.raises(SettingsError):
        s.clamp_page_size(0)
    with pytest.raises(SettingsError):
        s.clamp_page_size(-3)


def test_load_settings_reads_timeout_from_env():
    env = dict(BASE, PLAY_CONSOLE_REQUEST_TIMEOUT="7.5")
    assert load_settings(env=env).request_timeout == 7.5


def test_load_settings_rejects_malformed_timeout():
    env = dict(BASE, PLAY_CONSOLE_REQUEST_TIMEOUT="soon")
    with pytest.raises(SettingsError, match="PLAY_CONSOLE_REQUEST_TIMEOUT"):
        load_settings(env=env)


def test_settings_repr_does_not_leak_credentials_path_secret_scope():
    s = Settings(packages=frozenset({"com.example.app"}),
                 credentials_path="/secret/sa.json", subject="ops@example.com")
    text = repr(s)
    assert "/secret/sa.json" not in text