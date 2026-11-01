"""RED tests: package name validation."""
from __future__ import annotations

import pytest

from play_console_mcp.validation import ValidationError, validate_package_name


@pytest.mark.parametrize("name", [
    "com.example.app",
    "com.example.app_2",
    "a.b",
    "io.github.userx.someapp",
    "com.example.app1.name2",
])
def test_accepts_valid_package_names(name):
    assert validate_package_name(name) == name


@pytest.mark.parametrize("name", [
    "",
    "com",
    "com/example/app",
    "com.example..app",
    ".com.example",
    "com.example.",
    "com.example.app/../other",
    "com.example.app?foo=1",
    "com.example.app#frag",
    "com.example.app:8080",
    "com.example.app%2F..",
    "com ex",
    "com.example.App",
    "com.example-app",
    "../etc/passwd",
    "com.example.app\ncom.other",
    "com.example.app" + "a" * 300,
])
def test_rejects_invalid_package_names(name):
    with pytest.raises(ValidationError):
        validate_package_name(name)


def test_rejects_non_string():
    with pytest.raises(ValidationError):
        validate_package_name(None)  # type: ignore[arg-type]