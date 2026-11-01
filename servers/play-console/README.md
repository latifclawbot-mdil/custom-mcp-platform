# play-console-mcp

Read-only **Google Play Console** access for MCP clients, over stdio, backed by
the official **Android Publisher API v3** (`androidpublisher.googleapis.com`).

Intended to be mounted in LiteLLM (or any other MCP host) as a stdio server.

## Guarantees

| Property | How it is enforced |
| --- | --- |
| **Read-only** | `GoogleApiClient.request()` accepts only `GET` and refuses any other verb *before* any network call. A source-level test fails the build if a mutating call (`.post(`, `.put(`, `edits().insert`, `publish`, `commit`, …) appears anywhere in the shipped package. |
| **Package allowlist, fail closed** | `PLAY_CONSOLE_ALLOWED_PACKAGES` is mandatory. With no allowlist the process exits with status 2 at startup — it never defaults to permissive. Each tool call re-validates the package against the allowlist. |
| **No app enumeration** | The Play API has no "list my apps" endpoint this server is permitted to call, so every tool takes an explicit `package_name`. Nothing is invented. |
| **URL-injection safe** | Identifiers are validated (`validation.py`), not escaped: `/`, `\`, `?`, `#`, `:`, `%`, `..`, whitespace and control characters are rejected. The client re-asserts the final path stays under `/androidpublisher/v3/`. |
| **Bounded requests** | Every call shares a single total deadline (no 3× timeout retry amplification); page sizes are clamped to `PLAY_CONSOLE_MAX_PAGE_SIZE`; only idempotent GETs on 429/5xx retry, with bounded jittered backoff inside that deadline. |
| **Redacted errors** | Upstream bodies, auth headers and non-JSON (proxy HTML) bodies are redacted and truncated. Access tokens (`ya29.…`) are redacted even when embedded in a larger string, OAuth authorization codes (`4/…`) are redacted, and token material is masked in the audit echo — in path segments *and* query values. |
| **No secret printing** | Credential paths and delegated subjects are excluded from `repr()`; no credential material is ever returned in a tool result. |

No `publish`, `edits.insert`, `commit`, upload or any other side-effecting
operation exists in this package.

## Tools (16, all `GET`)

| Tool | Upstream path |
| --- | --- |
| `list_reviews` | `applications/{pkg}/reviews` |
| `get_review` | `applications/{pkg}/reviews/{reviewId}` |
| `list_device_tier_configs` | `applications/{pkg}/deviceTierConfigs` |
| `get_device_tier_config` | `applications/{pkg}/deviceTierConfigs/{id}` |
| `list_in_app_products` | `applications/{pkg}/inappproducts` |
| `get_in_app_product` | `applications/{pkg}/inappproducts/{sku}` |
| `list_subscriptions` | `applications/{pkg}/subscriptions` |
| `get_subscription` | `applications/{pkg}/subscriptions/{productId}` |
| `list_subscription_offers` | `applications/{pkg}/subscriptions/{productId}/basePlans/{basePlanId}/offers` |
| `list_one_time_products` | `applications/{pkg}/oneTimeProducts` |
| `get_one_time_product` | `applications/{pkg}/oneTimeProducts/{productId}` |
| `get_subscription_purchase_v2` | `applications/{pkg}/purchases/subscriptionsv2/tokens/{token}` |
| `get_product_purchase` | `applications/{pkg}/purchases/products/{productId}/tokens/{token}` |
| `list_voided_purchases` | `applications/{pkg}/purchases/voidedpurchases` |
| `get_order` | `applications/{pkg}/orders/{orderId}` |
| `list_app_recoveries` | `applications/{pkg}/appRecoveries` |

All are advertised with `readOnlyHint=true`, `destructiveHint=false`.

> The v1 `purchases.subscriptions.get` endpoint
> (`.../purchases/subscriptions/{subscriptionId}/tokens/{token}`) is no longer
> part of the official Android Publisher v3 discovery document, so that tool was
> removed. Use `get_subscription_purchase_v2` for subscription state.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `PLAY_CONSOLE_ALLOWED_PACKAGES` | **yes** | Comma-separated Android package names this server may read. Server refuses to start if absent or empty. |
| `PLAY_CONSOLE_CREDENTIALS_FILE` | no | Service-account JSON path. Omit to use ADC. |
| `PLAY_CONSOLE_SUBJECT` | no | Delegated user for domain-wide delegation. Requires `PLAY_CONSOLE_CREDENTIALS_FILE`. |
| `PLAY_CONSOLE_REQUEST_TIMEOUT` | no | Seconds, `0.1`–`300` (default `20`). |
| `PLAY_CONSOLE_MAX_PAGE_SIZE` | no | Hard page-size cap, `1`–`1000` (default `100`). |
| `PLAY_CONSOLE_USER_PROJECT` | no | Quota project billed for the requests. |

### Credentials

Scope requested: `https://www.googleapis.com/auth/androidpublisher` (the
narrowest scope that permits Play Console reads).

Credentials are resolved **lazily** — `initialize` and `list_tools` work with no
credentials present, so an MCP host can discover tools before auth is wired up.
The first actual tool call resolves them.

Two supported modes:

* **ADC** — set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account file, run
  `gcloud auth application-default login`, or rely on a workload identity.
* **Service account file** — set `PLAY_CONSOLE_CREDENTIALS_FILE`.

### Account restrictions you must satisfy on the Google side

These are outside this repo and are the usual cause of a working install
returning `403`:

1. The service-account email must be invited in **Play Console → Users and
   permissions** for the specific app, with at least **"View app information"**
   (read) permission. A project-level IAM role alone is not enough.
2. For a service account acting on behalf of a human (`PLAY_CONSOLE_SUBJECT`),
   **domain-wide delegation** must be configured in the Workspace admin console
   and the client ID must be authorized for the `androidpublisher` scope.
3. Play Console access is per-app. A service account with access to app A will
   still get `403` for app B — this is expected, not a bug in this server.

### Mounting in LiteLLM

```yaml
mcp_servers:
  play_console:
    transport: stdio
    command: play-console-mcp
    env:
      PLAY_CONSOLE_ALLOWED_PACKAGES: "com.example.app,com.example.other"
      PLAY_CONSOLE_CREDENTIALS_FILE: "/run/secrets/play-sa.json"
```

## Install and test

```bash
uv sync --extra dev        # or: pip install -e '.[dev]'
uv run pytest -q           # 150 tests
```

The suite is offline: Google endpoints are faked with `respx` in unit tests, and
the MCP end-to-end tests spawn a **test-only launcher**
(`tests/_fake_stdio_server.py`) that injects the fake HTTP transport into the
server via `build_server`'s injection points. The fake transport is not part of
the shipped package, so a production install can never return fabricated data.
**No live Google call is made, and no Google credentials are required to run the
tests.**

## Honest limitations

* **No live-auth verification has been performed.** Every Google interaction in
  the test suite goes through a fake transport; the real credential path
  (ADC resolution, token refresh, Play Console IAM grants) has not been
  exercised against production Google. Treat the first live call as a
  verification step.
* **Read-only means read-only.** If you need to author or publish releases, use
  the Play Console UI or a separate, explicitly mutating tool.
* **No app enumeration.** Package names must be supplied by the caller and
  allowlisted up front; there is no discovery endpoint.
* Purchase tokens are masked in the audit echo, but the upstream response body
  for a purchase lookup is returned as-is — it contains order/purchase detail.
  Keep tool-result logging appropriately scoped.