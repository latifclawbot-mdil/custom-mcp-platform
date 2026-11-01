"""Tests that the package cannot issue a mutating request.

Two independent guards:
* static -- no mutating verb appears anywhere in the shipped source;
* dynamic -- the client refuses every non-GET verb before touching the network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import respx

from play_console_mcp.errors import LocalError
from play_console_mcp.google_client import GoogleApiClient

SRC = Path(__file__).resolve().parents[1] / "src" / "play_console_mcp"

# Mutating verbs as they would appear in an API call, excluding HTTP-status-ish
# uses and the words deliberately present in refusal messages/docstrings.
_FORBIDDEN_CALLS = [
    r"\.post\(",
    r"\.put\(",
    r"\.patch\(",
    r"\.delete\(",
    r"\.request\(\s*[\"']POST",
    r"\.request\(\s*[\"']PUT",
    r"\.request\(\s*[\"']PATCH",
    r"\.request\(\s*[\"']DELETE",
    r"edits\(\)\.insert",
    r"\.publish\(",
    r"\.commit\(",
    r"\.upload\(",
]


def _sources():
    return sorted(SRC.glob("*.py"))


def test_source_tree_is_not_empty():
    assert len(_sources()) >= 7


@pytest.mark.parametrize("pattern", _FORBIDDEN_CALLS)
def test_no_mutating_call_in_source(pattern):
    offenders = [
        f"{path.name}" for path in _sources()
        if re.search(pattern, path.read_text())
    ]
    assert offenders == [], f"{pattern} found in {offenders}"


def test_fake_transport_is_test_only_and_get_only():
    """The fake upstream lives in tests/ and can never ship in the package."""
    assert not (SRC / "fake_transport.py").exists()
    fake = Path(__file__).resolve().parent / "fake_transport.py"
    assert fake.is_file()
    text = fake.read_text()
    assert "def do_" not in text
    assert ".post(" not in text


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
                                  "post", "delete"])
def test_client_refuses_verb_before_network(settings, verb):
    client = GoogleApiClient(settings=settings, credentials=_Creds())
    with respx.mock(base_url="https://androidpublisher.googleapis.com",
                    assert_all_called=False) as router:
        router.route().mock(return_value=__import__("httpx").Response(200, json={}))
        with pytest.raises(LocalError, match="read-only"):
            client.request(verb, "/androidpublisher/v3/applications/{packageName}/reviews",
                           params={"packageName": "com.example.app"})
        assert len(router.calls) == 0


class _Creds:
    token = "t"
    valid = True
    expired = False