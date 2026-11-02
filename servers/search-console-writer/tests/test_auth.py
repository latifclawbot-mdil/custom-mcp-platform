"""Credential handling for the write server.

Two properties matter more here than anywhere else in the package:

**Scope.** The read/write ``webmasters`` scope is the *only* scope requested.
A credential carrying anything broader is refused, because this server is the
one component that can mutate a property.

**Separation.** The write credential is resolved from write-specific
configuration (``GSC_WRITE_SERVICE_ACCOUNT_FILE``), never from the read
server's implied identity. A deployment can therefore keep the read-only and
write credentials distinct principals.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from search_console_writer import auth

SRC = Path(__file__).resolve().parents[1] / "src"


class TestScope:
    def test_only_the_read_write_scope_is_requested(self):
        assert auth.SCOPES == ("https://www.googleapis.com/auth/webmasters",)

    def test_the_read_only_scope_is_not_requested(self):
        assert "https://www.googleapis.com/auth/webmasters.readonly" not in auth.SCOPES


class TestLaziness:
    def test_importing_the_module_does_not_build_a_session(self):
        # A fresh interpreter: importing auth must not touch ADC.
        script = (
            "import sys\n"
            "import google.auth\n"
            "calls = []\n"
            "google.auth.default = lambda *a, **k: calls.append(1)\n"
            "import search_console_writer.auth as auth\n"
            "print(len(calls))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={"PATH": __import__("os").environ.get("PATH", ""), "PYTHONPATH": str(SRC)},
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "0"

    def test_reset_session_drops_the_cache(self):
        auth.reset_session()
        assert auth._cached_session() is None


class TestStaticTokenSeam:
    """The offline seam must be inert unless the API host is loopback."""

    def test_static_token_requires_a_loopback_base(self, monkeypatch):
        auth.reset_session()
        monkeypatch.setenv(auth.STATIC_TOKEN_ENV, "test-token")
        monkeypatch.setenv(auth.client.BASE_OVERRIDE_ENV, "https://evil.example/webmasters/v3")
        assert auth._loopback_override_active() is False

    def test_static_token_is_used_on_a_loopback_base(self, monkeypatch):
        auth.reset_session()
        monkeypatch.setenv(auth.STATIC_TOKEN_ENV, "test-token")
        monkeypatch.setenv(auth.client.BASE_OVERRIDE_ENV, "http://127.0.0.1:9/webmasters/v3")
        session = auth.build_session()
        assert session is not None
        assert auth._loopback_override_active() is True

    def test_no_static_token_without_a_base_override(self, monkeypatch):
        auth.reset_session()
        monkeypatch.delenv(auth.client.BASE_OVERRIDE_ENV, raising=False)
        monkeypatch.setenv(auth.STATIC_TOKEN_ENV, "test-token")
        assert auth._loopback_override_active() is False


class TestServiceAccountLoading:
    def test_service_account_file_is_preferred_when_configured(self, monkeypatch, tmp_path):
        """A configured SA file must be the credential source, not ambient ADC."""
        marker = tmp_path / "sa.json"
        marker.write_text("{}")

        used = {}

        class FakeCreds:
            scopes = ["https://www.googleapis.com/auth/webmasters"]

        def fake_loader(path):
            used["path"] = path
            return FakeCreds()

        def forbidden_adc():
            raise AssertionError("ADC must not be consulted when an SA file is configured")

        monkeypatch.setenv(auth.SERVICE_ACCOUNT_ENV, str(marker))
        monkeypatch.delenv(auth.client.BASE_OVERRIDE_ENV, raising=False)
        monkeypatch.setattr(auth, "_load_service_account", fake_loader, raising=False)
        monkeypatch.setattr(auth, "_load_adc", forbidden_adc, raising=False)

        session = auth.build_session()
        assert used["path"] == str(marker)
        assert session is not None

    def test_real_loader_requests_only_the_write_scope(self, monkeypatch):
        """The loader that ships must hand google-auth exactly the write scope."""
        import google.oauth2.service_account as sa

        captured = {}

        class FakeSACredentials:
            scopes = ["https://www.googleapis.com/auth/webmasters"]

            @staticmethod
            def from_service_account_file(path, scopes=None):
                captured["path"] = path
                captured["scopes"] = scopes
                return FakeSACredentials()

        monkeypatch.setattr(
            sa.Credentials, "from_service_account_file", FakeSACredentials.from_service_account_file
        )

        auth._load_service_account("/tmp/sa.json")
        assert captured["scopes"] == list(auth.SCOPES)

    def test_missing_service_account_file_is_a_clean_configuration_error(self, monkeypatch):
        auth.reset_session()
        monkeypatch.setenv(auth.SERVICE_ACCOUNT_ENV, "/nonexistent/sa.json")
        monkeypatch.delenv(auth.client.BASE_OVERRIDE_ENV, raising=False)
        with pytest.raises(auth.CredentialError) as excinfo:
            auth.build_session()
        # The path may appear in an operator-facing configuration error, but no
        # key material or token may.
        assert "PRIVATE KEY" not in str(excinfo.value)


class TestScopeConclusion:
    def test_broader_granted_scopes_are_refused(self, monkeypatch):
        auth.reset_session()
        monkeypatch.delenv(auth.SERVICE_ACCOUNT_ENV, raising=False)
        monkeypatch.delenv(auth.client.BASE_OVERRIDE_ENV, raising=False)

        class BroadCreds:
            scopes = [
                "https://www.googleapis.com/auth/webmasters",
                "https://www.googleapis.com/auth/cloud-platform",
            ]

        monkeypatch.setattr(auth, "_load_adc", lambda: BroadCreds(), raising=False)
        with pytest.raises(auth.CredentialError):
            auth.build_session()

    def test_the_write_scope_alone_is_accepted(self, monkeypatch):
        auth.reset_session()
        monkeypatch.delenv(auth.SERVICE_ACCOUNT_ENV, raising=False)
        monkeypatch.delenv(auth.client.BASE_OVERRIDE_ENV, raising=False)

        class GoodCreds:
            scopes = ["https://www.googleapis.com/auth/webmasters"]

        monkeypatch.setattr(auth, "_load_adc", lambda: GoodCreds(), raising=False)
        assert auth.build_session() is not None