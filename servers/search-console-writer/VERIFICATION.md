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
215 passed
```

| File | Tests | What it covers |
|---|---:|---|
| `test_writer_review_regressions.py` | 21 | review findings: redirects refused/not followed, 3xx never success, bounded/dropped untrusted text; each envelope bound pinned independently |
| `test_wire_redirects.py` | 14 | real loopback wire: a redirect is never followed to a second host; 200/204/403/5xx still behave |
| `test_writer_config_regressions.py` | 13 | credential mapping to `not_configured`, invalid numeric settings exit 2, loopback seam requires a test token |
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
| `allow_redirects` left at the requests default (a followed 307/308) | killed — `test_a_redirect_is_not_followed_to_another_host` and `test_a_redirect_status_is_an_error_not_a_success` failed |
| `CredentialError` left as a plain `RuntimeError` | killed — `test_credential_failure_maps_to_not_configured` failed |
| numeric settings parsed with a bare `int()`/`float()` | killed — the traceback-free startup test failed |
| loopback override allowed to fall through to a real service-account file | killed — `test_no_static_token_on_loopback_refuses_instead_of_using_a_real_key` failed |
| upstream error message reflected unbounded (both bounding layers removed) | killed — `test_a_huge_error_message_is_bounded` failed |
| envelope message cap removed on its own | killed — `test_the_envelope_bounds_an_over_long_message_by_itself` failed |
| envelope detail cap removed on its own | killed — `test_the_envelope_bounds_an_over_long_detail_by_itself` failed |
| client-side message bound removed on its own | killed — `test_the_client_bounds_a_hostile_message_before_the_envelope_sees_it` failed |

The message length is bounded twice — in `client._error_from_response` (before an
error is built) and again in `GscError.__init__`. Either layer alone keeps the
envelope small, so removing only one is invisible to a behavioral test. The
bounds are therefore pinned layer-by-layer above; the three "on its own" rows
are the evidence that neither cap can be deleted unnoticed.

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
