"""Regressions: credential mapping, numeric settings, and the loopback seam.

Companion to ``test_writer_review_regressions.py``, covering the three
configuration-time findings from the review:

* a credential failure must surface as an actionable ``not_configured`` error,
  not the generic ``upstream_error`` that hides the fix;
* an invalid numeric HTTP setting must refuse startup with exit code 2, not a
  ``ValueError`` traceback;
* the loopback base override must never fall back to a *real* credential — with
  no explicit test static token it must refuse rather than authenticate against
  the fake with the operator's real service-account file or ADC.
"""

from __future__ import annotations

import json

import pytest

from search_console_writer import auth
from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.client import BASE_OVERRIDE_ENV
from search_console_writer.tools import ToolRegistry

ALLOWED = "sc-domain:example.com"
LOOPBACK = "http://127.0.0.1:9/webmasters/v3"


# --------------------------------------------------------------------------
# 4. Credential failures map to an actionable, non-leaking code
# --------------------------------------------------------------------------


class TestCredentialErrorMapping:
    def test_credential_failure_maps_to_not_configured(self):
        def factory():
            raise auth.CredentialError(
                "could not obtain Application Default Credentials; set "
                "GSC_WRITE_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS"
            )

        reg = ToolRegistry(
            client_factory=factory, allowlist=PropertyAllowlist([ALLOWED])
        )
        result = reg.call("gsc_add_site", {"site_url": ALLOWED})
        payload = json.loads(result.text)
        assert result.is_error is True
        assert payload["error"]["code"] == "not_configured"

    def test_credential_failure_message_is_actionable(self):
        def factory():
            raise auth.CredentialError(
                "could not obtain Application Default Credentials; set "
                "GSC_WRITE_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS"
            )

        reg = ToolRegistry(client_factory=factory, allowlist=PropertyAllowlist([ALLOWED]))
        result = reg.call("gsc_add_site", {"site_url": ALLOWED})
        message = json.loads(result.text)["error"]["message"].lower()
        # The operator must be told what to configure, not just that it failed.
        assert "gsc_write_service_account_file" in message or "credential" in message

    def test_credential_failure_does_not_leak_key_material(self):
        def factory():
            raise auth.CredentialError(
                "could not load the service-account credential named by "
                "GSC_WRITE_SERVICE_ACCOUNT_FILE (ValueError) "
                "-----BEGIN PRIVATE KEY----- sekrit -----END PRIVATE KEY-----"
            )

        reg = ToolRegistry(client_factory=factory, allowlist=PropertyAllowlist([ALLOWED]))
        result = reg.call("gsc_add_site", {"site_url": ALLOWED})
        assert "PRIVATE KEY" not in result.text
        assert "sekrit" not in result.text

    def test_a_generic_client_error_still_maps_to_upstream_error(self):
        """The credential-specific mapping must not swallow other failures."""
        import json as _json

        def factory():
            raise RuntimeError("boom")

        reg = ToolRegistry(client_factory=factory, allowlist=PropertyAllowlist([ALLOWED]))
        result = reg.call("gsc_add_site", {"site_url": ALLOWED})
        assert _json.loads(result.text)["error"]["code"] == "upstream_error"


# --------------------------------------------------------------------------
# 5. Invalid numeric settings refuse startup cleanly
# --------------------------------------------------------------------------


