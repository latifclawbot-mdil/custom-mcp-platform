"""Deployment and behavioural checks specific to the Logto ops server.

These do not replace the per-server suite in ``servers/logto-ops/tests`` (which
covers the gate, the confirmation tokens, the client and every handler). They
assert the properties an operator cares about that only the *compose entry* can
express: which secrets are mounted, that no credential is an inline value, and
that the mode is not shipped as something more permissive than intended.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_deployment_artifacts import REPO, compose  # noqa: F401 - fixture re-exported

#: The compose fixture substitutes every ${VAR...} with `value-of-VAR`, so the
#: raw text is the only place interpolation syntax can be asserted.
RAW = (REPO / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
SERVICE_BLOCK = RAW.split("\n  logto-ops-mcp:", 1)[1].split("\n  play-console-mcp:", 1)[0]

SERVICE = "logto-ops-mcp"


def test_service_mounts_the_logto_token_secret(compose):
    env = compose["services"][SERVICE]["environment"]
    assert env["MCP_HTTP_AUTH_TOKEN_FILE"] == "/run/secrets/logto_mcp_token"
    assert "MCP_HTTP_AUTH_TOKEN" not in env
    assert "logto_mcp_token" in compose["services"][SERVICE]["secrets"]


def test_m2m_secret_is_a_file_not_an_environment_value(compose):
    env = compose["services"][SERVICE]["environment"]
    assert env["LOGTO_M2M_SECRET_FILE"] == "/run/secrets/logto_m2m_secret"
    assert "LOGTO_M2M_SECRET" not in env, "the client secret must not be an env value"
    assert "logto_m2m_secret" in compose["services"][SERVICE]["secrets"]


def test_confirmation_key_is_a_file_not_an_environment_value(compose):
    env = compose["services"][SERVICE]["environment"]
    assert env["LOGTO_MCP_CONFIRM_SECRET_FILE"] == "/run/secrets/logto_confirm_secret"
    assert "LOGTO_MCP_CONFIRM_SECRET" not in env
    assert "logto_confirm_secret" in compose["services"][SERVICE]["secrets"]


def test_confirm_ttl_default_is_short_enough_to_matter(compose):
    # A confirmation that can be replayed hours later is not a review of the
    # action; 15 minutes is the shipped default.
    env = compose["services"][SERVICE]["environment"]
    assert "LOGTO_MCP_CONFIRM_TTL_SECONDS" in env
    assert 'LOGTO_MCP_CONFIRM_TTL_SECONDS: "${LOGTO_MCP_CONFIRM_TTL_SECONDS:-900}"' in SERVICE_BLOCK


def test_shipped_mode_is_apply_not_admin(compose):
    assert 'LOGTO_MCP_MODE: "${LOGTO_MCP_MODE:-apply}"' in SERVICE_BLOCK
    assert "admin" not in SERVICE_BLOCK.split("LOGTO_MCP_MODE")[1].split("\n")[0]


def test_logto_base_url_is_a_required_variable(compose):
    assert "LOGTO_BASE_URL: \"${LOGTO_BASE_URL:?" in SERVICE_BLOCK


def test_alias_matches_the_host_allowlist(compose):
    service = compose["services"][SERVICE]
    aliases = service["networks"]["coolify"]["aliases"]
    entries = [e.strip() for e in service["environment"]["MCP_HTTP_ALLOWED_HOSTS"].split(",")]
    for alias in aliases:
        assert alias in entries, (alias, entries)


def test_service_publishes_no_host_port(compose):
    service = compose["services"][SERVICE]
    assert "ports" not in service
    assert "hostname" not in service


def test_image_does_not_bake_a_logto_credential():
    text = (REPO / "deploy" / "Dockerfile.logto-ops").read_text(encoding="utf-8")
    assert "LOGTO_M2M_SECRET=" not in text
    assert "LOGTO_MCP_CONFIRM_SECRET=" not in text
    assert not any(
        line.startswith("COPY") and ("secret" in line.lower() or ".json" in line)
        for line in text.splitlines()
    ), "no credential file may be copied into the image"
    # Exactly one server package, and it is this one.
    copied = [line for line in text.splitlines() if line.startswith("COPY servers/")]
    assert len(copied) == 1
    assert "logto-ops" in copied[0]
