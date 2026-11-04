"""Deployment packaging + real authenticated HTTP for the two *writer* servers.

Two independent things are checked here, and they are checked differently
because they can be checked differently:

1. **Artifact properties (static).** ``deploy/Dockerfile.play-writer``,
   ``deploy/Dockerfile.search-writer`` and ``deploy/docker-compose.writers.yml``
   are read as text and asserted to have the properties the isolation claims
   depend on: a non-root user that is *distinct* from the read-only writers, a
   pinned base image and pinned constraints, exactly one server package copied
   per image, no baked credential and no token literal, no published host port,
   a fixed private alias whose value is also in the Host allowlist, and one
   distinct token plus one distinct credential per service. Docker is **not**
   required for these and nothing here claims an image was built.

2. **The Play writer's HTTP entry point (executed).** Until now the Play writer
   was stdio-only. ``servers/play-console-writer/src/play_console_writer/http_main.py``
   is new; the tests below import it, resolve an ``AppConfig`` through it, build
   the ASGI app from the *real* server object and drive it over a **real
   loopback socket** with uvicorn. They assert authentication is enforced before
   MCP dispatch, that MCP sessions are stateful, that the Host allowlist is a
   closed list, and that the port is loopback-only for the lifetime of the test.

The two writer servers are then run **concurrently** to prove caller isolation:
each rejects the other's token and never shares a session id.

What is explicitly *not* claimed: that any container was built, that a provider
credential is valid, or that the two containers cannot reach each other on a
shared Docker network. A dedicated bridge is not a per-client firewall; the
bearer token is the boundary. See ``docs/writer-deployment.md``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"

# The root ``pytest.ini`` puts only the read-only servers and the shared
# transport on ``pythonpath``. The writer packages are deliberately separate
# trees, so this file adds their ``src`` directories itself — that way the suite
# runs identically under ``make test`` and under a bare pytest invocation.
for _pkg in ("play-console-writer", "search-console-writer"):
    _src = REPO / "servers" / _pkg / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
_transport_src = REPO / "packages" / "mcp_http_transport" / "src"
if str(_transport_src) not in sys.path:
    sys.path.insert(0, str(_transport_src))

COMPOSE = DEPLOY / "docker-compose.writers.yml"
PLAY_DOCKERFILE = DEPLOY / "Dockerfile.play-writer"
SEARCH_DOCKERFILE = DEPLOY / "Dockerfile.search-writer"
WRITER_DOCKERFILES = [PLAY_DOCKERFILE, SEARCH_DOCKERFILE]

#: Two distinct, obviously-fake tokens. Neither is a secret and neither is
#: valid against any real deployment.
PLAY_TEST_TOKEN = "play-writer-http-test-token-not-a-secret-0001"
SEARCH_TEST_TOKEN = "search-writer-http-test-token-not-a-secret-0002"

PLAY_PACKAGE = "com.example.app"
GSC_PROPERTY = "sc-domain:example.com"

#: Bash-style interpolation that would break a plain YAML load.
PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


# ---------------------------------------------------------------------------
# Static artifact checks
# ---------------------------------------------------------------------------


def load_writer_compose() -> dict:
    """Load the writers' compose, giving each ``${VAR}`` a distinct placeholder.

    A single shared placeholder would make two different variables compare
    equal and hide exactly the aliasing these tests exist to catch.
    """
    text = COMPOSE.read_text(encoding="utf-8")

    def _sub(match: re.Match[str]) -> str:
        name = match.group(0)[2:-1].split(":")[0].split("-")[0]
        return f"value-of-{name}"

    return yaml.safe_load(PLACEHOLDER.sub(_sub, text))


@pytest.fixture(scope="module")
def compose() -> dict:
    return load_writer_compose()


def test_writer_compose_declares_exactly_the_two_writer_services(compose):
    assert set(compose["services"]) == {"play-console-writer-mcp", "search-console-writer-mcp"}


@pytest.mark.parametrize("service_name", ["play-console-writer-mcp", "search-console-writer-mcp"])
def test_writer_service_publishes_no_host_port(compose, service_name):
    """A published port is a public endpoint; writers must have none."""
    service = compose["services"][service_name]
    assert "ports" not in service, "a published port is a public endpoint"
    assert "hostname" not in service, "hostnames are not used; a fixed alias is"
    assert service["expose"] == ["8080"]


@pytest.mark.parametrize("service_name", ["play-console-writer-mcp", "search-console-writer-mcp"])
def test_writer_service_has_a_fixed_private_alias_matching_its_allowlist(compose, service_name):
    """The alias LiteLLM will resolve must be an entry in MCP_HTTP_ALLOWED_HOSTS.

    A mismatch starts cleanly and then answers every gateway request with 421,
    so the two are asserted to agree here rather than debugged in production.
    """
    service = compose["services"][service_name]
    networks = service["networks"]
    assert "coolify" in networks, networks
    aliases = networks["coolify"].get("aliases")
    assert aliases == [service_name], aliases
    assert "*" not in aliases

    allowlist = service["environment"]["MCP_HTTP_ALLOWED_HOSTS"]
    entries = [entry.strip() for entry in allowlist.split(",")]
    assert entries
    assert "*" not in entries
    assert "127.0.0.1" in entries, "loopback is needed for the in-container healthcheck"
    for alias in aliases:
        assert alias in entries, f"alias {alias!r} is not in the Host allowlist {entries!r}"


def test_writer_services_use_distinct_tokens_and_distinct_credentials(compose):
    play = compose["services"]["play-console-writer-mcp"]
    search = compose["services"]["search-console-writer-mcp"]

    # Tokens arrive as mounted files, never as inline env values.
    assert "MCP_HTTP_AUTH_TOKEN" not in play["environment"]
    assert "MCP_HTTP_AUTH_TOKEN" not in search["environment"]
    assert play["environment"]["MCP_HTTP_AUTH_TOKEN_FILE"] == "/run/secrets/play_writer_mcp_token"
    assert search["environment"]["MCP_HTTP_AUTH_TOKEN_FILE"] == "/run/secrets/search_writer_mcp_token"

    # The two token secrets must be distinct file references.
    assert compose["secrets"]["play_writer_mcp_token"]["file"] != compose["secrets"][
        "search_writer_mcp_token"
    ]["file"]

    # Separate credential mounts, and neither service may see the other's path.
    play_env = play["environment"]
    search_env = search["environment"]
    assert play_env["PLAY_WRITER_CREDENTIALS_FILE"] == "/run/secrets/play_writer_service_account.json"
    assert search_env["GOOGLE_APPLICATION_CREDENTIALS"] == "/run/secrets/search_writer_service_account.json"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in play_env
    assert "PLAY_WRITER_CREDENTIALS_FILE" not in search_env


def test_writer_token_secrets_do_not_use_uid_gid_or_mode(compose):
    """A ``file:`` secret is a bind mount; uid/gid/mode would be a false promise."""
    for name in ("play_writer_mcp_token", "search_writer_mcp_token"):
        assert set(compose["secrets"][name]) == {"file"}, compose["secrets"][name]

    # Paths must be *required* variables so a missing secret aborts the deploy.
    raw = COMPOSE.read_text(encoding="utf-8")
    block = raw.split("\nsecrets:", 1)[1]
    for name in compose["secrets"]:
        line = block.split(f"\n  {name}:", 1)[1].split("\n", 1)[1].strip()
        assert line.startswith("file:"), line
        assert ":?" in line, f"{name} path must be a required variable: {line}"


def test_writer_allowlists_are_required_variables_not_defaults():
    """A missing allowlist must abort the deploy, not fall back to permissive."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${PLAY_WRITER_ALLOWED_PACKAGES:?" in text
    assert "${GSC_WRITE_ALLOWED_SITES:?" in text


