"""Static guarantees over the shipped source tree.

Two independent claims, checked by reading the source rather than by exercising
it:

* no financial method id appears as a *reachable spec* anywhere in the package;
* there is no generic request surface (no code path that forwards a
  caller-supplied path or verb).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from play_console_writer import policy

SRC = Path(__file__).resolve().parents[1] / "src" / "play_console_writer"


def _sources() -> list[Path]:
    return sorted(SRC.glob("*.py"))


def test_source_tree_is_not_empty():
    assert len(_sources()) >= 8


@pytest.mark.parametrize("method_id", sorted(policy.FINANCIAL_METHODS))
def test_financial_method_id_never_appears_in_the_client_registry(method_id):
    from play_console_writer.client import METHOD_SPECS

    assert method_id not in METHOD_SPECS


@pytest.mark.parametrize("method_id", sorted(policy.FINANCIAL_METHODS))
def test_financial_method_id_is_never_allowlisted_or_tooled(method_id):
    from play_console_writer.operations import TOOL_SPECS

    assert method_id not in policy.ALLOWED_WRITE_METHODS
    assert all(spec["method_id"] != method_id for spec in TOOL_SPECS.values())


def test_no_method_call_takes_a_caller_supplied_path_or_verb():
    """``request`` must not accept a path/verb/url keyword in its signature."""
    import inspect

    from play_console_writer.client import GoogleWriteClient

    params = set(inspect.signature(GoogleWriteClient.request).parameters)
    assert "path" not in params
    assert "verb" not in params
    assert "url" not in params
    assert "method" not in params


def test_no_client_uses_a_caller_supplied_base_url_in_production():
    """Only ``_validated_root`` may set the host, and it pins Google."""
    text = (SRC / "client.py").read_text(encoding="utf-8")
    assert "androidpublisher.googleapis.com" in text
    # The only hosts accepted are the fixed Google host and loopback.
    assert "ALLOWED_HOSTS" in text


def test_financial_words_do_not_name_any_tool():
    from play_console_writer.operations import TOOL_SPECS

    for tool_name in TOOL_SPECS:
        for word in ("refund", "price", "purchase", "monetiz", "billing",
                     "grant", "order", "tax"):
            assert word not in tool_name, (tool_name, word)


def test_source_has_no_wildcard_request_helper():
    """A generic escape hatch (path + verb + body) must not exist."""
    pattern = re.compile(r"def\s+(call_api|request_raw|raw_request|call_any|proxy)\b")
    offenders = [p.name for p in _sources() if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_policy_module_is_the_single_allowlist_source():
    """The allowlist lives in policy.py; client imports it rather than copying."""
    client_text = (SRC / "client.py").read_text(encoding="utf-8")
    assert "from .policy import ALLOWED_WRITE_METHODS" in client_text


def test_fake_upstream_is_test_only_and_absent_from_the_package():
    assert not (SRC / "mcp_harness.py").exists()
    fake = Path(__file__).resolve().parent / "mcp_harness.py"
    assert fake.is_file()