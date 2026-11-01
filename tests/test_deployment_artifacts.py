"""Deployment-artifact tests: network reachability, secret hygiene, and no publishing.

These read the files that would actually be deployed and assert the properties
the task requires. They are explicitly *static* checks — they do not build
images (Docker is not required here), and nothing in this file claims a
container was run. What they do prove is that the artifacts have (or do not
have) the properties the isolation and reachability claims depend on: no
published port and no hostname, an external `coolify` network joined with a
per-service alias that is also in the Host allowlist, distinct tokens and
credential paths per service, no secret in a layer, and no Google identifier.

They do **not** claim the containers are unreachable from other containers on
the `coolify` network; that network is shared with the gateway, and the bearer
token plus the absence of a host port is the boundary. See docs/security.md.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "deploy" / "docker-compose.yml"
DOCKERFILES = [REPO / "deploy" / "Dockerfile.play-console", REPO / "deploy" / "Dockerfile.search-console"]

#: Bash-style interpolation that would otherwise break a plain YAML load.
PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


def load_compose() -> dict:
    """Load the compose file, replacing each ``${VAR}`` with its own name.

    A single shared placeholder would make two different variables compare
    equal, hiding exactly the kind of aliasing this file exists to catch.
    """
    text = COMPOSE.read_text(encoding="utf-8")

    def _sub(match: re.Match[str]) -> str:
        name = match.group(0)[2:-1].split(":")[0].split("-")[0]
        return f"value-of-{name}"

    return yaml.safe_load(PLACEHOLDER.sub(_sub, text))


@pytest.fixture(scope="module")
def compose() -> dict:
    return load_compose()


def test_compose_is_valid_yaml_with_both_services(compose):
    services = compose["services"]
    assert set(services) == {"play-console-mcp", "search-console-mcp"}


@pytest.mark.parametrize("service_name", ["play-console-mcp", "search-console-mcp"])
def test_no_service_publishes_a_host_port(compose, service_name):
    service = compose["services"][service_name]
    assert "ports" not in service, "a published port is a public endpoint"
    assert "hostname" not in service, "hostnames are not used; aliases are"
    assert service["expose"] == ["8080"]


@pytest.mark.parametrize("service_name", ["play-console-mcp", "search-console-mcp"])
def test_services_join_the_external_coolify_network_and_the_local_bridge(compose, service_name):
    """LiteLLM reaches these by alias on `coolify`; the local bridge stays for east-west.

    The endpoints must be reachable from the gateway, which lives on the
    external `coolify` network. They must **not** be reachable from off-host:
    that is what `no ports` above guarantees.
    """
    service = compose["services"][service_name]
    networks = service["networks"]
    assert set(networks) == {"coolify", "mcp-internal"}, networks

    # `coolify` is external: compose must consume the existing network, not make one.
    coolify = compose["networks"]["coolify"]
    assert coolify["external"] is True
    assert coolify["name"] == "coolify"

    # The project-local bridge is unchanged and still not attachable.
    assert compose["networks"]["mcp-internal"]["driver"] == "bridge"
    assert compose["networks"]["mcp-internal"].get("attachable") is False


@pytest.mark.parametrize("service_name", ["play-console-mcp", "search-console-mcp"])
def test_each_service_has_a_stable_coolify_alias_matching_its_allowlist(compose, service_name):
    """The alias LiteLLM resolves must be an entry in MCP_HTTP_ALLOWED_HOSTS.

    A mismatch here is a deployment that starts cleanly and then answers every
    gateway request with 421; asserting the two agree moves that failure from
    run time to test time.
    """
    service = compose["services"][service_name]
    aliases = service["networks"]["coolify"].get("aliases")
    assert aliases == [service_name], aliases

    allowlist = service["environment"]["MCP_HTTP_ALLOWED_HOSTS"]
    entries = [entry.strip() for entry in allowlist.split(",")]
    for alias in aliases:
        assert alias in entries, f"alias {alias!r} is not in the Host allowlist {entries!r}"


def test_each_service_mounts_its_own_token_secret(compose):
    play = compose["services"]["play-console-mcp"]
    search = compose["services"]["search-console-mcp"]
    assert play["environment"]["MCP_HTTP_AUTH_TOKEN_FILE"] == "/run/secrets/play_mcp_token"
    assert search["environment"]["MCP_HTTP_AUTH_TOKEN_FILE"] == "/run/secrets/gsc_mcp_token"
    assert "play_mcp_token" in play["secrets"]
    assert "gsc_mcp_token" in search["secrets"]
    # The two secrets must be distinct file references.
    assert compose["secrets"]["play_mcp_token"]["file"] != compose["secrets"]["gsc_mcp_token"]["file"]


def test_google_credentials_are_separate_per_service(compose):
    play = compose["services"]["play-console-mcp"]["environment"]
    search = compose["services"]["search-console-mcp"]["environment"]
    assert play["PLAY_CONSOLE_CREDENTIALS_FILE"] == "/run/secrets/play_service_account.json"
    assert search["GOOGLE_APPLICATION_CREDENTIALS"] == "/run/secrets/gsc_service_account.json"
    # Neither service may see the other's credential path.
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in play
    assert "PLAY_CONSOLE_CREDENTIALS_FILE" not in search


@pytest.mark.parametrize(
    "secret_name",
    ["play_mcp_token", "gsc_mcp_token", "play_service_account.json", "gsc_service_account.json"],
)
def test_file_secrets_declare_no_uid_gid_or_mode(compose, secret_name):
    """A ``file:`` secret is a bind mount; ``uid``/``gid``/``mode`` do not apply.

    Compose implements a file-sourced secret with a bind mount, and bind mounts
    do not remap ownership or permissions: the container sees the *host* file's
    uid/gid and mode. Setting ``uid``/``gid``/``mode`` here would therefore be a
    promise the runtime silently ignores — the earlier "chmod is handled by
    compose" assumption was false. The real requirement is on the operator: the
    host file must be readable by the container user (uid/gid 10001), which is
    documented in the compose header and docs/security.md.

    Asserting the keys are absent keeps the file honest: if someone adds them
    expecting them to take effect, this test fails and points at the reason.
    """
    secret = compose["secrets"][secret_name]
    assert set(secret) == {"file"}, secret
    # The path must be a *required* variable (`${VAR:?...}`): a missing secret
    # path should abort the deploy, not silently point at a wrong file. Inspect
    # the top-level `secrets:` block of the raw source, since the parsed view
    # has placeholders substituted.
    raw = COMPOSE.read_text(encoding="utf-8")
    block = raw.split("\nsecrets:", 1)[1]
    for name in compose["secrets"]:
        line = block.split(f"\n  {name}:", 1)[1].split("\n", 1)[1].strip()
        assert line.startswith("file:"), line
        assert ":?" in line, f"{name} path must be a required variable: {line}"


def test_compose_documents_the_secret_permission_requirement():
    """The header must state the bind-mount permission rule, not hide it.

    The prior version of this file implied secret permissions were compose's
    business. They are not; an operator who creates a root:0600 file gets a
    container that exits at startup. The requirement must be written down where
    an operator reads it (the compose file itself).
    """
    header = COMPOSE.read_text(encoding="utf-8")
    head = header.split("services:", 1)[0]
    assert "bind mount" in head
    assert "10001" in head
    assert "0600" in head or "chmod 0400" in head


def test_no_token_is_passed_as_a_plain_environment_value(compose):
    """Tokens must arrive as mounted files, never as inline env values."""
    for service in compose["services"].values():
        assert "MCP_HTTP_AUTH_TOKEN" not in service["environment"]
        assert "MCP_HTTP_AUTH_TOKEN_FILE" in service["environment"]


@pytest.mark.parametrize("service_name", ["play-console-mcp", "search-console-mcp"])
def test_hardening_defaults_are_present(compose, service_name):
    service = compose["services"][service_name]
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["healthcheck"]["test"][0] == "CMD"


@pytest.mark.parametrize("service_name", ["play-console-mcp", "search-console-mcp"])
def test_host_allowlist_is_explicit_and_has_no_wildcard(compose, service_name):
    hosts = compose["services"][service_name]["environment"]["MCP_HTTP_ALLOWED_HOSTS"]
    entries = [entry.strip() for entry in hosts.split(",")]
    assert entries, hosts
    assert "*" not in entries
    assert "127.0.0.1" in entries


def test_allowlists_are_required_variables_not_defaults(compose):
    """A missing allowlist must abort the deploy, not fall back to permissive."""
    play_env = COMPOSE.read_text(encoding="utf-8")
    assert "${PLAY_CONSOLE_ALLOWED_PACKAGES:?" in play_env
    assert "${GSC_ALLOWED_SITES:?" in play_env


# --- Dockerfile checks -------------------------------------------------------


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_dockerfile_is_non_root(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    assert re.search(r"^USER 10001:10001$", text, re.M)
    assert "useradd --uid 10001" in text


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_dockerfile_installs_only_one_server_package(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    copied = [line for line in text.splitlines() if line.startswith("COPY servers/")]
    assert len(copied) == 1, copied
    other = "search-console" if "play-console" in dockerfile.name else "play-console"
    assert other not in text, "one image must not contain the other server's code"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_dockerfile_has_no_secret_and_no_credential_copy(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    for forbidden in ("PLAY_CONSOLE_CREDENTIALS_FILE=", "GOOGLE_APPLICATION_CREDENTIALS=", "MCP_HTTP_AUTH_TOKEN="):
        assert forbidden not in text
    # No JSON/credential file may be COPYed into the image.
    assert not re.search(r"COPY .*\.json", text)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda path: path.name)
def test_base_image_and_constraints_are_pinned(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.11\.\d+-slim-bookworm", text, re.M), "base image must be an exact tag"
    assert "-r /build/constraints.txt" in text


def test_constraints_pin_exact_versions_only():
    text = (REPO / "deploy" / "constraints.txt").read_text(encoding="utf-8")
    requirements = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert requirements
    for line in requirements:
        # Either "name==version" or "name==version ; marker".
        head = line.split(";")[0].strip()
        assert "==" in head, f"unpinned requirement: {line}"
        assert ">=" not in head and "<" not in head, f"range requirement: {line}"


def test_repository_contains_no_google_identifiers_or_credentials():
    """No guessed project id, client id, or credential material anywhere."""
    suspicious = re.compile(
        r"(ya29\.[A-Za-z0-9_\-]{10,})"            # access token
        r"|(\d{12}-[a-z0-9]{16,}\.apps\.googleusercontent\.com)"  # OAuth client id
        r"|(-----BEGIN [A-Z ]*PRIVATE KEY-----)"   # private key material
        r"|(\"private_key\")"                      # service-account field
        r"|(client_email\")",
    )
    # Scope: everything authored for this platform. The vendored server trees
    # are byte-identical copies of the original repositories (asserted in
    # test_isolation.py) and contain deliberate fake-token fixtures; scanning
    # them would flag upstream test data, not anything this work introduced.
    authored = [
        REPO / "README.md",
        REPO / "Makefile",
        REPO / "pyproject.toml",
        REPO / "packages",
        REPO / "deploy",
        REPO / "docs",
        REPO / "tests",
        REPO / "scripts",
        REPO / "servers" / "play-console" / "src" / "play_console_mcp" / "http_main.py",
        REPO / "servers" / "search-console" / "src" / "search_console_mcp" / "http_main.py",
    ]
    allowed_suffixes = {".py", ".md", ".toml", ".yml", ".txt", ".ini", ".cfg", ".yaml", ".sh", ""}
    offenders = []
    candidates = []
    for entry in authored:
        if entry.is_dir():
            candidates.extend(path for path in entry.rglob("*") if path.is_file())
        elif entry.is_file():
            candidates.append(entry)
    for path in candidates:
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        if path.suffix not in allowed_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file
            continue
        for match in suspicious.finditer(text):
            offenders.append(f"{path.relative_to(REPO)}: {match.group(0)[:40]}")
    assert offenders == [], offenders


def test_test_token_is_not_a_credential_and_is_marked_as_such():
    harness = (REPO / "tests" / "harness.py").read_text(encoding="utf-8")
    assert "TEST_TOKEN" in harness
    assert "not a secret" in harness or "not a real secret" in harness


def _git_ignores(relative: str) -> bool:
    """True if ``.gitignore`` would ignore ``relative`` (works pre-commit too)."""
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", relative],
        cwd=REPO,
        capture_output=True,
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "relative",
    [
        "deploy/play.token",
        "secrets/play_token",
        "service_account.json",
        "deploy/gsc-service_account.json",
        "gsc-credentials.json",
        "client_secret_123.json",
        "application_default_credentials.json",
        "deploy/key.pem",
        "deploy/signing.key",
        ".env",
        "deploy/.env",
        "deploy/docker-compose.override.yml",
        ".gcloud/config",
    ],
)
def test_gitignore_covers_credential_shaped_paths(relative):
    """A credential dropped anywhere in the tree must be ignored by git.

    The compose file tells operators to keep secrets outside the repository,
    but the repository must also make it hard to commit one *by accident*. This
    checks the patterns actually match the names secrets come in, using git's
    own matcher so the test cannot drift from git's behaviour.
    """
    assert _git_ignores(relative), f"{relative} is not covered by .gitignore"


@pytest.mark.parametrize(
    "relative",
    [
        "servers/search-console/requirements.txt",
        "servers/play-console/src/play_console_mcp/server.py",
        ".gitignore",
    ],
)
def test_gitignore_does_not_hide_real_source(relative):
    """The credential rules must not swallow tracked source files."""
    assert not _git_ignores(relative), f"{relative} must remain tracked"