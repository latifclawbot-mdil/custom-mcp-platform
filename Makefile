# Developer entry points for the custom MCP platform.
#
# Every target reports the real exit code: no pipeline is allowed to mask a
# failure, which is why the test targets run pytest directly rather than
# through a pager or a `tail`.
SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

PY := .venv/bin/python
LIBS := packages/mcp_http_transport/src:servers/play-console/src:servers/search-console/src

.DEFAULT_GOAL := help
.PHONY: help venv sync test test-http test-fast test-upstream test-play test-search integrity lint clean images

help:
	@echo "make sync        create/refresh .venv from the pinned lock"
	@echo "make test        run the platform test suite (real HTTP, isolation, artifacts)"
	@echo "make test-http   only the authenticated HTTP protocol/security tests"
	@echo "make test-fast   unit tests only (no server subprocesses)"
	@echo "make test-upstream  run each vendored server's own original suite"
	@echo "make integrity   check the vendored trees against the committed manifest"
	@echo "make images      build both container images (needs docker)"
	@echo "make clean       remove caches and bytecode"

venv:
	uv venv --python 3.11 .venv
	uv sync --all-groups

sync:
	uv sync --all-groups

test:
	PYTHONPATH="$(LIBS)" $(PY) -m pytest tests/ -q -p no:cacheprovider --timeout=180

test-http:
	PYTHONPATH="$(LIBS)" $(PY) -m pytest tests/test_http_security.py tests/test_http_protocol.py tests/test_isolation.py -q -p no:cacheprovider --timeout=180

test-fast:
	PYTHONPATH="$(LIBS)" $(PY) -m pytest tests/test_auth_unit.py tests/test_app_config.py tests/test_deployment_artifacts.py -q -p no:cacheprovider

# Hermetic vendored-source check: compares the working tree against the
# committed manifest, with no dependency on any upstream checkout. Fails if a
# file changed without regenerating docs/integrity-manifest.json.
integrity:
	$(PY) scripts/gen_integrity_manifest.py --check

# The vendored servers keep their own suites. They are run here against the
# copied source, with the shared transport on the path, so the "existing MCP
# behaviour is preserved" claim is checked by the original tests and not only
# by new ones.
test-upstream: test-play test-search

test-play:
	cd servers/play-console && PYTHONPATH="src:../../packages/mcp_http_transport/src" ../../$(PY) -m pytest -q -p no:cacheprovider

test-search:
	cd servers/search-console && PYTHONPATH="src:../../packages/mcp_http_transport/src" ../../$(PY) -m pytest -q -p no:cacheprovider

images:
	docker build -f deploy/Dockerfile.play-console -t custom-mcp/play-console:local .
	docker build -f deploy/Dockerfile.search-console -t custom-mcp/search-console:local .

clean:
	find . -path ./.venv -prune -o -name '__pycache__' -type d -print -exec rm -rf {} +
	find . -path ./.venv -prune -o -name '*.pyc' -type f -print -delete
	rm -rf .pytest_cache servers/*/.pytest_cache servers/*/*.egg-info