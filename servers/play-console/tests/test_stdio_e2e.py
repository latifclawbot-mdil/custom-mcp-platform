"""End-to-end MCP stdio protocol over a real subprocess.

These spawn ``tests/_fake_stdio_server.py``, a test-only launcher that builds the
production server with the fake HTTP transport and fake credentials injected via
``build_server``'s explicit injection points. The shipped server itself has no
fake-transport path, so production can never return fabricated data.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
LAUNCHER = TESTS / "_fake_stdio_server.py"
sys.path.insert(0, str(REPO / "src"))

ALLOWED = "com.example.app"


def _env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(TESTS)]),
        "PLAY_CONSOLE_ALLOWED_PACKAGES": ALLOWED,
        "PLAY_CONSOLE_REQUEST_TIMEOUT": "5",
        "PLAY_CONSOLE_MAX_PAGE_SIZE": "25",
    }
    env.update(extra or {})
    return env


def _params(tmp_path: Path, extra=None) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(LAUNCHER)],
        env=_env(tmp_path, extra),
        cwd=str(REPO),
    )


async def _list_tools(extra_env=None, tmp_path=None):
    async with stdio_client(_params(tmp_path, extra_env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.list_tools()


async def _call(name, arguments, extra_env=None, tmp_path=None):
    async with stdio_client(_params(tmp_path, extra_env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)


async def test_stdio_initialize_and_list_tools(tmp_path):
    result = await _list_tools(tmp_path=tmp_path)
    names = {t.name for t in result.tools}
    assert {
        "list_reviews",
        "get_review",
        "list_device_tier_configs",
        "get_device_tier_config",
        "list_in_app_products",
        "get_in_app_product",
        "list_subscriptions",
        "get_subscription",
        "list_one_time_products",
        "get_one_time_product",
        "list_subscription_offers",
        "list_voided_purchases",
        "get_order",
        "list_app_recoveries",
        "get_subscription_purchase_v2",
        "get_product_purchase",
    } <= names


async def test_every_tool_is_annotated_read_only(tmp_path):
    result = await _list_tools(tmp_path=tmp_path)
    assert result.tools
    for tool in result.tools:
        ann = tool.annotations
        assert ann is not None, tool.name
        assert ann.read_only_hint is True, tool.name
        assert ann.destructive_hint is False, tool.name


async def test_list_reviews_end_to_end_hits_real_path(tmp_path):
    result = await _call("list_reviews",
                         {"package_name": ALLOWED, "max_results": 25},
                         tmp_path=tmp_path)
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["request"]["path"] == (
        "/androidpublisher/v3/applications/com.example.app/reviews"
    )
    assert payload["request"]["query"]["maxResults"] == "25"
    assert payload["data"]["reviews"][0]["reviewId"] == "rev-1"


async def test_unlisted_package_is_rejected_without_network(tmp_path):
    result = await _call("list_reviews", {"package_name": "com.evil.app"}, tmp_path=tmp_path)
    assert result.is_error is True
    text = result.content[0].text
    assert "allowlist" in text
    assert result.content[0].text.count("androidpublisher.googleapis.com") == 0


async def test_url_injection_package_is_rejected(tmp_path):
    result = await _call("list_reviews", {"package_name": "../../../admin"}, tmp_path=tmp_path)
    assert result.is_error is True
    assert "package_name" in result.content[0].text


async def test_upstream_403_is_redacted_in_result(tmp_path):
    result = await _call("list_reviews",
                         {"package_name": "com.example.denied"},
                         extra_env={"PLAY_CONSOLE_ALLOWED_PACKAGES": "com.example.denied"},
                         tmp_path=tmp_path)
    assert result.is_error is True
    text = result.content[0].text
    assert "ya29.SUPERSECRET" not in text
    assert "403" in text


async def test_page_size_is_bounded(tmp_path):
    result = await _call("list_reviews",
                         {"package_name": ALLOWED, "max_results": 9999},
                         tmp_path=tmp_path)
    payload = json.loads(result.content[0].text)
    assert payload["request"]["query"]["maxResults"] == "25"


async def test_invalid_page_size_rejected(tmp_path):
    result = await _call("list_reviews",
                         {"package_name": ALLOWED, "max_results": 0},
                         tmp_path=tmp_path)
    assert result.is_error is True


async def test_get_review_requires_review_id(tmp_path):
    result = await _call("get_review", {"package_name": ALLOWED, "review_id": ""},
                         tmp_path=tmp_path)
    assert result.is_error is True


async def test_server_refuses_to_start_without_allowlist(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(LAUNCHER)],
        env={"PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(TESTS)])},
        cwd=str(REPO),
    )
    with pytest.raises(Exception):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()


async def test_list_subscriptions_end_to_end(tmp_path):
    result = await _call("list_subscriptions", {"package_name": ALLOWED}, tmp_path=tmp_path)
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["request"]["path"] == (
        "/androidpublisher/v3/applications/com.example.app/subscriptions"
    )
    assert payload["request"]["query"]["pageSize"] == "25"


async def test_path_placeholders_become_real_segments(tmp_path):
    result = await _call(
        "get_subscription_purchase_v2",
        {"package_name": ALLOWED, "token": "tok-1"},
        tmp_path=tmp_path,
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    # The token segment is masked in the audit echo even though the caller
    # supplied it: purchase tokens are bearer-like and get logged downstream.
    assert payload["request"]["path"] == (
        "/androidpublisher/v3/applications/com.example.app"
        "/purchases/subscriptionsv2/tokens/***"
    )
    assert payload["request"]["token_masked"] is True


async def test_purchase_token_cannot_escape_path_segment(tmp_path):
    result = await _call(
        "get_subscription_purchase_v2",
        {"package_name": ALLOWED, "token": "../../admin"},
        tmp_path=tmp_path,
    )
    assert result.is_error is True
    assert "token" in result.content[0].text


async def test_no_write_tools_exposed(tmp_path):
    result = await _list_tools(tmp_path=tmp_path)
    banned = ("publish", "commit", "insert", "create", "update", "delete",
              "patch", "upload", "edit_")
    for tool in result.tools:
        assert not any(b in tool.name for b in banned), tool.name