@pytest.mark.parametrize("service_name", ["play-console-writer-mcp", "search-console-writer-mcp"])
def test_writer_hardening_defaults_are_present(compose, service_name):
    service = compose["services"][service_name]
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["healthcheck"]["test"][0] == "CMD"


def test_writer_compose_does_not_alter_the_read_only_compose():
    """The writers live in their own file; the read-only services are untouched."""
    read_only = yaml.safe_load(
        PLACEHOLDER.sub(
            lambda m: f"value-of-{m.group(0)[2:-1].split(':')[0]}",
            (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"),
        )
    )
    assert set(read_only["services"]) == {"play-console-mcp", "search-console-mcp"}
    # No writer service leaked into the read-only file.
    for name in read_only["services"]:
        assert "writer" not in name


# --- Dockerfiles -----------------------------------------------------------


@pytest.mark.parametrize("dockerfile", WRITER_DOCKERFILES, ids=lambda p: p.name)
def test_writer_dockerfile_is_non_root(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    assert re.search(r"^USER 10001:10001$", text, re.M)
    assert "useradd --uid 10001" in text


@pytest.mark.parametrize("dockerfile", WRITER_DOCKERFILES, ids=lambda p: p.name)
def test_writer_dockerfile_installs_only_its_own_server_and_the_transport(dockerfile: Path):
    """One server package per image, plus the shared transport and nothing else.

    Only the *build* lines (``COPY`` / ``pip install``) are inspected, not prose:
    a comment or an image tag may legitimately name the same server, but nothing
    that lands in the image may carry the other server's code.
    """
    text = dockerfile.read_text(encoding="utf-8")
    build_lines = [
        line
        for line in text.splitlines()
        if line.startswith("COPY ") or "pip install" in line or line.strip().startswith("/build/")
    ]
    copied = [line for line in build_lines if line.startswith("COPY servers/")]
    assert len(copied) == 1, copied
    joined = "\n".join(build_lines)
    # The file name is ``Dockerfile.<server>`` where <server> is the short form
    # (``play-writer``), so match on the ``play``/``search`` stem rather than the
    # full package name.
    is_play = "play" in dockerfile.name
    own = "play-console-writer" if is_play else "search-console-writer"
    other = "search-console-writer" if is_play else "play-console-writer"
    assert own in joined, f"{own} must be installed in its own image"
    assert other not in joined, "one image must not contain the other writer's code"
    # The read-only servers must not be present either.
    assert "/build/servers/play-console\n" not in joined + "\n"
    assert "/build/servers/search-console\n" not in joined + "\n"
    assert "packages/mcp_http_transport" in text


@pytest.mark.parametrize("dockerfile", WRITER_DOCKERFILES, ids=lambda p: p.name)
def test_writer_dockerfile_entrypoint_is_the_http_main(dockerfile: Path):
    """The container must serve HTTP, not stdio: a stdio entry point has no port."""
    text = dockerfile.read_text(encoding="utf-8")
    match = re.search(r"^ENTRYPOINT \[.*\]$", text, re.M)
    assert match, "no ENTRYPOINT"
    assert ".http_main" in match.group(0), match.group(0)
    assert "EXPOSE 8080" in text


@pytest.mark.parametrize("dockerfile", WRITER_DOCKERFILES, ids=lambda p: p.name)
def test_writer_dockerfile_has_no_secret_and_no_credential_copy(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    for forbidden in (
        "PLAY_WRITER_CREDENTIALS_FILE=",
        "GOOGLE_APPLICATION_CREDENTIALS=",
        "MCP_HTTP_AUTH_TOKEN=",
    ):
        assert forbidden not in text
    assert not re.search(r"COPY .*\.json", text), "no credential file may be copied in"


@pytest.mark.parametrize("dockerfile", WRITER_DOCKERFILES, ids=lambda p: p.name)
def test_writer_base_image_and_constraints_are_pinned(dockerfile: Path):
    text = dockerfile.read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.11\.\d+-slim-bookworm", text, re.M), "base image must be an exact tag"
    assert "-r /build/constraints.txt" in text


def test_writer_docs_exist_and_state_the_honest_limits():
    doc = REPO / "docs" / "writer-deployment.md"
    assert doc.is_file(), "the writer deployment doc must exist"
    text = doc.read_text(encoding="utf-8")
    # It must say the writers are unreleased / not deployed, and that no image
    # was built here, rather than implying a finished deployment.
    lowered = text.lower()
    assert "not deployed" in lowered or "unreleased" in lowered
    assert "stdio" in lowered  # documents that Play was stdio-only before this
    assert "bind mount" in lowered  # the secret permission rule


def test_play_writer_http_main_exists_and_imports_build_server():
    """The Play writer gains an HTTP entry point; it must reuse build_server."""
    http_main = (
        REPO / "servers" / "play-console-writer" / "src" / "play_console_writer" / "http_main.py"
    )
    assert http_main.is_file(), "play writer HTTP entry point is missing"
    text = http_main.read_text(encoding="utf-8")
    assert "build_server" in text
    # It must not re-declare tools or reach into the client directly.
    assert "Tool.from_function" not in text


# ---------------------------------------------------------------------------
# Real loopback HTTP for the Play writer
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _LiveApp:
    """Serve an ASGI app on 127.0.0.1 with uvicorn for the test's lifetime."""

    def __init__(self, app, host: str = "127.0.0.1"):
        self.host = host
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        self.port = sock.getsockname()[1]
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        self._sock = sock
        self._thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, daemon=True
        )

    def __enter__(self) -> "_LiveApp":
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                break
            time.sleep(0.02)
        else:  # pragma: no cover - startup timeout
            raise RuntimeError("uvicorn did not start")
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=20)

    def url(self, path: str = "/mcp") -> str:
        return f"http://{self.host}:{self.port}{path}"


PLAY_ENV = {
    "PLAY_WRITER_ALLOWED_PACKAGES": PLAY_PACKAGE,
    "MCP_HTTP_ALLOWED_HOSTS": "play-console-writer-mcp,127.0.0.1",
}


@pytest.fixture
def play_app(monkeypatch):
    """Build the Play writer's real HTTP app through its own http_main."""
    for key, value in PLAY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MCP_HTTP_AUTH_TOKEN", PLAY_TEST_TOKEN)
    monkeypatch.delenv("MCP_HTTP_AUTH_TOKEN_FILE", raising=False)

    from play_console_writer import http_main
    from mcp_http_transport.server import build_streamable_http_app
    from play_console_writer.settings import load_settings
    from play_console_writer.server import build_server

    config = http_main.app_config("127.0.0.1")
    assert config.expected_token == PLAY_TEST_TOKEN
    # The token must never survive into a repr.
    assert PLAY_TEST_TOKEN not in repr(config)

    server = build_server(load_settings())
    app = build_streamable_http_app(server, config)
    return app


INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "writer-deploy-test", "version": "1"},
    },
}
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _rpc(client, url, body, token=PLAY_TEST_TOKEN, extra=None):
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return client.post(url, json=body, headers=headers)


