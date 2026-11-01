"""RED tests: credential loading (ADC lazy, service account, delegated subject)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from play_console_mcp.auth import SCOPE, CredentialError, load_credentials


def test_scope_is_androidpublisher():
    assert SCOPE == "https://www.googleapis.com/auth/androidpublisher"


def test_lazy_adc_is_not_invoked_until_needed(monkeypatch):
    calls = {"n": 0}
    import google.auth

    def fake_default(scopes=None):
        calls["n"] += 1
        raise AssertionError("should be deferred")

    monkeypatch.setattr(google.auth, "default", fake_default)
    cfg = load_credentials(credentials_path=None, subject=None, lazy=True)
    assert calls["n"] == 0
    assert cfg.deferred is True


def test_deferred_credentials_resolve_on_demand(monkeypatch):
    sentinel = object()
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (sentinel, "proj"))
    cfg = load_credentials(credentials_path=None, subject=None, lazy=True)
    assert cfg.resolve() is sentinel


def test_once_resolved_adc_is_cached(monkeypatch):
    calls = {"n": 0}
    import google.auth

    def fake_default(scopes=None):
        calls["n"] += 1
        return object(), "proj"

    monkeypatch.setattr(google.auth, "default", fake_default)
    cfg = load_credentials(credentials_path=None, subject=None, lazy=True)
    cfg.resolve()
    cfg.resolve()
    assert calls["n"] == 1


def test_missing_adc_raises_clear_error(monkeypatch):
    import google.auth
    import google.auth.exceptions as gexc

    def boom(scopes=None):
        raise gexc.DefaultCredentialsError("no adc")

    monkeypatch.setattr(google.auth, "default", boom)
    cfg = load_credentials(credentials_path=None, subject=None, lazy=True)
    with pytest.raises(CredentialError, match="Application Default Credentials"):
        cfg.resolve()


def test_service_account_path_missing_file_raises(tmp_path: Path):
    cfg = load_credentials(credentials_path=str(tmp_path / "nope.json"),
                          subject=None, lazy=True)
    with pytest.raises(CredentialError, match="not found"):
        cfg.resolve()


def test_service_account_with_subject_delegates(tmp_path: Path, monkeypatch):
    sa = tmp_path / "sa.json"
    sa.write_text(json.dumps({
        "type": "service_account",
        "project_id": "p",
        "private_key_id": "kid",
        "private_key": "-----BEGIN PRIVATE KEY-----\nnotreal\n-----END PRIVATE KEY-----\n",
        "client_email": "svc@p.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }))
    seen = {}

    class FakeCred:
        def with_subject(self, subject):
            seen["subject"] = subject
            return self

    def fake_from_file(path, **kw):
        seen["from_file_path"] = path
        seen["from_file_scopes"] = list(kw.get("scopes") or [])
        return FakeCred()

    import google.oauth2.service_account as sa_mod

    monkeypatch.setattr(sa_mod.Credentials, "from_service_account_file",
                        staticmethod(fake_from_file))

    cfg = load_credentials(credentials_path=str(sa), subject="ops@example.com", lazy=True)
    cred = cfg.resolve()

    assert seen["subject"] == "ops@example.com"
    assert seen["from_file_path"] == str(sa)
    assert seen["from_file_scopes"] == [SCOPE]
    assert isinstance(cred, FakeCred)


def test_service_account_from_file_receives_scope(tmp_path: Path, monkeypatch):
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    seen = {}
    import google.oauth2.service_account as sa_mod

    class FakeCred:
        def with_subject(self, s):
            seen["subject"] = s
            return self

    def fake_from_file(path, **kw):
        seen.update(kw)
        return FakeCred()

    monkeypatch.setattr(sa_mod.Credentials, "from_service_account_file",
                        staticmethod(fake_from_file))
    cfg = load_credentials(credentials_path=str(sa), subject=None, lazy=True)
    cfg.resolve()
    assert seen["scopes"] == [SCOPE]


def test_credential_config_repr_hides_path_and_subject():
    cfg = load_credentials(credentials_path="/secret/path/sa.json",
                          subject="ops@example.com", lazy=True)
    text = repr(cfg)
    assert "/secret/path/sa.json" not in text
    assert "ops@example.com" not in text