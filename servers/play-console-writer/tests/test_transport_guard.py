"""Transport-layer argument guard: unknown keys are refused, not dropped.

The MCP SDK builds its argument model from the handler signature and silently
discards keys it does not know, so a caller could smuggle an unrecognised — or
financial-sounding — argument into a tool and have the call succeed as if it had
been honoured. ``schema_guard`` runs as transport middleware and refuses the
call before dispatch.

These tests exercise the guard directly (it is a plain middleware coroutine).
The wire-level proof that the guard is actually installed lives in
``test_stdio_e2e.py`` (``test_unknown_argument_is_refused_over_the_wire``).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from play_console_writer.server import schema_guard

SCHEMAS = {
    "play_edit_insert": {
        "type": "object",
        "properties": {"package_name": {"type": "string"}},
        "required": ["package_name"],
        "additionalProperties": False,
    },
    "play_describe_capability": {
        "type": "object", "properties": {}, "required": [],
        "additionalProperties": False,
    },
}


def _ctx(name, arguments):
    return SimpleNamespace(method="tools/call",
                           params={"name": name, "arguments": arguments})


class _Next:
    def __init__(self):
        self.called = False

    async def __call__(self, ctx):
        self.called = True
        return "PASSED_THROUGH"


def _run(name, arguments):
    guard = schema_guard(SCHEMAS)
    nxt = _Next()
    result = asyncio.run(guard(_ctx(name, arguments), nxt))
    return result, nxt


def _text(result) -> str:
    return " ".join(b.text for b in result.content if getattr(b, "type", None) == "text")


def test_unknown_argument_is_refused_before_dispatch():
    result, nxt = _run("play_edit_insert",
                       {"package_name": "com.example.app", "price": "9.99"})
    assert result.is_error is True
    assert "not a recognised argument" in _text(result)
    assert nxt.called is False


def test_financial_argument_on_a_real_tool_is_refused():
    guard = schema_guard({
        "play_listing_update": {
            "type": "object",
            "properties": {"package_name": {"type": "string"}},
            "required": ["package_name"],
            "additionalProperties": False,
        }
    })
    nxt = _Next()
    result = asyncio.run(guard(_ctx("play_listing_update",
                                    {"package_name": "x", "prices": [{"a": 1}]}), nxt))
    assert result.is_error is True
    assert nxt.called is False


def test_missing_required_argument_is_refused():
    result, nxt = _run("play_edit_insert", {})
    assert result.is_error is True
    assert "required" in _text(result)
    assert nxt.called is False


def test_wrong_type_is_refused():
    result, nxt = _run("play_edit_insert", {"package_name": 123})
    assert result.is_error is True
    assert nxt.called is False


def test_valid_arguments_pass_through():
    result, nxt = _run("play_edit_insert", {"package_name": "com.example.app"})
    assert result == "PASSED_THROUGH"
    assert nxt.called is True


def test_capability_tool_rejects_any_argument():
    result, nxt = _run("play_describe_capability", {"depth": "full"})
    assert result.is_error is True
    assert nxt.called is False


def test_capability_tool_accepts_empty_arguments():
    result, nxt = _run("play_describe_capability", {})
    assert result == "PASSED_THROUGH"
    assert nxt.called is True


def test_non_tools_call_method_passes_through_untouched():
    guard = schema_guard(SCHEMAS)
    nxt = _Next()
    ctx = SimpleNamespace(method="tools/list", params={})
    result = asyncio.run(guard(ctx, nxt))
    assert result == "PASSED_THROUGH"
    assert nxt.called is True


def test_unknown_tool_name_passes_through_to_the_sdk():
    """An unknown tool is the SDK's to reject; the guard must not hide it."""
    result, nxt = _run("play_does_not_exist", {"x": 1})
    assert result == "PASSED_THROUGH"
    assert nxt.called is True


def test_guard_accepts_params_as_objects_too():
    """The SDK may hand the middleware a typed params object, not a dict."""
    guard = schema_guard(SCHEMAS)
    nxt = _Next()
    ctx = SimpleNamespace(
        method="tools/call",
        params=SimpleNamespace(name="play_edit_insert",
                               arguments={"package_name": "x", "extra": 1}),
    )
    result = asyncio.run(guard(ctx, nxt))
    assert result.is_error is True
    assert nxt.called is False