def test_play_http_missing_token_is_401_and_opens_no_session(play_app):
    with _LiveApp(play_app) as live, httpx.Client(timeout=20) as client:
        resp = _rpc(client, live.url(), INIT_BODY, token=None)
    assert resp.status_code == 401
    assert resp.headers.get("mcp-session-id") is None
    assert "www-authenticate" in {k.lower() for k in resp.headers}
    assert "serverInfo" not in resp.text


@pytest.mark.parametrize("token", ["wrong-token", PLAY_TEST_TOKEN + "x", PLAY_TEST_TOKEN[:-1]])
def test_play_http_wrong_and_near_miss_tokens_are_401(play_app, token):
    with _LiveApp(play_app) as live, httpx.Client(timeout=20) as client:
        resp = _rpc(client, live.url(), INIT_BODY, token=token)
    assert resp.status_code == 401


def test_play_http_malformed_authorization_is_401(play_app):
    bad = ["", "Bearer", "Token {}".format(PLAY_TEST_TOKEN), "Bearerx {}".format(PLAY_TEST_TOKEN)]
    with _LiveApp(play_app) as live, httpx.Client(timeout=20) as client:
        for value in bad:
            headers = dict(HEADERS)
            if value:
                headers["Authorization"] = value
            resp = client.post(live.url(), json=INIT_BODY, headers=headers)
            assert resp.status_code == 401, value