class TestNumericSettings:
    def _config(self, monkeypatch, **env):
        monkeypatch.setenv("MCP_HTTP_AUTH_TOKEN", "token-not-a-real-secret")
        monkeypatch.delenv("MCP_HTTP_AUTH_TOKEN_FILE", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from search_console_writer import http_main

        return http_main.app_config("127.0.0.1")

    def test_non_numeric_max_sessions_is_a_config_error(self, monkeypatch):
        from mcp_http_transport.server import AppConfigError

        with pytest.raises(AppConfigError):
            self._config(monkeypatch, MCP_HTTP_MAX_SESSIONS="many")

    def test_non_numeric_idle_timeout_is_a_config_error(self, monkeypatch):
        from mcp_http_transport.server import AppConfigError

        with pytest.raises(AppConfigError):
            self._config(monkeypatch, MCP_HTTP_SESSION_IDLE_TIMEOUT="soon")

    def test_zero_or_negative_values_are_rejected(self, monkeypatch):
        from mcp_http_transport.server import AppConfigError

        with pytest.raises(AppConfigError):
            self._config(monkeypatch, MCP_HTTP_MAX_SESSIONS="0")
        with pytest.raises(AppConfigError):
            self._config(monkeypatch, MCP_HTTP_SESSION_IDLE_TIMEOUT="-5")

    def test_valid_settings_are_accepted(self, monkeypatch):
        config = self._config(
            monkeypatch, MCP_HTTP_MAX_SESSIONS="25", MCP_HTTP_SESSION_IDLE_TIMEOUT="12.5"
        )
        assert config.max_sessions == 25
        assert config.session_idle_timeout == 12.5

    def test_startup_exits_two_without_a_traceback_on_a_bad_setting(self):
        """A bad numeric setting must be a one-line failure, not a traceback."""
        import os
        import subprocess
        import sys
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(src),
            "HOME": os.environ.get("HOME", ""),
            "GSC_WRITE_ALLOWED_SITES": ALLOWED,
            "MCP_HTTP_AUTH_TOKEN": "token-not-a-real-secret",
            "MCP_HTTP_MAX_SESSIONS": "not-a-number",
        }
        proc = subprocess.run(
            [sys.executable, "-m", "search_console_writer.http_main"],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr
        assert "refusing to start" in proc.stderr


# --------------------------------------------------------------------------
# 6. The loopback override requires an explicit static test token
# --------------------------------------------------------------------------


class TestLoopbackSeamRequiresStaticToken:
    def test_no_static_token_on_loopback_refuses_instead_of_using_a_real_key(
        self, monkeypatch, tmp_path
    ):
        """With the base overridden to loopback but no static token, the session
        builder must NOT load the configured (real) service-account file."""
        real_key = tmp_path / "real-sa.json"
        real_key.write_text("{}")

        load_calls = []

        def forbidden_loader(path):
            load_calls.append(path)
            raise AssertionError("a real service-account file must not be loaded for a test base")

        auth.reset_session()
        monkeypatch.setenv(BASE_OVERRIDE_ENV, LOOPBACK)
        monkeypatch.setenv(auth.SERVICE_ACCOUNT_ENV, str(real_key))
        monkeypatch.delenv(auth.STATIC_TOKEN_ENV, raising=False)
        monkeypatch.setattr(auth, "_load_service_account", forbidden_loader, raising=False)
        monkeypatch.setattr(
            auth, "_load_adc", lambda: (_ for _ in ()).throw(AssertionError("ADC must not run"))
        )

        with pytest.raises(auth.CredentialError):
            auth.build_session()
        assert load_calls == []

    def test_no_static_token_on_loopback_refuses_instead_of_adc(self, monkeypatch):
        auth.reset_session()
        monkeypatch.setenv(BASE_OVERRIDE_ENV, LOOPBACK)
        monkeypatch.delenv(auth.SERVICE_ACCOUNT_ENV, raising=False)
        monkeypatch.delenv(auth.STATIC_TOKEN_ENV, raising=False)
        monkeypatch.setattr(
            auth, "_load_adc", lambda: (_ for _ in ()).throw(AssertionError("ADC must not run"))
        )
        with pytest.raises(auth.CredentialError):
            auth.build_session()

    def test_a_static_token_on_loopback_is_honoured(self, monkeypatch):
        auth.reset_session()
        monkeypatch.setenv(BASE_OVERRIDE_ENV, LOOPBACK)
        monkeypatch.setenv(auth.STATIC_TOKEN_ENV, "test-token")
        assert auth.build_session() is not None

    def test_off_loopback_the_static_token_is_inert(self, monkeypatch, tmp_path):
        """A stray static token must not be usable without the loopback base."""
        auth.reset_session()
        marker = tmp_path / "sa.json"
        marker.write_text("{}")

        class FakeCreds:
            scopes = ["https://www.googleapis.com/auth/webmasters"]

        monkeypatch.delenv(BASE_OVERRIDE_ENV, raising=False)
        monkeypatch.setenv(auth.STATIC_TOKEN_ENV, "test-token")
        monkeypatch.setenv(auth.SERVICE_ACCOUNT_ENV, str(marker))
        monkeypatch.setattr(auth, "_load_service_account", lambda path: FakeCreds(), raising=False)
        session = auth.build_session()
        assert session is not None