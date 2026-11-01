# Vendoring

## What was copied

Both servers were copied from their own git repositories at `HEAD`, using the
committed tree only:

```bash
git -C ../google-operations-mcp/play-console   archive HEAD | tar -x -C servers/play-console
git -C ../google-operations-mcp/search-console archive HEAD | tar -x -C servers/search-console
```

`git archive` ships exactly the tracked files, so `.git/`, `.venv/`,
`.pytest_cache/`, and bytecode were never copied. That is checked rather than
asserted: `tests/test_isolation.py::test_no_vcs_or_virtualenv_state_was_copied`.

| Origin | Destination | Commits copied | Verified by |
| --- | --- | --- | --- |
| `play-console` @ `HEAD` | `servers/play-console/` | see `docs/vendoring-manifest.txt` | `docs/integrity-manifest.json` |
| `search-console` @ `HEAD` | `servers/search-console/` | see `docs/vendoring-manifest.txt` | `docs/integrity-manifest.json` |

## How integrity is verified (hermetically)

The expected content of each vendored tree is frozen in
`docs/integrity-manifest.json`: a committed map of `relative/path -> sha256`,
plus the upstream repository name and HEAD it was taken from.
`tests/test_isolation.py` re-hashes the working tree and compares it to that
manifest, so the check reads only files inside this repository.

This replaced an earlier test that hashed the vendored files against the
upstream checkouts at an **absolute path on the authoring machine**. That test
could not run in a clean `git clone`: with no originals present it either
`skip`-ped (proving nothing) or failed on a machine with a different layout, and
its result depended on mutable local state rather than on what this repository
ships. The manifest version runs identically in a fresh clone with no originals
anywhere.

Regenerate after an intentional re-vendor (which is exactly when it should
change):

```bash
python scripts/gen_integrity_manifest.py          # rewrite the manifest
python scripts/gen_integrity_manifest.py --check  # CI: fail if stale
make test
```

The manifest excludes `http_main.py` (the platform's own addition) and any
cache byproduct; `test_platform_added_files_are_the_only_extra_source` asserts
the added file is present and non-empty. A `git clone` is therefore enough to
run the full integrity check — no source repositories required.

## Adding the transport without forking the servers

Each vendored server gained **one** new file:

- `servers/play-console/src/play_console_mcp/http_main.py`
- `servers/search-console/src/search_console_mcp/http_main.py`

Nothing else in either tree was edited. That is why the original test suites
still pass unchanged (150 Play, 254 Search) and why the integrity manifest can
hash every copied file without depending on an upstream checkout.

Note what this buys: the original stdio entry points are untouched, so an
operator can still run `play-console-mcp` over stdio if they need to. The HTTP
entry point imports the same `build_server` / `create_server` function and
changes only the transport.

## Updating a vendored server

1. Copy the new committed tree over `servers/<name>/` (do not copy `.git`).
2. Re-apply nothing: `http_main.py` is a separate file, so it survives a
   re-copy only if you re-create it. Keep a copy outside the tree, or use a
   three-way merge.
3. Regenerate the integrity manifest (`python scripts/gen_integrity_manifest.py`)
   and run `make test` — the manifest check fails if the tree and manifest
   disagree, naming the files that moved, which is what you want.
4. Run `make test-upstream`.

## Dependency pins

- `pyproject.toml` (root): exact versions for `mcp`, `google-auth`, `httpx`,
  `requests`, `starlette`, `uvicorn`.
- `uv.lock`: the full resolved graph, including transitive dependencies.
- `deploy/constraints.txt`: the same graph as an install list for the images,
  generated with `uv export --format requirements.txt --no-dev --no-emit-project`.

`mcp` is pinned exactly (`==2.3.0`) on purpose: `streamable_http_app`,
`TokenVerifier`, and `AuthSettings` are all version-sensitive, and the transport
package reaches one private attribute that only exists in this release. An
upgrade is a deliberate change with a test run, not a side effect of a rebuild.

## Known gap

`deploy/constraints.txt` does not carry hashes. Close it with:

```bash
uv export --format requirements.txt --no-dev --no-emit-project --generate-hashes \
  -o deploy/constraints.txt
```

and add `--require-hashes` back to the `pip install` lines in both Dockerfiles.
This was left out because the running environment has no Docker to verify the
build after the change; a hash mismatch discovered only at deploy time would be
worse than a documented gap.