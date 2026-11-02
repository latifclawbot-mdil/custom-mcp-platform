"""Policy tests: the capability boundary is enforced by data, not by promises.

These tests read the real discovery-document snapshot under ``tests/fixtures``
and cross-check the server's allowlist against it. That is what makes claims
like "no financial method is reachable" checkable rather than rhetorical.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from play_console_writer import policy
from play_console_writer.client import METHOD_SPECS
from play_console_writer.operations import TOOL_SPECS

FIXTURE = Path(__file__).parent / "fixtures" / "androidpublisher_v3_methods.json"

DISCOVERY = json.loads(FIXTURE.read_text(encoding="utf-8"))
METHODS = DISCOVERY["methods"]


def test_discovery_fixture_is_the_real_document_summary():
    assert DISCOVERY["name"] == "androidpublisher"
    assert DISCOVERY["version"] == "v3"
    assert DISCOVERY["method_count"] == 145
    assert len(METHODS) == 145


def test_every_allowlisted_method_exists_in_the_discovery_document():
    missing = sorted(m for m in policy.ALLOWED_WRITE_METHODS if m not in METHODS)
    assert missing == []


@pytest.mark.parametrize("method_id", sorted(policy.ALLOWED_WRITE_METHODS))
def test_allowlisted_method_uses_the_published_verb(method_id):
    assert METHOD_SPECS[method_id].verb == METHODS[method_id]["verb"]


@pytest.mark.parametrize("method_id", sorted(policy.ALLOWED_WRITE_METHODS))
def test_allowlisted_method_uses_the_published_path(method_id):
    """The spec template must render to the exact published path."""
    published = METHODS[method_id]["path"]
    assert published.startswith("/androidpublisher/v3/")
    relative = published[len("/androidpublisher/v3/"):]
    # Compare placeholder-by-placeholder: names and order must match exactly.
    assert _placeholders(relative) == _placeholders(METHOD_SPECS[method_id].path_template)
    assert _literal_shape(relative) == _literal_shape(METHOD_SPECS[method_id].path_template)


def _placeholders(template: str) -> list[str]:
    import re

    return re.findall(r"\{(\+?)([A-Za-z_][A-Za-z0-9_]*)\}", template)


def _literal_shape(template: str) -> str:
    import re

    return re.sub(r"\{(\+?)([A-Za-z_][A-Za-z0-9_]*)\}", "{}", template)


def test_no_financial_method_is_allowlisted():
    overlap = policy.ALLOWED_WRITE_METHODS & policy.FINANCIAL_METHODS
    assert overlap == frozenset()


@pytest.mark.parametrize("method_id", sorted(policy.FINANCIAL_METHODS))
def test_financial_methods_exist_and_are_not_implemented(method_id):
    assert method_id in METHODS, f"{method_id} is not a real method id"
    assert method_id not in METHOD_SPECS, f"{method_id} must have no spec"
    assert method_id not in policy.ALLOWED_WRITE_METHODS


def test_every_write_verb_in_the_api_is_classified_exactly_once():
    """No write method is unclassified: each is allowed, financial, or excluded."""
    write_verbs = {"POST", "PUT", "PATCH", "DELETE"}
    unclassified = []
    for method_id, meta in METHODS.items():
        if meta["verb"] not in write_verbs:
            continue
        buckets = sum([
            method_id in policy.ALLOWED_WRITE_METHODS,
            method_id in policy.FINANCIAL_METHODS,
            method_id in policy.EXCLUDED_METHODS,
        ])
        if buckets != 1:
            unclassified.append((method_id, buckets))
    assert unclassified == [], f"methods not in exactly one bucket: {unclassified}"


def test_total_write_methods_match_the_document():
    write_verbs = {"POST", "PUT", "PATCH", "DELETE"}
    total = sum(1 for m in METHODS.values() if m["verb"] in write_verbs)
    assert total == 100
    assert (len(policy.ALLOWED_WRITE_METHODS) + len(policy.FINANCIAL_METHODS)
            + len(policy.EXCLUDED_METHODS)) == total


def test_every_tool_maps_to_an_allowlisted_method():
    for tool_name, spec in TOOL_SPECS.items():
        assert spec["method_id"] in policy.ALLOWED_WRITE_METHODS, tool_name


def test_every_allowlisted_method_has_a_tool():
    """No method is allowlisted without an explicit, named tool."""
    covered = {spec["method_id"] for spec in TOOL_SPECS.values()}
    assert covered == set(policy.ALLOWED_WRITE_METHODS)


def test_excluded_methods_are_real_ids_and_documented():
    for method_id in policy.EXCLUDED_METHODS:
        assert method_id in METHODS, f"{method_id} is not a real method id"
        assert policy.exclusion_reason(method_id) != (
            "Excluded from this server's non-financial scope."
        ), f"{method_id} has no specific reason"


def test_read_back_methods_are_all_get():
    for method_id in policy.READ_BACK_METHODS:
        assert METHODS[method_id]["verb"] == "GET", method_id
        assert METHOD_SPECS[method_id].kind == "read"


def test_no_write_spec_is_marked_read():
    for method_id, spec in METHOD_SPECS.items():
        if spec.kind == "read":
            assert METHODS[method_id]["verb"] == "GET"
        else:
            assert method_id in policy.ALLOWED_WRITE_METHODS


def test_financial_keywords_absent_from_every_tool_name():
    financial = ("price", "refund", "purchase", "billing", "monetiz", "grant",
                 "user", "order", "tax", "currency", "payment")
    for tool_name in TOOL_SPECS:
        assert not any(word in tool_name for word in financial), tool_name
