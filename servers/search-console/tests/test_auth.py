"""Tests for credential laziness, scope pinning, and redaction of auth failures."""

import pytest

from search_console_mcp import auth


class FakeCredentials:
    def __init__(self, scopes=None):
        self.scopes = scopes
        self.token = "ya29.FAKE-SECRET-TOKEN-abcdefghij"


class TestLazyAcquisition:
    def setup_method(self):
        auth.reset_session()

    def teardown_method(self):
        auth.reset_session()

    def test_no_credentials_are_touched_at_import(self):
        """Importing the auth module must not invoke google.auth.default."""
        import importlib
        import sys

        calls = []

        def boom(*a, **kw):
            calls.append(1)
            raise AssertionError("ADC must not be loaded at import time")

        sys.modules.pop("search_console_mcp.auth", None)
        import google.auth as gauth

        original = gauth.default
        gauth.default = boom
        try:
            importlib.import_module("search_console_mcp.auth")
            assert calls == []
        finally:
            gauth.default = original

    def test_first_get_session_builds_once_and_caches(self, monkeypatch):
        builds = []

        def fake_build():
            builds.append(1)
            return FakeCredentials()

        monkeypatch.setattr(auth, "_build_session", fake_build)
        a = auth.get_session()
        b = auth.get_session()
        assert a is b
        assert len(builds) == 1

    def test_reset_forces_a_rebuild(self, monkeypatch):
        builds = []

        def fake_build():
            builds.append(1)
            return FakeCredentials()

        monkeypatch.setattr(auth, "_build_session", fake_build)
        auth.get_session()
        auth.reset_session()
        auth.get_session()
        assert len(builds) == 2

    def test_scope_constant_is_read_only(self):
        assert auth.SCOPES == ("https://www.googleapis.com/auth/webmasters.readonly",)

    def test_no_read_write_scope_anywhere_in_package(self):
        import pathlib

        src = pathlib.Path(auth.__file__).parent
        for path in src.glob("*.py"):
            text = path.read_text()
            assert "auth/webmasters\"" not in text, path.name
            # The read/write scope, unqualified, must never appear as a literal.
            assert '"/auth/webmasters"' not in text, path.name


class TestCredentialErrors:
    def setup_method(self):
        auth.reset_session()

    def teardown_method(self):
        auth.reset_session()

    def test_adc_failure_raises_generic_credential_error(self, monkeypatch):
        import google.auth as gauth

        def boom(**kw):
            raise Exception(
                "/home/secret/svc.json principal robot@example.internal invalid"
            )

        monkeypatch.setattr(gauth, "default", boom)
        with pytest.raises(auth.CredentialError) as ei:
            auth._build_session()
        msg = str(ei.value)
        assert "/home/secret/svc.json" not in msg
        assert "robot@example.internal" not in msg

    def test_scopes_are_requested_exactly(self, monkeypatch):
        import google.auth as gauth

        captured = {}

        def fake_default(scopes=None):
            captured["scopes"] = scopes
            return FakeCredentials(scopes=scopes), "proj"

        monkeypatch.setattr(gauth, "default", fake_default)
        auth._build_session()
        assert captured["scopes"] == ["https://www.googleapis.com/auth/webmasters.readonly"]

    def test_broader_credential_scopes_are_refused(self, monkeypatch):
        import google.auth as gauth

        def fake_default(scopes=None):
            return FakeCredentials(scopes=["https://www.googleapis.com/auth/webmasters"]), "p"

        monkeypatch.setattr(gauth, "default", fake_default)
        with pytest.raises(auth.CredentialError):
            auth._build_session()


class TestTokenNotExposed:
    def test_session_repr_and_attributes_are_not_logged(self, monkeypatch, caplog):
        import google.auth as gauth

        def fake_default(scopes=None):
            return FakeCredentials(scopes=scopes), "p"

        monkeypatch.setattr(gauth, "default", fake_default)
        with caplog.at_level("DEBUG"):
            session = auth._build_session()
        assert "ya29.FAKE-SECRET-TOKEN-abcdefghij" not in caplog.text
        # The token lives on the credentials object, which we hand to the
        # transport and never read ourselves.
        assert not hasattr(session, "_search_console_token")