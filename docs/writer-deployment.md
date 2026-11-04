# Writer MCP deployment

How the two write-capable MCP servers in this repository are packaged and
deployed, and — stated plainly — what has and has not been verified about that
packaging.

## Status: unreleased, not deployed

The Play Console writer and Search Console writer images described here are
**not deployed anywhere**, have **not been published**, and have **not been
built** in the environment where this document was written (no Docker daemon was
available). This document describes the packaging artifacts in the repository;
it is not evidence that a container ran, that a provider credential is valid, or
that the gateway has been reconfigured. Those are deployment-time checks and are
listed in "What is still outstanding" below.

The four artifacts:

| Artifact | Purpose |
| --- | --- |
| `deploy/Dockerfile.play-writer` | Image for the non-financial Play Console writer |
| `deploy/Dockerfile.search-writer` | Image for the Search Console writer |
| `deploy/docker-compose.writers.yml` | Runtime definition for both writers |
| `servers/play-console-writer/src/play_console_writer/http_main.py` | The Play writer's HTTP entry point (it was stdio-only before) |

They are separate from the read-only platform's `deploy/Dockerfile.play-console`,
`deploy/Dockerfile.search-console` and `deploy/docker-compose.yml`. The read-only
files are untouched; the writers never share an image, a container, or a
credential with them.

## The Play writer gains an HTTP entry point

Until this change the Play writer (`servers/play-console-writer`) could only
serve over **stdio**: `python -m play_console_writer` calls
`play_console_writer.server.main`, which runs `server.run("stdio")`. A stdio
process has no listening socket, so a container built from it could not be
reached by the LiteLLM gateway at all, and it has no bearer requirement of its
own.

`http_main.py` is a thin adapter — the same pattern the read-only servers and the
Search writer already use:

- it imports `build_server` from `play_console_writer.server` and hands the same
  server object to the shared HTTP transport in
  `packages/mcp_http_transport`; it does not re-declare any tool;
- startup order is allowlist → bearer token → bind, so a missing token exits `2`
  before a socket is opened;
- Google credentials are still acquired lazily, so `initialize` and `tools/list`
  need no credential;
- it is served as `python -m play_console_writer.http_main` and is the
  Dockerfile `ENTRYPOINT`.

## Isolation posture

Both writer containers run with:

