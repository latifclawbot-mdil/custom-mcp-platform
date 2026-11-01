"""Shared fixtures: fake Google transport + fake credentials.

No live Google calls are ever made. The fake HTTP layer records every request
so tests can assert on real request paths, methods and query parameters.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from httpx import Response

from play_console_mcp.google_client import GoogleApiClient
from play_console_mcp.settings import Settings

API_ROOT = "https://androidpublisher.googleapis.com"


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
        user_project=None,
    )


@pytest.fixture
def client(settings: Settings) -> GoogleApiClient:
    return GoogleApiClient(settings=settings, credentials=FakeToken())


@pytest.fixture
def mocked_api() -> respx.Router:
    with respx.mock(base_url=API_ROOT, assert_all_called=False) as router:
        yield router


def json_route(router: respx.Router, path: str, payload: Any, status: int = 200):
    return router.get(path).mock(return_value=Response(status, json=payload))


def error_route(router: respx.Router, path: str, status: int, body: Any):
    return router.get(path).mock(
        return_value=Response(status, json=body, headers={"content-type": "application/json"})
    )