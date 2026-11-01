# Verification evidence

What was actually run, in what environment, and what it proved. Commands are
reproducible from a fresh `git clone` unless marked otherwise. Nothing here is
a claim about a live deployment.

Environment for every run below: Linux, Python 3.11.15, the repository's
`.venv` built from `uv.lock`. **Docker is not installed in this environment**,
so no container was built or started here; the container secrets/permissions
behaviour is covered by `scripts/verify_secret_mount.sh` (which skips cleanly
without Docker) and by the static artifact tests.

## 1. Full platform suite

```
$ make test
PYTHONPATH="packages/mcp_http_transport/src:servers/play-console/src:servers/search-console/src" \
  .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --timeout=180
122 passed in ~52s
```

Zero skips (`pytest -rs` reports nothing skipped). The previous suite had 96
tests, two of which (`test_vendored_files_match_the_original_committed_tree`,
`test_original_repositories_are_unmodified`) could silently `skip` when the
upstream checkouts were absent, which meant they proved nothing on a clean
machine. Those are gone; the integrity check never skips.

## 2. Hermeticity: the suite passes in a clean clone with the originals absent

This is the property the old tests could not offer. Procedure:

1. Copy the working tree to a scratch directory (no `.venv`, no caches).
2. `git init && git add -A && git commit` (88 tracked files — confirms
   `.gitignore` does not hide needed source).
3. `git clone` that repository to a second scratch directory.
4. `uv venv && uv sync --all-groups` inside the clone (fresh environment).
5. **Temporarily move the upstream checkouts away** so no test can read them.
6. Run the full suite.
7. Restore the originals and confirm they are untouched.

Observed:

```
=== temporarily hiding upstream originals ===
moved <path>/google-operations-mcp -> <scratch>/originals_hidden_*
ls: cannot access '<path>/google-operations-mcp': No such file or directory
=== FULL suite with originals ABSENT ===
122 passed in 52.08s
=== restore originals ===
restored
play-console
search-console
```

After restore, `git -C <repo> status --porcelain` was empty for both upstream
repositories and their `HEAD` shas still matched the values recorded in
`docs/integrity-manifest.json` (`74d2d2f6…` for play-console, `06aa911e…` for
search-console). The source repositories were not modified.

## 3. Integrity manifest

```
$ make integrity
.venv/bin/python scripts/gen_integrity_manifest.py --check
integrity manifest OK (53 files across 2 servers)
```

- play-console: 26 files hashed (the upstream tracked count; `http_main.py` is
  the platform's own addition and is excluded, asserted separately).
- search-console: 27 files hashed, same reasoning.

The manifest lives in `docs/integrity-manifest.json` and is the only source of
expected hashes — no absolute path, no sibling checkout, no git remote.

## 4. Vendored servers' own suites (unchanged behaviour)

```
$ make test-upstream
play-console:    150 passed in 18.32s
search-console:  254 passed in 40.31s
```

Run against the copied source with the shared transport on `PYTHONPATH`, so the
"existing MCP behaviour is preserved" claim is checked by the original tests,
not only by new ones.

## 5. Secret bind-mount permission rule

Verified two ways:

- **Static (pytest):** `tests/test_deployment_artifacts.py` asserts each
  `file:`-sourced secret declares only `file:` (no `uid`/`gid`/`mode`, which
  Compose ignores for a bind mount) and that the compose header documents the
  requirement.
- **Live (script, needs Docker):** `scripts/verify_secret_mount.sh` runs a
  container as uid 10001 against a `root:0600` file and a `10001:0400` file and
  prints whether each is readable. In this environment it prints
  `SKIP: docker is not installed`.

The rule itself matches Docker's documented behaviour: a `file:` secret is
delivered by bind mount, and bind mounts do not remap uid/gid/mode. Independent
Docker-host testing (outside this repository) observed root:0600 unreadable and
chown 10001:10001 / chmod 0400 readable, which is why the compose file and
`docs/security.md` §10 state the operator requirement rather than setting
`mode:`.

## 6. Network topology

Static assertions in `tests/test_deployment_artifacts.py`:

- both services join `coolify` (`external: true`, `name: coolify`) **and**
  `mcp-internal`;
- each service has exactly one alias on `coolify` equal to its service name;
- that alias appears in its `MCP_HTTP_ALLOWED_HOSTS`;
- neither service has `ports` or `hostname`, and both `expose` only `8080`.

Not verified here: that the `coolify` network exists on the target host, that
LiteLLM is attached to it, or that a real handshake over it succeeds. Those are
host properties and require the live deployment; see `docs/security.md`
"Not verified".

## 7. Token-newline behaviour (the corrected claim)

`docs/security.md` §3 was wrong: it said a trailing newline is always an error.
The two paths differ, and this was executed to confirm it:

- `load_expected_token({"MCP_HTTP_AUTH_TOKEN": "a"*32 + "\n"})` →
  `AuthConfigError: ... has leading or trailing whitespace` (env var: rejected).
- `resolve_token({"MCP_HTTP_AUTH_TOKEN_FILE": <file with trailing \n>})` →
  returns the 32-char token (file: `.strip()` applied, accepted).

`tests/test_auth_unit.py` asserts both, so the documentation and the code can no
longer drift apart silently.