- **non-root**, uid/gid `10001`, no shell, no home directory;
- `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, an 8 MiB
  `noexec` tmpfs at `/tmp`;
- **no published host port** (`expose: ["8080"]`, never `ports:`), no
  `hostname:` and no domain — nothing is exposed on the host;
- **one server package per image**: the Play image contains only
  `play-console-writer` plus the shared transport, and the Search image only
  `search-console-writer` plus the shared transport. The read-only packages and
  the other writer are absent from each image.
- a **pinned base image** (`python:3.11.15-slim-bookworm`) and fully pinned
  dependencies from `deploy/constraints.txt`.
- a **socket-level healthcheck** only. It proves a listener exists; it proves
  neither authentication nor provider readiness.

Credentials are always mounted files, never environment literals baked into a
layer:

- each writer has its **own** bearer-token secret and its **own** provider
  credential secret, and neither service is given the other's path;
- the bearer token is read from a mounted file (`MCP_HTTP_AUTH_TOKEN_FILE`), so
  it does not appear in `docker inspect`;
- the Play writer takes a service-account JSON via
  `PLAY_WRITER_CREDENTIALS_FILE`; the Search writer takes one via
  `GOOGLE_APPLICATION_CREDENTIALS`.

### Secret files: a bind mount, and why the operator must act

A Compose `file:` secret is delivered as a **bind mount**, and bind mounts do not
remap ownership or mode. The file arrives in the container with the *host file's*
uid/gid and permission bits, so the `uid`, `gid` and `mode` keys of a secret do
not apply to a `file:` source — they are deliberately absent from the compose
file rather than set to values that would be ignored. A root-owned `0600` file is
the natural thing to create and it fails for uid `10001`, making the container
exit at startup rather than serve an unauthenticated endpoint. The operator must
make each host secret readable by the container user:

```
chown 10001:10001 /path/to/secret
chmod 0400        /path/to/secret
```

### Network reachability, stated honestly

The writers attach to the existing external Docker network `coolify` (the one
LiteLLM is on) with a **fixed private alias** each —
`play-console-writer-mcp` and `search-console-writer-mcp`. There is no published
port, no public domain and no tunnel route.

What this does and does not mean:

- No host port is published; host and network routing controls must still be verified.
- On the shared `coolify` network, any attached container can attempt a connection.
  A dedicated bridge joined only by LiteLLM and the writer can reduce which
  containers can reach the endpoint; this example has not implemented it.
- Anyone who can reach the endpoint *and* holds the token bypasses the gateway's
  own authorisation. Treat writer tokens as sensitive and distinct per service.
  Do not expose a writer until its chosen network and gateway controls are tested.

The Host allowlist (`MCP_HTTP_ALLOWED_HOSTS`) must contain the alias the service
answers to, or every gateway request is rejected with `421`. `tests/test_writer_deploy.py`
asserts the alias and the allowlist agree, so a mismatch fails at test time
rather than in production.

## Running it

```
PLAY_WRITER_ALLOWED_PACKAGES=com.example.app \
GSC_WRITE_ALLOWED_SITES=sc-domain:example.com \
PLAY_WRITER_MCP_TOKEN_FILE=/run/host-secrets/play_writer.token \
SEARCH_WRITER_MCP_TOKEN_FILE=/run/host-secrets/search_writer.token \
PLAY_WRITER_SERVICE_ACCOUNT_FILE=/run/host-secrets/play_writer.json \
SEARCH_WRITER_SERVICE_ACCOUNT_FILE=/run/host-secrets/search_writer.json \
docker compose -f deploy/docker-compose.writers.yml up -d
```

The allowlist variables are required (`:?`), so a missing allowlist aborts the
deploy rather than defaulting to permissive. The two tokens must differ, and each
must differ from the read-only services' tokens.

## What has been verified, and how

Only what the tests actually execute is claimed:

- **`tests/test_writer_deploy.py`** reads the three deploy files and asserts the
  isolation properties above (no published port, fixed alias in the allowlist,
  distinct tokens and credentials, non-root, pinned base and constraints, one
  server package per image, no baked secret).
- The same file starts the **Play writer's real HTTP app** built through its own
  `http_main` and drives it over a **real loopback socket** with uvicorn. It
  asserts: a missing / wrong / near-miss token is `401` with no session opened;
  a malformed `Authorization` header is `401`; a valid token opens a **stateful**
  MCP session (`Mcp-Session-Id` returned, `tools/list` works on it); an unlisted
  `Host` is `421`; an unknown path is `404`; an unauthenticated `tools/call` is
  `401` with no tool result.
- The same file runs **both writers concurrently** and shows each rejects the
  other's token, and that one writer's session id is not accepted by the other.
- The process-level fail-closed path is exercised by spawning
  `python -m play_console_writer.http_main` with no token and asserting exit `2`
  with no token material in stderr.

The Search writer's own HTTP behaviour is already covered by
`servers/search-console-writer/tests/test_http_protocol.py`.

## What is still outstanding (do not read this as "deployed")

- **The images were never built** — no Docker daemon was available. Validate with
  `docker compose -f deploy/docker-compose.writers.yml config` and a real
  `build` before trusting the packaging.
- **No provider credential was exercised.** The Play writer remains
  **unreleased**: `PLAY_WRITER_ENABLE_RELEASE_GATE` and
  `PLAY_WRITER_ENABLE_ARTIFACT_UPLOAD` default off, so this deployment cannot
  stage or publish a release or upload binaries as configured.
- **Service-account scope is unverified here.** The Search writer needs the
  Search Console **read/write** `webmasters` scope and the Play writer the
  Android Publisher scope plus access to each allowlisted package; confirm the
  actual grant on the deployed identity rather than assuming it.
- **The gateway is unchanged.** Registering these servers in LiteLLM (private
  address, scoped upstream token), verifying the network alias is reachable from
  the gateway container, and confirming authenticated calls through the gateway
  are deployment-time steps this repository does not perform.
- **Network placement is a placement, not a firewall.** Only the token
  authorises a request; do not describe the writers as "reachable only by the
  gateway".
- **No hashes are pinned** on `deploy/constraints.txt`; images are rebuildable to
  the same versions but not to the same bytes.