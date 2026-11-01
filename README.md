# Authenticated MCP Streamable HTTP for two Google operations servers

A self-contained monorepo that packages two existing read-only MCP servers
(Google Play Console, Google Search Console) for a LiteLLM gateway that speaks
**MCP Streamable HTTP**, with each server in its own container behind its own
bearer token.

The server source is maintained in this monorepo. The initial copies came from
independent local repositories; their source revisions and file hashes are recorded
in `docs/vendoring-manifest.txt` and `docs/integrity-manifest.json`.

| Path | What it is |
| --- | --- |
| `packages/mcp_http_transport/` | The reusable transport package: bearer auth, Host/Origin allowlist, path normalisation, session limits, `serve()`. |
| `servers/play-console/` | Committed Play Console source, plus one new file: `src/play_console_mcp/http_main.py`. |
| `servers/search-console/` | Committed Search Console source, plus one new file: `src/search_console_mcp/http_main.py`. |
| `deploy/` | Two Dockerfiles, a compose file, and the pinned requirement set. |
| `tests/` | Platform tests: real-HTTP security, real MCP protocol, isolation, deployment artifacts, hermetic vendored-source integrity. |
| `docs/security.md` | The security model, stated precisely, including what is **not** verified. |
| `docs/integrity-manifest.json` | Committed `path -> sha256` map of the vendored trees; regenerate with `scripts/gen_integrity_manifest.py`. |
| `docs/verification-evidence.md` | What was run, in what environment, and what it proved (and did not). |

## Why the transport is a package, not two copied snippets

Auth, Host allowlisting, path handling, and failure modes are identical for both
servers, and they are the parts most likely to be got subtly wrong twice. The
package is the only place any of that lives; each server gets a ~60-line entry
point that declares its own allowlist and its own token, and nothing else.

## Quick start

```bash
make venv          # .venv with the pinned lock
make test          # 122 platform tests: real HTTP, isolation, artifacts, integrity
make integrity     # hermetic vendored-source check against the committed manifest
make test-upstream # 150 Play + 254 Search tests, from the copied source
```

`make test` covers the platform suite and `make test-upstream` both original
suites. No Google credential is needed for any test:
the servers are spawned with a fake upstream on loopback and a placeholder
token, and the credential path is never exercised.

## Run one server locally

```bash
TOKEN=$(python -m mcp_http_transport.generate_token)
export MCP_HTTP_AUTH_TOKEN="$TOKEN"
export MCP_HTTP_PORT=8080
export MCP_HTTP_ALLOWED_HOSTS="127.0.0.1,play-console-mcp"
export PLAY_CONSOLE_ALLOWED_PACKAGES="com.example.app"
PYTHONPATH="packages/mcp_http_transport/src:servers/play-console/src" \
  .venv/bin/python -m play_console_mcp.http_main
```

`scripts/smoke_http.sh` does that plus an unauthenticated call, a wrong-token
call, a valid `initialize`, and a `tools/list`, printing the real HTTP status of
each.

## Configuration

Both servers read the same transport variables.

| Variable | Required | Meaning |
| --- | --- | --- |
| `MCP_HTTP_AUTH_TOKEN` | one of the two | The shared bearer token. |
| `MCP_HTTP_AUTH_TOKEN_FILE` | one of the two | File containing the token; preferred in containers so it stays out of the process environment. |
| `MCP_HTTP_HOST` | no | Bind address (default `0.0.0.0`). |
| `MCP_HTTP_PORT` | no | Bind port (default `8080`). |
| `MCP_HTTP_PATH` | no | Endpoint path (default `/mcp`). |
| `MCP_HTTP_ALLOWED_HOSTS` | no | Comma-separated Host header allowlist; defaults to the bind host. |
| `MCP_HTTP_MAX_SESSIONS` | no | Concurrent session cap (default `100`). |
| `MCP_HTTP_SESSION_IDLE_TIMEOUT` | no | Idle session expiry in seconds (default `300`). |

Plus each server's own fail-closed allowlist (`PLAY_CONSOLE_ALLOWED_PACKAGES`,
`GSC_ALLOWED_SITES`) and credential variables, unchanged from the originals.

**Missing or short token ⇒ exit code 2, no socket opened.** That is covered by
`tests/test_http_security.py::test_startup_refuses_without_a_token`.

## LiteLLM connection

For the homelab, add each server through the LiteLLM dashboard as Streamable HTTP.
Use the private Docker aliases below and configure a separate upstream bearer
credential securely for each server. Check compatibility with the deployed
LiteLLM version; no connection has yet been verified through the gateway.

```text
http://play-console-mcp:8080/mcp
http://search-console-mcp:8080/mcp
```

Both containers sit on the shared external Docker network `coolify` (the one
LiteLLM is already on), each with a **stable alias** — `play-console-mcp` and
`search-console-mcp` — so the URLs above resolve. Neither publishes a host port
and neither sets a hostname. The alias must also appear in each service's
`MCP_HTTP_ALLOWED_HOSTS` (it does), otherwise every gateway request is answered
`421`; a test asserts the two agree.

The trade-off: because `coolify` is shared, any container on that network can
open a connection to `:8080` and attempt authentication. The bearer token, not
the network, is what authorises a call. See `docs/security.md` §9 for the risk
and how to narrow it.

## Containers

```bash
PLAY_MCP_TOKEN_FILE=/run/host-secrets/play.token \
GSC_MCP_TOKEN_FILE=/run/host-secrets/gsc.token \
PLAY_SERVICE_ACCOUNT_FILE=/run/host-secrets/play-sa.json \
GSC_SERVICE_ACCOUNT_FILE=/run/host-secrets/gsc-sa.json \
PLAY_CONSOLE_ALLOWED_PACKAGES=com.example.app \
GSC_ALLOWED_SITES=sc-domain:example.com \
docker compose -f deploy/docker-compose.yml up -d --build
```

Neither image contains a secret, a Google identifier, or the other server's
code. Both run as uid 10001 with a read-only root filesystem, all capabilities
dropped, and `no-new-privileges`.

Each secret file passed above is delivered as a **bind mount**, so it arrives
in the container with the host file's owner and mode (Compose cannot remap them
for a `file:` secret). Make each host file readable by uid 10001, or the
container exits at startup:

```bash
chown 10001:10001 /run/host-secrets/*.token /run/host-secrets/*-sa.json
chmod 0400        /run/host-secrets/*.token /run/host-secrets/*-sa.json
```

`scripts/verify_secret_mount.sh` demonstrates both the failing (`root:0600`)
and working (`10001:0400`) cases against a live engine.

## What is deliberately not here

- **No deployment and no Coolify resources.** The compose file attaches to the
  existing `coolify` network and is ready to run, but nothing here is deployed
  and no Coolify resource is created. No published port.
- **No submodule dependency.** Server source is copied into this repository;
  its initial content is recorded in `docs/integrity-manifest.json`.
- **No guessed Google identifiers.** No project number, client id, service
  account email, or allowlist value is invented anywhere. Allowlists come from
  environment variables that abort the deploy when unset.
- **No stdio command surface.** The servers run one process each and expose one
  MCP endpoint. There is no endpoint that can be told to run a command, and no
  adapter process in between.
- **No security claim that has not been tested.** See `docs/security.md`,
  especially the "not verified" section.