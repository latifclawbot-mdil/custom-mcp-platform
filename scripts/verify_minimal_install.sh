#!/usr/bin/env bash
# Verify that a *minimal* interpreter — one with only deploy/constraints.txt
# plus the two local packages installed, no dev dependencies — can serve the
# MCP endpoint. This is the closest local proxy for the container image, whose
# install sequence is exactly these two pip commands.
#
# Usage: scripts/verify_minimal_install.sh <python-from-minimal-venv> <port>
set -uo pipefail

PY="${1:?path to the minimal venv python}"
PORT="${2:-34610}"
TOKEN="minimal-install-token-0123456789abcdef"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${BH_AGENT_WORKSPACE:-${TMPDIR:-/tmp}}"
LOG="$WORK/verify_minimal_${PORT}.log"
cd "$ROOT"

MCP_HTTP_AUTH_TOKEN="$TOKEN" \
MCP_HTTP_PORT="$PORT" \
MCP_HTTP_HOST=127.0.0.1 \
GSC_ALLOWED_SITES=sc-domain:example.com \
  "$PY" -m search_console_mcp.http_main > "$LOG" 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null' EXIT

READY=0
for _ in $(seq 1 100); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then READY=1; break; fi
  sleep 0.2
done
if [ "$READY" != 1 ]; then
  echo "FAIL: minimal install did not start:" >&2
  cat "$LOG" >&2
  exit 3
fi

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"minimal","version":"0"}}}'
URL="http://127.0.0.1:$PORT/mcp"

NO_AUTH=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST "$URL" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d "$INIT")
AUTH=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST "$URL" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -H "authorization: Bearer $TOKEN" -d "$INIT")

echo "no_token_status=$NO_AUTH authenticated_status=$AUTH"
if [ "$NO_AUTH" != "401" ] || [ "$AUTH" != "200" ]; then
  echo "FAIL: expected 401 without a token and 200 with one" >&2
  cat "$LOG" >&2
  exit 4
fi
echo "OK: minimal install serves the authenticated MCP endpoint"