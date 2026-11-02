# Verification evidence — search-console-writer

What was run, in what environment, and what each result proves. Every number
below is from a real run; no test reaches Google or uses a real credential.

## Environment

- Python 3.11.15, mcp 2.3.0, google-auth 2.55.1, requests 2.32.5.
- Test interpreter: `/home/prod/repos/custom-mcp-platform/.venv/bin/python`
  (used without modification).
- Command:
  `PYTHONPATH="src:../../packages/mcp_http_transport/src" python -m pytest -c pytest.ini -q`
- Offline: the suite installs an autouse guard that raises on any `connect()`
  to a non-loopback address (`tests/conftest.py`), proven by
  `tests/test_network_guard.py`.

## Result

```
167 passed
```

| File | Tests | What it covers |
|---|---:|---|
| `test_validation.py` | 29 | property shape; sitemap URL shape, scope, dot-segment/traversal, length, credentials, fragment |
| `test_tools.py` | 23 | four-tool surface, dispatch, validation-before-credential ordering, envelopes, argument picking |
| `test_client.py` | 23 | exact PUT/DELETE URLs, encoded segments, one-request-per-call, no retry, timeout, ambiguity, status mapping |
| `test_allowlist.py` | 21 | exact matching, narrow normalisation, prefix/substring denial, `GSC_WRITE_ALLOWED_SITES` |
| `test_stdio_mcp.py` | 19 | real MCP stdio protocol: initialize, tools/list, tools/call, real PUT/DELETE, refusals, capture, exit 2 |
| `test_safety_properties.py` | 15 | structural: verb set, fixed host, no retry machinery, no generic request tool, redaction, package separation |
| `test_server.py` | 13 | truthful annotations (readOnly/destructive/idempotent/openWorld), closed schema, middleware |
| `test_auth.py` | 12 | write-only scope, laziness, SA-file preference, broader-scope refusal, loopback-gated seams |
| `test_http_protocol.py` | 7 | real HTTP: 401 without/with wrong token, initialize, tools/list, allowlist refusal, config repr |
| `test_network_guard.py` | 3 | the no-outbound-network guard blocks public IPs and allows loopback |
| `test_credential_laziness.py` | 2 | fresh interpreter: rejected calls make 0 ADC calls; a valid call makes exactly 1 |

## Tests were shown to fail when the behaviour is broken

Mutation probes, each applied to a throwaway copy and run against the targeted
test (then discarded):

| Mutation | Result |
|---|---|
| annotations always `destructive_hint=False` | killed — destructive-hint test failed |
| allowlist gate removed from dispatch | killed — non-allowlisted-property test failed |
| verb guard widened to permit `POST` | killed — verb-guard test failed |

## Untouched surfaces re-verified

- Read-only Search Console suite: `254 passed`.
- Platform suite: `122 passed`.
- `scripts/gen_integrity_manifest.py --check`: `OK (53 files across 2 servers)`.
- `git diff` over `servers/search-console`, `servers/play-console`,
  `packages/`, `tests/`, `docs/`, `deploy/`, `Makefile`, `pyproject.toml`,
  `pytest.ini`, `README.md`: empty.

## What is not proven

- No call was made to Google. The write endpoints are exercised against a
  loopback fake; Google's actual acceptance of the four requests is not
  demonstrated here.
- No real service-account credential was loaded; the write scope is asserted
  from the code path, not from a token.
- No deployment, image build, or gateway connection was performed.
- Per-operation human approval is not implemented or tested, because it is
  delegated to the LiteLLM gateway by design.
