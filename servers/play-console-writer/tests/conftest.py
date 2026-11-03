"""Shared fixtures: a fake Google HTTP transport + fake credentials.

No live Google call is ever made. The fake transport records every request so
tests can assert on real request paths, verbs and bodies, and it stubs the
non-financial endpoints this server uses.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from play_console_writer.client import GoogleWriteClient
from play_console_writer.edit_registry import EditRegistry
from play_console_writer.settings import Settings


class FakeToken:
    token = "fake-access-token"
    expiry = None
    valid = True
    expired = False
    quota_project_id = None
    scopes = ["https://www.googleapis.com/auth/androidpublisher"]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        packages=frozenset({"com.example.app"}),
        credentials_path=None,
        subject=None,
        request_timeout=10.0,
        max_page_size=50,
        max_reply_length=350,
        enable_artifact_upload=False,
        enable_release_gate=False,
        user_project=None,
    )


@pytest.fixture
def unlocked_settings() -> Settings:
    """Settings with both gates enabled, for exercising the gated operations."""
    return Settings(
        packages=frozenset({"com.example.app"}),
        credentials_path=None,
        subject=None,
        request_timeout=10.0,
        max_page_size=50,
        max_reply_length=350,
        enable_artifact_upload=True,
        enable_release_gate=True,
        user_project=None,
    )


@pytest.fixture
def transport() -> "FakeApiTransport":
    return FakeApiTransport()


@pytest.fixture
def client(settings: Settings, transport: "FakeApiTransport") -> GoogleWriteClient:
    return GoogleWriteClient(settings=settings, credentials=FakeToken(),
                             transport=transport, api_root="https://androidpublisher.example.test")


@pytest.fixture
def registry() -> EditRegistry:
    return EditRegistry()


# --------------------------------------------------------------------------
# Fake upstream
# --------------------------------------------------------------------------

class FakeApiTransport(httpx.BaseTransport):
    """Strict, recording transport that answers the writer's endpoints.

    GET/read endpoints always 200. Write endpoints 200 with a plausible body
    unless a per-(verb, path-substring) override is configured. Unknown paths
    404 so a test that builds a wrong path fails loudly.
    """

    def __init__(self) -> None:
        # (verb, path, query dict, json body or None)
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        #: path-substring -> (status, json payload) applied to every request
        self.overrides: dict[str, tuple[int, Any]] = {}
        #: path-substring -> (status, json payload) applied to GET reads only, so
        #: a test can fail the read-back while leaving the mutation successful.
        self.read_overrides: dict[str, tuple[int, Any]] = {}
        #: set to raise a transport error on the next Nth write
        self.raise_on_write: Exception | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        body: Any = None
        raw = request.content
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw
        self.requests.append((request.method, path, query, body))

        if self.raise_on_write is not None and request.method != "GET":
            exc = self.raise_on_write
            self.raise_on_write = None
            raise exc

        if request.method == "GET":
            for needle, (status, payload) in self.read_overrides.items():
                if needle in path:
                    return httpx.Response(status, json=payload)

        for needle, (status, payload) in self.overrides.items():
            if needle in path:
                return httpx.Response(status, json=payload)

        return httpx.Response(200, json=_route(request.method, path))

    # -- inspection helpers ------------------------------------------------

    def writes(self) -> list[tuple[str, str, dict[str, str], Any]]:
        return [r for r in self.requests if r[0] != "GET"]

    def last_write(self) -> tuple[str, str, dict[str, str], Any]:
        writes = self.writes()
        assert writes, "no write was recorded"
        return writes[-1]


def _route(verb: str, path: str) -> Any:
    if path.endswith("/edits") and verb == "POST":
        return {"id": "edit-1", "expiryTimeSeconds": "1800000000"}
    if path.endswith(":validate"):
        return {"id": "edit-1"}
    if path.endswith(":commit"):
        return {"id": "edit-1"}
    if "/details" in path:
        return {"contactEmail": "dev@example.com", "defaultLanguage": "en-US"}
    if "/listings" in path:
        return {"title": "Example", "language": "en-US",
                "shortDescription": "s", "fullDescription": "f"}
    if "/testers/" in path:
        return {"googleGroups": ["testers@example.com"]}
    if "/images" in path or "/phoneScreenshots" in path:
        return {"images": [{"id": "img-1", "sha1": "abc"}]}
    if "/bundles" in path or "/apks" in path:
        return {"versionCode": 42}
    if "/dataSafety" in path:
        return {}
    if "/deviceTierConfigs" in path:
        return {"deviceTierConfigId": "7"}
    if ":reply" in path:
        return {"result": {"replyText": "thanks"}}
    if "/tracks" in path:
        return {"track": "production", "releases": []}
    if path.endswith("/edits/edit-1"):
        return {"id": "edit-1", "expiryTimeSeconds": "1800000000"}
    return {}