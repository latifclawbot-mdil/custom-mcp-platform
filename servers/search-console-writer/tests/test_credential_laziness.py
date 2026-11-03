"""Whitelist denial happens *before* credential acquisition.

Proved in a fresh interpreter: ``google.auth.default`` is replaced with a
function that records and then fails loudly. A call for a non-allowlisted
property must be refused with ``site_not_allowed`` and record **zero** ADC
attempts. A valid call must reach ADC exactly once, so the probe cannot pass by
never calling anything.

This is the same shape as the read server's credential-laziness proof, applied
to the component where a credential is a write credential.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

PROBE = r"""
import json
import google.auth

calls = []

def tracking_default(*a, **kw):
    calls.append(1)
    raise AssertionError("ADC must not be reached for a rejected call")

google.auth.default = tracking_default

import search_console_writer.auth as auth
auth.reset_session()

from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.server import build_registry

registry = build_registry(PropertyAllowlist(["sc-domain:example.com"]))

# 1. non-allowlisted property
rejected = registry.call("gsc_delete_site", {"site_url": "sc-domain:not-mine.example"})

# 2. out-of-scope sitemap under an allowlisted property
rejected_sitemap = registry.call("gsc_submit_sitemap", {
    "site_url": "sc-domain:example.com",
    "sitemap_url": "https://evil.example/sitemap.xml",
})

# 3. malformed property
rejected_shape = registry.call("gsc_add_site", {"site_url": "https://u:p@example.com/"})

print(json.dumps({
    "adc_calls": len(calls),
    "codes": [
        json.loads(rejected.text)["error"]["code"],
        json.loads(rejected_sitemap.text)["error"]["code"],
        json.loads(rejected_shape.text)["error"]["code"],
    ],
    "is_errors": [rejected.is_error, rejected_sitemap.is_error, rejected_shape.is_error],
}))
"""

VALID_PROBE = r"""
import json
import google.auth

calls = []

class FakeCreds:
    scopes = ["https://www.googleapis.com/auth/webmasters"]

def tracking_default(*a, **kw):
    calls.append(1)
    raise RuntimeError("simulated: no ADC in this environment")

google.auth.default = tracking_default

import search_console_writer.auth as auth
auth.reset_session()

from search_console_writer.allowlist import PropertyAllowlist
from search_console_writer.server import build_registry

registry = build_registry(PropertyAllowlist(["sc-domain:example.com"]))
result = registry.call("gsc_add_site", {"site_url": "sc-domain:example.com"})
_payload = json.loads(result.text).get("error", {})
print(json.dumps({
    "adc_calls": len(calls),
    "code": _payload.get("code"),
    "message": _payload.get("message", ""),
}))
"""


def _run(script: str, tmp_path: Path) -> dict:
    path = tmp_path / "probe.py"
    path.write_text(script)
    proc = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SRC),
            "HOME": str(tmp_path),
        },
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_rejected_calls_never_touch_adc(tmp_path):
    out = _run(PROBE, tmp_path)
    assert out["adc_calls"] == 0
    assert out["codes"] == ["site_not_allowed", "invalid_argument", "invalid_argument"]
    assert out["is_errors"] == [True, True, True]


def test_a_valid_call_does_reach_adc_exactly_once(tmp_path):
    out = _run(VALID_PROBE, tmp_path)
    assert out["adc_calls"] == 1
    # ADC failing is reported cleanly, not as a traceback, and as the
    # actionable configuration code rather than a generic upstream failure.
    assert out["code"] == "not_configured"
    assert "GSC_WRITE_SERVICE_ACCOUNT_FILE" in out["message"]