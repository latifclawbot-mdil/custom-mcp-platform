# search-console-writer-mcp

A **write-capable** MCP server exposing exactly the four mutation methods of the
Google Search Console API, for use behind a LiteLLM MCP gateway. It is a separate
server from the read-only `servers/search-console/` package: separate package,
separate process, separate write allowlist, and a credential carrying the
read/write `webmasters` scope.

It cannot make an arbitrary HTTP request. Every request is one of four fixed
Google operations, against a fixed host, built from validated and
percent-encoded path segments.

## The four tools

| Tool | Google method | HTTP | Scope |
|---|---|---|---|
| `gsc_add_site` | `sites.add` | `PUT /webmasters/v3/sites/{siteUrl}` | `webmasters` |
| `gsc_delete_site` | `sites.delete` | `DELETE /webmasters/v3/sites/{siteUrl}` | `webmasters` |
| `gsc_submit_sitemap` | `sitemaps.submit` | `PUT /webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}` | `webmasters` |
| `gsc_delete_sitemap` | `sitemaps.delete` | `DELETE /webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}` | `webmasters` |

These are the complete non-financial write operations of the Search Console API.
There is no fifth tool, no "index this URL" tool, and no generic request tool.

MCP annotations are per-tool and truthful: `readOnlyHint=false` on all four,
`destructiveHint=true` only on the two deletes, `idempotentHint=true` (PUT and
DELETE are idempotent at the HTTP level), `openWorldHint=true`.

## Where authorisation happens

This server authenticates the **caller of its endpoint** with a shared bearer
token (HTTP transport) or the spawning process's stdio pipe. That is endpoint
authentication only.

Authorisation — which client may call which tool, rate limits, and any human
approval step — is delegated to the **LiteLLM MCP gateway** in front of this
server. In particular:

> An authenticated write is **not** evidence that an individual human approved
> that specific operation. This server does not implement per-call approval and
> does not claim to. If a deployment requires per-operation human approval, that
> gate belongs in the gateway (or a policy engine beside it), not here.

## Configure

| Variable | Required | Meaning |
|---|---|---|
| `GSC_WRITE_ALLOWED_SITES` | **yes** | Comma-separated exact property identifiers this server may mutate |
| `GSC_WRITE_SERVICE_ACCOUNT_FILE` | no | Service-account JSON for a **write** principal; preferred over ADC so the writer can run as a different identity from the read server |
| `GOOGLE_APPLICATION_CREDENTIALS` | no | Standard ADC fallback, used with the write scope |
| `GSC_WRITE_LOG_LEVEL` | no | stderr log level, default `WARNING` |
| `MCP_HTTP_AUTH_TOKEN` / `MCP_HTTP_AUTH_TOKEN_FILE` | HTTP only | Bearer token the endpoint requires |

The server **refuses to start** (exit code 2) when `GSC_WRITE_ALLOWED_SITES` is
unset or empty. There is no permissive default and no wildcard.

Grant the write credential `https://www.googleapis.com/auth/webmasters` — the
only scope this server requests. A credential carrying anything broader is
refused at use time.

## Run

```bash
# stdio (spawned by an MCP client)
GSC_WRITE_ALLOWED_SITES='sc-domain:example.com' \
GSC_WRITE_SERVICE_ACCOUNT_FILE=/run/secrets/gsc-write.json \
.venv/bin/python -m search_console_writer

# authenticated Streamable HTTP
MCP_HTTP_AUTH_TOKEN=... GSC_WRITE_ALLOWED_SITES='sc-domain:example.com' \
.venv/bin/python -m search_console_writer.http_main
```

## Security properties

- **Write allowlist on every tool, fail-closed.** Exact match after narrow
  normalisation (Domain hosts lowercased; URL-prefix properties gain a trailing
  `/`). A property that merely starts with, extends, or case-differs from an
  allowed one is rejected. It is a **separate** variable and instance from the
  read server's `GSC_ALLOWED_SITES`.
- **Validation before credentials.** Argument shape → property shape → allowlist
  → sitemap scope → *then* client/credential construction. A refused call
  performs no credential lookup; a test proves `google.auth.default` is never
  called for rejected calls.
- **Fixed host, fixed verbs.** The only origin is
  `https://www.googleapis.com/webmasters/v3`. The only verbs are `PUT` and
  `DELETE`. No tool argument names a host, scheme, method, path, or header.
- **Segments are percent-encoded with `safe=""`.** A property or sitemap URL
  cannot add a path segment, a query, a fragment, or `..` traversal.
- **Sitemap URLs are scoped to the property.** For a Domain property the host
  must be the domain or a subdomain of it; for a URL-prefix property the URL
  must start with the property prefix and use the same scheme. Dot-segments —
  raw or percent-encoded — are rejected.
- **One bounded timeout.** Every request carries a single explicit timeout
  (default 30 s). There is no connect/read split to reason about and no retry
  budget.
- **No automatic retry of an ambiguous effect.** A transport failure on a
  mutating verb that may have been delivered is reported as
  `ambiguous_outcome` with `may_have_applied: unknown`, and a 5xx carries
  `may_have_applied: true`. A connect timeout (request never sent) is
  `network_error` with `may_have_applied: false`. The caller must reconcile
  before retrying.
- **Redacted errors.** Structured envelopes, stable codes, no tracebacks, no
  upstream hostnames, URLs, numeric IDs, or bearer tokens.
- **Zero network in tests.** The suite installs a process-wide guard that
  rejects any `connect()` to a non-loopback address.

## Test

```bash
cd servers/search-console-writer
PYTHONPATH="src:../../packages/mcp_http_transport/src" \
  ../../.venv/bin/python -m pytest -q
```

or `make test` from this directory. No test reaches Google, uses a real
credential, or contains private data. The stdio suite spawns the real server and
issues real `PUT`/`DELETE` requests to an in-process loopback fake.

See `DESIGN.md` for the model and the exact list of known gaps.

## Known gaps

- The writer does not enforce per-operation human approval; that is the
  gateway's job (see above).
- `sites.add` only adds a property to the account. It does not verify
  ownership, and it cannot claim a property the credential does not already
  control.
- Sitemap URLs are validated by scope and shape, not by fetching them. A
  syntactically valid sitemap URL that Google cannot fetch is accepted here and
  fails at Google.
- Property hosts must be ASCII (or punycode `xn--` A-labels); a Unicode IDN
  written in its native script is rejected rather than silently converted.
- No response caching and no fan-out: one call is one property and at most one
  request.
- A 4xx response is treated as "not applied". Google does not document a case
  where it mutates and then returns 4xx, but only 5xx/transport ambiguity is
  explicitly flagged.
- The test seams (`GSC_WRITE_API_BASE_OVERRIDE`, `GSC_WRITE_TEST_CAPTURE_DIR`,
  `GSC_WRITE_TEST_STATIC_TOKEN`) are not supported configuration; they are
  ignored unless the API base points at a loopback host.