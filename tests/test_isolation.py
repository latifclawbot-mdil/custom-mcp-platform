"""Cross-server isolation over real HTTP, plus hermetic vendored-source integrity.

Isolation is asserted by running **both** servers at once, each with its own
token, and proving four things on the wire:

* server A's token is rejected by server B (401) and vice versa;
* a session opened on A cannot be used on B;
* the tool sets are disjoint (no server exposes the other's tools);
* the server name in ``initialize`` identifies one server, not a shared facade.

Integrity of the vendored trees is checked against ``docs/integrity-manifest.json``
— a committed ``path -> sha256`` map. That check is **hermetic**: it reads only
files inside this repository, so it runs identically in a clean ``git clone`` on
a machine that has never seen the upstream repositories, and proves nothing
about (or dependent on) any other checkout on the host.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from harness import (
    REPO_ROOT,
    SERVERS,
    TRANSPORT_SRC,
    free_port,
    initialize_body,
    server_env,
    wait_for_port,
)

pytestmark = pytest.mark.anyio

PLAY_TOKEN = "play-isolation-token-0123456789abcdef"
GSC_TOKEN = "gsc-isolation-token-0123456789abcdef"

PLAY = {
    "name": "play-console-mcp",
    "module": "play_console_mcp.http_main",
    "token": PLAY_TOKEN,
    "env": {
        "PLAY_CONSOLE_ALLOWED_PACKAGES": "com.example.app",
        "PYTHONPATH": f"{SERVERS / 'play-console' / 'src'}:{TRANSPORT_SRC}",
    },
}

GSC = {
    "name": "search-console-mcp",
    "module": "search_console_mcp.http_main",
    "token": GSC_TOKEN,
    "env": {
        "GSC_ALLOWED_SITES": "sc-domain:example.com",
        "PYTHONPATH": f"{SERVERS / 'search-console' / 'src'}:{TRANSPORT_SRC}",
    },
}


@contextlib.contextmanager
def start(spec, tmp_path):
    port = free_port()
    log = Path(tmp_path) / f"{spec['name']}.log"
    env = server_env(
        {
            "MCP_HTTP_AUTH_TOKEN": spec["token"],
            "MCP_HTTP_PORT": str(port),
            "MCP_HTTP_HOST": "127.0.0.1",
            **spec["env"],
        }
    )
    with open(log, "wb") as handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", spec["module"]],
            env=env,
            cwd=str(REPO_ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    try:
        try:
            with wait_for_port(port):
                pass
        except TimeoutError:
            detail = log.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(
                f"{spec['name']} did not start (exit {proc.poll()}):\n{detail}"
            ) from None
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()


def post(url: str, body: dict, token: str, session_id: str | None = None):
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    return httpx.post(url, json=body, headers=headers, timeout=30.0)


def payload(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no SSE data: {response.text!r}")
    return response.json()


async def test_each_server_rejects_the_others_token(tmp_path):
    with start(PLAY, tmp_path) as play_url, start(GSC, tmp_path) as gsc_url:
        assert post(play_url, initialize_body(), PLAY_TOKEN).status_code == 200
        assert post(gsc_url, initialize_body(), GSC_TOKEN).status_code == 200

        cross_play = post(play_url, initialize_body(), GSC_TOKEN)
        cross_gsc = post(gsc_url, initialize_body(), PLAY_TOKEN)
        assert cross_play.status_code == 401, cross_play.text
        assert cross_gsc.status_code == 401, cross_gsc.text
        assert cross_play.headers.get("mcp-session-id") is None
        assert cross_gsc.headers.get("mcp-session-id") is None


async def test_a_session_from_one_server_is_not_usable_on_the_other(tmp_path):
    with start(PLAY, tmp_path) as play_url, start(GSC, tmp_path) as gsc_url:
        opened = post(play_url, initialize_body(), PLAY_TOKEN)
        assert opened.status_code == 200
        session_id = opened.headers["mcp-session-id"]

        # Same session id, other server, its own correct token: must not resume
        # a session that never existed there.
        foreign = post(
            gsc_url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            GSC_TOKEN,
            session_id=session_id,
        )
        assert foreign.status_code in (400, 404), (foreign.status_code, foreign.text)
        assert "result" not in foreign.text


async def test_tool_sets_are_disjoint_and_identify_the_right_server(tmp_path):
    with start(PLAY, tmp_path) as play_url, start(GSC, tmp_path) as gsc_url:
        play_init = post(play_url, initialize_body(), PLAY_TOKEN)
        gsc_init = post(gsc_url, initialize_body(), GSC_TOKEN)
        play_name = payload(play_init)["result"]["serverInfo"]["name"]
        gsc_name = payload(gsc_init)["result"]["serverInfo"]["name"]
        assert play_name != gsc_name

        play_session = play_init.headers["mcp-session-id"]
        gsc_session = gsc_init.headers["mcp-session-id"]
        play_tools = {
            tool["name"]
            for tool in payload(
                post(play_url, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}, PLAY_TOKEN, play_session)
            )["result"]["tools"]
        }
        gsc_tools = {
            tool["name"]
            for tool in payload(
                post(gsc_url, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}, GSC_TOKEN, gsc_session)
            )["result"]["tools"]
        }
        assert play_tools and gsc_tools
        assert play_tools.isdisjoint(gsc_tools), play_tools & gsc_tools
        assert all(name.startswith("gsc_") for name in gsc_tools)
        assert not any(name.startswith("gsc_") for name in play_tools)


# --- hermetic vendoring integrity --------------------------------------------
#
# The expected content of the vendored trees is frozen in a committed manifest
# (docs/integrity-manifest.json). Verification therefore reads only files inside
# this repository: it needs no upstream checkout, no absolute path off this
# machine, and no git remote. That is what makes it reproducible in a clean
# ``git clone``.
#
# Regenerate after an intentional re-vendor with
# ``python scripts/gen_integrity_manifest.py`` (then re-run the tests).

MANIFEST_PATH = REPO_ROOT / "docs" / "integrity-manifest.json"

#: Directories that hold byproducts, never vendored content.
_IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git", ".venv"}


def _load_manifest() -> dict:
    assert MANIFEST_PATH.is_file(), (
        f"missing {MANIFEST_PATH.relative_to(REPO_ROOT)}; regenerate it with "
        "scripts/gen_integrity_manifest.py"
    )
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert data.get("servers"), "manifest has no server entries"
    return data


def _hashed_tree(server: str, platform_added: set[str]) -> dict[str, str]:
    """Hash the vendored tree, excluding caches and platform-added files."""
    root = SERVERS / server
    assert root.is_dir(), f"vendored tree missing: {root}"
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in _IGNORED_PARTS for part in path.parts):
            continue
        if relative.endswith(".pyc") or "egg-info" in relative:
            continue
        if relative in platform_added:
            continue
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def test_manifest_covers_both_vendored_servers():
    manifest = _load_manifest()
    assert set(manifest["servers"]) == {"play-console", "search-console"}
    for server, block in manifest["servers"].items():
        assert block["file_count"] == len(block["files"]), server
        assert block["file_count"] > 0, server


@pytest.mark.parametrize("server", ["play-console", "search-console"])
def test_vendored_files_match_the_committed_integrity_manifest(server):
    """Every non-added vendored file hashes to the committed digest.

    This is the hermetic replacement for comparing against the upstream
    checkout: the expected hashes live in this repository, so the test proves
    the shipped tree matches what was reviewed, with no external dependency.
    """
    block = _load_manifest()["servers"][server]
    platform_added = set(block["platform_added_files"])
    assert platform_added, f"{server}: the platform-added file list must be recorded"
    actual = _hashed_tree(server, platform_added)

    missing = sorted(set(block["files"]) - set(actual))
    unexpected = sorted(set(actual) - set(block["files"]))
    assert not missing, f"{server}: vendored files missing from the tree: {missing}"
    assert not unexpected, (
        f"{server}: files present but absent from the manifest (re-vendor and "
        f"regenerate): {unexpected}"
    )

    mismatched = [
        relative
        for relative, digest in block["files"].items()
        if actual[relative] != digest
    ]
    assert not mismatched, f"{server}: content differs from the manifest: {mismatched}"


@pytest.mark.parametrize("server", ["play-console", "search-console"])
def test_platform_added_files_are_the_only_extra_source(server):
    """The one intentional addition per server is present as a real source file."""
    block = _load_manifest()["servers"][server]
    for relative in block["platform_added_files"]:
        added = SERVERS / server / relative
        assert added.is_file(), f"missing platform-added file: {server}/{relative}"
        assert added.read_text(encoding="utf-8").strip(), f"empty file: {server}/{relative}"


@pytest.mark.parametrize("server", ["play-console", "search-console"])
def test_no_vcs_or_virtualenv_state_was_copied(server):
    root = SERVERS / server
    assert not (root / ".git").exists(), "the .git directory must not be copied"
    assert not (root / ".venv").exists(), "the virtualenv must not be copied"
    assert not (root / ".pytest_cache").exists(), "test cache must not be copied"


def test_integrity_check_is_hermetic():
    """The manifest and the check reference nothing outside this repository.

    Guards against a regression back to an absolute host path under the home
    directory or a dependency on a sibling checkout, either of which would make
    the suite pass only on the machine where it was written.
    """
    text = (REPO_ROOT / "tests" / "test_isolation.py").read_text(encoding="utf-8")
    # Built from fragments so the needles do not appear literally in this file.
    needles = (
        "/" + "home/",
        "google-operations-" + "mcp",
    )
    for needle in needles:
        assert needle not in text, (
            f"isolation test references {needle!r}; the integrity check must "
            "stay inside the repository"
        )
    # The manifest is JSON inside the repo; a reader needs no network or peer.
    manifest = _load_manifest()
    for block in manifest["servers"].values():
        assert "upstream_head" in block and len(block["upstream_head"]) == 40
        for relative in block["files"]:
            assert not relative.startswith("/"), relative


def test_repository_declares_no_submodules_or_symlinked_vendored_source():
    """Dependencies are copied files, never submodules or symlinks out of tree.

    No claim is made about git remotes: this repository may legitimately have
    one (it is a publishable project). What must hold is that the vendored
    source is real copied content, not a pointer to another repository.
    """
    assert not (REPO_ROOT / ".gitmodules").exists(), "no git submodules"
    for path in REPO_ROOT.rglob("*"):
        if _IGNORED_PARTS.intersection(path.parts):
            continue
        assert not path.is_symlink(), f"symlink in repository: {path.relative_to(REPO_ROOT)}"