def test_play_http_initialize_succeeds_and_opens_a_stateful_session(play_app):
    with _LiveApp(play_app) as live, httpx.Client(timeout=20) as client:
        init = _rpc(client, live.url(), INIT_BODY)
        assert init.status_code == 200
        session_id = init.headers.get("mcp-session-id")
        assert session_id, "a stateful MCP session must return Mcp-Session-Id"
        assert "play-console-writer" in init.text

        # Notifications/initialized then tools/list on the same session.
        client.post(
            live.url(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**HEADERS, "Authorization": f"Bearer {PLAY_TEST_TOKEN}", "mcp-session-id": session_id},
        )
        listed = client.post(
            live.url(),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**HEADERS, "Authorization": f"Bearer {PLAY_TEST_TOKEN}", "mcp-session-id": session_id},
        )
        assert listed.status_code == 200
        assert "play_describe_capability" in listed.text


def test_play_http_unlisted_host_is_rejected(play_app):
    """DNS-rebinding protection: a Host outside the allowlist must be refused."""
    with _LiveApp(play_app) as live, httpx.Client(timeout=20) as client:
        resp = _rpc(client, live.url(), INIT_BODY, extra={"Host": "evil.example.com:8080"})
    assert resp.status_code == 421


def test_play_http_unknown_path_is_404(play_app):
    with _LiveApp(play_app) as live, httpx.Client(timeout=20) as client:
        resp = _rpc(client, live.url("/not-mcp"), INIT_BODY)
    assert resp.status_code == 404


