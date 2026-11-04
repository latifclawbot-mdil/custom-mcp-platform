# Entry points for the Search Console write server.
#
# Every target reports the real exit code: pytest is invoked directly so a
# failure is never masked by a pipe.
SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

PY ?= ../../.venv/bin/python
SRC := src
TRANSPORT := ../../packages/mcp_http_transport/src

.DEFAULT_GOAL := help
.PHONY: help test test-unit test-protocol test-http test-safety

help:
	@echo "make test            run the whole writer suite"
	@echo "make test-unit       validation, allowlist, client, auth, tools"
	@echo "make test-protocol   real stdio MCP protocol against a loopback fake"
	@echo "make test-http       real HTTP transport on a loopback socket"
	@echo "make test-safety     structural no-write/no-retry/redaction checks"

test:
	PYTHONPATH="$(SRC):$(TRANSPORT)" $(PY) -m pytest -q -p no:cacheprovider

test-unit:
	PYTHONPATH="$(SRC):$(TRANSPORT)" $(PY) -m pytest -q -p no:cacheprovider \
		tests/test_allowlist.py tests/test_validation.py tests/test_client.py \
		tests/test_auth.py tests/test_tools.py tests/test_server.py

test-protocol:
	PYTHONPATH="$(SRC):$(TRANSPORT)" $(PY) -m pytest -q -p no:cacheprovider \
		tests/test_stdio_mcp.py tests/test_credential_laziness.py

test-http:
	PYTHONPATH="$(SRC):$(TRANSPORT)" $(PY) -m pytest -q -p no:cacheprovider \
		tests/test_http_protocol.py

test-safety:
	PYTHONPATH="$(SRC):$(TRANSPORT)" $(PY) -m pytest -q -p no:cacheprovider \
		tests/test_safety_properties.py tests/test_network_guard.py