#!/usr/bin/env bash
# Manual smoke probe: start the Play server over HTTP, then exercise the
# unauthenticated, wrong-token and valid-token paths and a tools/list call.
#
# This is a debugging aid, not part of the pytest suite. Ephemeral files go
# under $BH_AGENT_WORKSPACE / $TMPDIR, never a hardcoded /tmp path.
set -uo pipefail

PORT="${1:-34570}"
TOKEN="test-token-not-a-real-secret-0123456789"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${BH_AGENT_WORKSPACE:-${TMPDIR:-/tmp}}"
LOG="$WORK/smoke_http_${PORT}.log"
cd "$ROOT"

MCP_HTTP_AUTH_TOKEN="$TOKEN" \
MCP_HTTP_PORT="$PORT" \
MCP_HTTP_HOST=127.0.0.1 \
PLAY_CONSOLE_ALLOWED_PACKAGES=com.example.app \
PYTHONPATH="servers/play-console/src:packages/mcp_http_transport/src" \
  .venv/bin/python -m play_console_mcp.http_main > "$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

READY=0
for _ in $(seq 1 100); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then READY=1; break; fi
  sleep 0.2
done

echo "--- server log ($LOG) ---"
cat "$LOG"
if [ "$READY" != 1 ]; then
  echo "FAIL: server never listened on 127.0.0.1:$PORT" >&2
  exit 3
fi

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
URL="http://127.0.0.1:$PORT/mcp"

echo "--- no token (expect 401) ---"
curl -sS -m 10 -o "$WORK/no_auth.body" -w 'http=%{http_code}\n' -X POST "$URL" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d "$INIT"
cat "$WORK/no_auth.body" 2>/dev/null || echo "(no body)"

echo "--- wrong token (expect 401) ---"
curl -sS -m 10 -o "$WORK/wrong_auth.body" -w 'http=%{http_code}\n' -X POST "$URL" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -H 'authorization: Bearer nope-nope-nope-000000000000' \
  -d "$INIT"
cat "$WORK/wrong_auth.body" 2>/dev/null || echo "(no body)"

echo "--- valid token (expect 200 + session id) ---"
curl -sS -m 10 -o "$WORK/valid_auth.body" -D "$WORK/valid_auth.headers" -w 'http=%{http_code}\n' \
  -X POST "$URL" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -H "authorization: Bearer $TOKEN" \
  -d "$INIT"
grep -i 'mcp-session-id\|content-type' "$WORK/valid_auth.headers" || true
head -c 300 "$WORK/valid_auth.body" 2>/dev/null || echo "(no body)"
echo

SESSION="$(grep -i '^mcp-session-id' "$WORK/valid_auth.headers" | tr -d '\r' | awk '{print $2}')"
if [ -z "$SESSION" ]; then
  echo "FAIL: no Mcp-Session-Id on a successful initialize" >&2
  exit 4
fi

echo "--- tools/list on that session (expect 200) ---"
curl -sS -m 10 -o "$WORK/tools.body" -w 'http=%{http_code}\n' -X POST "$URL" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -H "authorization: Bearer $TOKEN" -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

python3 - "$WORK/tools.body" <<'PY'
import json, pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
if "data:" in text:
    text = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")][0]
payload = json.loads(text)
tools = payload["result"]["tools"]
print("tool_count =", len(tools))
print("first_names =", [tool["name"] for tool in tools][:6])
print("all_read_only =", all((t.get("annotations") or {}).get("readOnlyHint") is True for t in tools))
PY