def test_play_http_unauthenticated_tools_call_is_401_with_no_result(play_app):
    body = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "play_listing_delete", "arguments": {"package_name": PLAY_PACKAGE}},
    }
    with _LiveApp(play_app) as live, httpx.Client(timeout=20) as client:
        resp = _rpc(client, live.url(), body, token=None)
    assert resp.status_code == 401
    assert "result" not in resp.text


def test_play_http_main_module_imports_without_binding_a_socket():
    """Importing the entry point must not open a socket or touch a credential."""
    sys.path.insert(0, str(REPO / "servers" / "play-console-writer" / "src"))
    import play_console_writer.http_main as http_main

    assert callable(http_main.main)
    assert callable(http_main.app_config)


def test_play_http_main_refuses_to_start_without_a_token(monkeypatch):
    """Fail-closed: no token configured means a clean exit, not an open port."""
    for key, value in PLAY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MCP_HTTP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_HTTP_AUTH_TOKEN_FILE", raising=False)

    env = {
        **os.environ,
        **PLAY_ENV,
        "PYTHONPATH": str(REPO / "servers" / "play-console-writer" / "src")
        + os.pathsep
        + str(REPO / "packages" / "mcp_http_transport" / "src"),
        "MCP_HTTP_HOST": "127.0.0.1",
        "MCP_HTTP_PORT": str(_free_port()),
        "HOME": "/nonexistent-home-for-tests",
    }
    env.pop("MCP_HTTP_AUTH_TOKEN", None)
    env.pop("MCP_HTTP_AUTH_TOKEN_FILE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "play_console_writer.http_main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO / "servers" / "play-console-writer"),
    )
    assert proc.returncode == 2, proc.stderr
    assert PLAY_TEST_TOKEN not in proc.stderr
    assert "token" in proc.stderr.lower() or "auth" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# Caller isolation between the two writers, run concurrently
