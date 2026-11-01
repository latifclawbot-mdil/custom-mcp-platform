# search-console-mcp

A read-only, stdio-transport MCP server for the Google Search Console API,
intended to be spawned by an MCP client such as a LiteLLM gateway.

It exposes four read-only tools over an explicit, fail-closed property
allowlist. It cannot write to Search Console, cannot reach a host of the
caller's choosing, and cannot inspect URLs outside an allowlisted property.

## Tools

| Tool | Purpose |
|---|---|
| `gsc_list_sites` | List the allowlisted accessible properties, with permission level |
| `gsc_search_analytics` | Clicks / impressions / CTR / position, grouped and filtered |
| `gsc_list_sitemaps` | Sitemaps with submission, error and warning counts |
| `gsc_inspect_url` | Index status of one URL inside an allowlisted property |

`searchAnalytics.query` and `urlInspection.index:inspect` are POSTs, but both are
read-only operations: neither mutates Search Console state.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

Pinned runtime dependencies: `mcp==2.3.0`, `google-auth==2.55.1`,
`requests>=2.31,<3` (the transport `google-auth` needs for `AuthorizedSession`).

## Configure

| Variable | Required | Meaning |
|---|---|---|
| `GSC_ALLOWED_SITES` | **yes** | Comma-separated exact property identifiers |
| `GOOGLE_APPLICATION_CREDENTIALS` | no | Service-account JSON path, standard ADC discovery |
| `GSC_LOG_LEVEL` | no | stderr log level, default `WARNING` |

```bash
export GSC_ALLOWED_SITES='sc-domain:example.com,https://www.example.com/'
```

The server **refuses to start** (exit code 2) when `GSC_ALLOWED_SITES` is unset
or empty. There is no permissive default and no "allow all" mode.

Grant the credential the **read-only** scope
`https://www.googleapis.com/auth/webmasters.readonly` — the only scope this
server ever requests. A service account must additionally be added as a user of
each property in the Search Console UI; otherwise Search Console returns 403,
which this server reports as `unauthorized` with that hint.

### MCP client config

```json
{
  "mcpServers": {
    "search-console": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "search_console_mcp"],
      "env": {
        "GSC_ALLOWED_SITES": "sc-domain:example.com",
        "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/service-account.json"
      }
    }
  }
}
```

## Security properties

- **Allowlist, fail-closed.** Exact match after narrow normalization (Domain
  hosts lowercased, URL-prefix properties gain a trailing `/`). A property that
  merely starts with, extends, or case-differs from an allowed one is rejected.
- **Property is one encoded path segment.** Encoded with `quote(safe="")`, so
  `sc-domain:example.com` becomes `sc-domain%3Aexample.com` and no property
  string can introduce a slash, query, fragment, or traversal.
- **Fixed hosts.** Both API origins are module constants. No tool argument can
  influence scheme, host, or base path.
- **Inspection is property-scoped.** The inspected URL must be an absolute
  http(s) URL with no credentials and no fragment, and must sit under the
  allowlisted property. A `.`/`..` path segment — raw *or* percent-encoded — is
  rejected outright, because the same string resolves to a different path (and
  can leave the property) once a client applies RFC 3986 resolution.
- **Only allowlisted properties are named.** `gsc_list_sites` returns the
  properties this server may actually serve; properties the credential can
  reach but the allowlist excludes are reported only as a `sites_withheld`
  count, so the account's property inventory is not disclosed.
- **Bounds before request.** Dates (format, order, span ≤ 1640 days, not
  future), `row_limit` 1–25000, `start_row` 0–200000, allowlisted dimensions
  without duplicates (≤ 5), allowlisted search type / aggregation / data state,
  filter count ≤ 10 and expression ≤ 1024 chars. Returned rows are capped and
  truncation is reported explicitly, with `next_start_row` advancing past every
  upstream row consumed.
- **No writes.** Only `GET` and two read-only `POST`s are constructed; a
  defensive check rejects any other verb.
- **Lazy credentials.** ADC is touched on the first *valid* call only. Rejected
  calls — bad allowlist, bad bounds, bad URL — never reach credential
  acquisition, which tests assert in a fresh interpreter.
- **Redacted errors.** Structured envelopes with stable codes; upstream text is
  stripped of URLs, numeric IDs, bearer/Basic credentials, key/value secrets,
  and key material. Output is strict JSON (no bare `NaN`/`Infinity`), a failed
  call sets the MCP `isError` flag, and tool results never contain a traceback.
- **Key material stays out of scope.** The bearer token is attached by
  `AuthorizedSession` and never read, stored, or logged by this code.

## Startup

```bash
# Serving
GSC_ALLOWED_SITES='sc-domain:example.com' .venv/bin/python -m search_console_mcp
# or, after `pip install -e .`
GSC_ALLOWED_SITES='sc-domain:example.com' search-console-mcp

# Without a configured allowlist (expect exit 2, message on stderr)
.venv/bin/python -m search_console_mcp
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

- `test_allowlist.py` — allowlist matching, encoding, redaction, error envelopes
- `test_client.py` — request construction, bounds, URL scope, upstream error mapping
- `test_auth.py` — credential laziness, scope pinning, redaction of auth failures
- `test_tools.py` — tool schemas, argument rejection, envelope shape, lazy clients
- `test_safety_properties.py` — structural no-write and no-secret-logging checks
- `test_seams.py` — test-only seams (capture, static token, base override) are inert off-loopback
- `test_stdio_mcp.py` — real subprocess over stdio: `initialize`, `tools/list`, `tools/call`

No test reaches Google, uses a real credential, or contains private data. The
stdio tests run the server as a real subprocess against an in-process fake
upstream, with an empty `HOME` and no ADC file.

## Known gaps

- URL-prefix scope matching is string-prefix based against the normalized
  property, which is segment-aligned because normalization supplies the
  trailing slash.
- No response caching; one upstream request per call.
- One property per call; no fan-out.
- `GSC_API_BASE_OVERRIDE`, `GSC_INSPECTION_BASE_OVERRIDE`,
  `GSC_TEST_CAPTURE_DIR`, and `GSC_TEST_STATIC_TOKEN` are test seams, not
  supported configuration. They are ignored unless *every* API base points at a
  loopback host, so a deployment that inherits one cannot persist request
  bodies or send a static token to Google.
- On mcp 2.3.0, the SDK's derived input schema advertises
  `additionalProperties` from the handler signature rather than from this
  server's contract; the advertised value is corrected to `false` and enforced
  by a transport-layer middleware. See `DESIGN.md`.