"""Structural safety properties of the write package.

These are deliberately paranoid and static: a write server is only as safe as
the set of requests it *can* construct. The assertions here are about the shape
of the code, not any one call path:

* only PUT and DELETE appear as request verbs;
* exactly four endpoint templates exist, and no generic request primitive;
* the API host and the scope are fixed constants;
* there is no retry, backoff, or loop around a request;
* no secret is logged.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "search_console_writer"


def source_files():
    return sorted(SRC.glob("*.py"))


class TestNoUnexpectedVerbs:
    def test_only_put_and_delete_literals_appear(self):
        allowed = {"PUT", "DELETE"}
        # Any quoted upper-case HTTP verb is a verb this package can emit.
        verb_re = re.compile(r"""["'](GET|POST|PATCH|HEAD|OPTIONS|PUT|DELETE|TRACE|CONNECT)["']""")
        seen: set[str] = set()
        for path in source_files():
            for verb in verb_re.findall(path.read_text()):
                seen.add(verb)
        assert seen <= allowed, f"unexpected verbs in package: {sorted(seen - allowed)}"

    def test_request_guard_rejects_other_verbs(self):
        from search_console_writer.client import ALLOWED_WRITE_METHODS

        assert ALLOWED_WRITE_METHODS == frozenset({"PUT", "DELETE"})


class TestFixedDestinations:
    def test_base_url_is_the_official_webmasters_host(self):
        from search_console_writer.client import BASE_URL

        assert BASE_URL == "https://www.googleapis.com/webmasters/v3"

    def test_scope_is_the_read_write_scope(self):
        from search_console_writer.auth import SCOPES

        assert SCOPES == ("https://www.googleapis.com/auth/webmasters",)

    def test_no_caller_supplied_host_or_scheme(self):
        """No tool argument names a host, URL base, method, or path."""
        from search_console_writer.tools import ToolRegistry

        reg = ToolRegistry(client_factory=lambda: None)
        accepted = {name for spec in reg.specs() for name in spec.accepted}
        assert accepted == {"site_url", "sitemap_url"}
        for forbidden in ("host", "base_url", "method", "endpoint", "headers", "path"):
            assert forbidden not in accepted


class TestNoRetry:
    def test_no_retry_library_is_imported(self):
        offenders = []
        for path in source_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if re.search(r"(?i)tenacity|retry|backoff", name):
                        offenders.append(f"{path.name}: {name}")
        assert offenders == []

    def test_no_retry_call_or_loop_around_a_request(self):
        text = (SRC / "client.py").read_text()
        tree = ast.parse(text)
        retry_call = re.compile(r"(?i)^(retry|backoff|sleep|wait_exponential)$")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None) or ""
                assert not retry_call.match(name), f"retry-shaped call: {name}"
        # No loop may enclose a `_request` call: that is what a retry would be.
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.While)):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == "_request":
                        raise AssertionError("a request call sits inside a loop")

    def test_each_operation_issues_exactly_one_request(self):
        tree = ast.parse((SRC / "client.py").read_text())
        operations = {
            "add_site",
            "delete_site",
            "submit_sitemap",
            "delete_sitemap",
        }
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in operations:
                calls = [
                    n
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "attr", "") == "_request"
                ]
                found[node.name] = len(calls)
        assert found == {name: 1 for name in operations}


class TestFourEndpointsOnly:
    def test_no_generic_request_tool(self):
        from search_console_writer.tools import ToolRegistry

        reg = ToolRegistry(client_factory=lambda: None)
        assert len(reg.names()) == 4
        for name in reg.names():
            assert name in {
                "gsc_add_site",
                "gsc_delete_site",
                "gsc_submit_sitemap",
                "gsc_delete_sitemap",
            }

    def test_endpoint_templates_are_the_two_official_ones(self):
        text = (SRC / "client.py").read_text()
        assert '"/sites/' not in text  # paths are assembled, not literal-joined
        assert "/sitemaps/" in text
        assert "/searchAnalytics/" not in text
        assert "/urlInspection/" not in text


class TestNoSecretLogging:
    def test_package_never_logs_a_token_variable(self):
        suspicious = re.compile(r"\b(token|access_token|bearer)\s*=")
        for path in source_files():
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if suspicious.search(line):
                    assert "static_token" in line or "STATIC_TOKEN" in line, (
                        f"{path.name}:{line_no} assigns a token-like name"
                    )

    def test_redaction_strips_credentials_and_hosts(self):
        from search_console_writer.errors import redact

        text = (
            "Authorization: Bearer ya29.SECRETTOKEN123456 at "
            "https://www.googleapis.com/webmasters/v3/sites/123456789012"
        )
        out = redact(text)
        assert "ya29.SECRETTOKEN123456" not in out
        assert "googleapis.com" not in out
        assert "123456789012" not in out

    def test_redaction_keeps_ordinary_prose(self):
        from search_console_writer.errors import redact

        text = "the property was not found, or is not managed by this credential"
        assert redact(text) == text


class TestPackageSeparation:
    def test_writer_does_not_import_the_read_only_server(self):
        """The two servers must stay independently installable and auditable."""
        offenders = []
        for path in source_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name.startswith("search_console_mcp"):
                        offenders.append(f"{path.name}: {name}")
        assert offenders == [], f"writer imports the read-only server: {offenders}"

    def test_package_lives_under_its_own_directory(self):
        assert SRC.name == "search_console_writer"
        assert SRC.parent.name == "src"
        assert SRC.parents[1].name == "search-console-writer"