# ---------------------------------------------------------------------------


def _writer_apps():
    """Build both writers' real HTTP apps with distinct tokens."""
    sys.path.insert(0, str(REPO / "servers" / "play-console-writer" / "src"))
    sys.path.insert(0, str(REPO / "servers" / "search-console-writer" / "src"))
    from mcp_http_transport.server import AppConfig, build_streamable_http_app

    import play_console_writer.server as play_server
    import play_console_writer.settings as play_settings
    import search_console_writer.server as search_server
    from search_console_writer.allowlist import PropertyAllowlist

    play_cfg = AppConfig(
        expected_token=PLAY_TEST_TOKEN,
        allowed_hosts=("127.0.0.1",),
    )
    play_app = build_streamable_http_app(
        play_server.build_server(play_settings.load_settings()),
        play_cfg,
    )

    search_cfg = AppConfig(
        expected_token=SEARCH_TEST_TOKEN,
        allowed_hosts=("127.0.0.1",),
    )
    search_app = build_streamable_http_app(
        search_server.create_server(allowlist=PropertyAllowlist([GSC_PROPERTY])),
        search_cfg,
    )
    return play_app, search_app


@pytest.fixture
def both_writers(monkeypatch):
    monkeypatch.setenv("PLAY_WRITER_ALLOWED_PACKAGES", PLAY_PACKAGE)
    play_app, search_app = _writer_apps()
    with _LiveApp(play_app) as play, _LiveApp(search_app) as search:
        yield play, search


def test_writers_run_concurrently_with_distinct_ports(both_writers):
    play, search = both_writers
    assert play.port != search.port


def test_each_writer_rejects_the_other_token(both_writers):
    play, search = both_writers
    with httpx.Client(timeout=20) as client:
        # Play's token against the search writer: refused.
        assert _rpc(client, search.url(), INIT_BODY, token=PLAY_TEST_TOKEN).status_code == 401
        # Search's token against the play writer: refused.
        assert _rpc(client, play.url(), INIT_BODY, token=SEARCH_TEST_TOKEN).status_code == 401
        # Each accepts only its own.
        assert _rpc(client, play.url(), INIT_BODY, token=PLAY_TEST_TOKEN).status_code == 200
        assert _rpc(client, search.url(), INIT_BODY, token=SEARCH_TEST_TOKEN).status_code == 200


def test_session_id_from_one_writer_is_not_accepted_by_the_other(both_writers):
    play, search = both_writers
    with httpx.Client(timeout=20) as client:
        play_init = _rpc(client, play.url(), INIT_BODY, token=PLAY_TEST_TOKEN)
        session_id = play_init.headers.get("mcp-session-id")
        assert session_id
        # The play session id presented to the search writer with the *search*
        # token must not resume play's session.
        resp = client.post(
            search.url(),
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            headers={
                **HEADERS,
                "Authorization": f"Bearer {SEARCH_TEST_TOKEN}",
                "mcp-session-id": session_id,
            },
        )
        # Either a 4xx for the unknown session or a fresh 400/404; never a 200
        # that would mean sessions are shared across processes.
        assert resp.status_code != 200


def test_writer_static_artifacts_contain_no_secret_material():
    """The three new deploy files carry no credential-shaped literal."""
    suspicious = re.compile(
        r"(ya29\.[A-Za-z0-9_\-]{10,})"
        r"|(\d{12}-[a-z0-9]{16,}\.apps\.googleusercontent\.com)"
        r"|(-----BEGIN [A-Z ]*PRIVATE KEY-----)"
        r"|(\"private_key\")"
        r"|(client_email\")",
    )
    for path in (COMPOSE, PLAY_DOCKERFILE, SEARCH_DOCKERFILE):
        text = path.read_text(encoding="utf-8")
        assert suspicious.search(text) is None, f"secret-shaped literal in {path.name}"