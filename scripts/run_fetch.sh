#!/bin/bash
# Run the fetch MCP server (scripts/fetch_server.py: the reference mcp-server-fetch tool over
# streamable HTTP, every request answered on its own) so the gateway can federate it next to pantry:
#   http://$FETCH_HOST:$FETCH_PORT/mcp   (POST, JSON responses; GET /healthz reports the settings)
#
# mcp-server-fetch gets its own venv ($CONTEXTFORGE_HOME/.venv-fetch): it pins its own mcp SDK (< 2)
# and HTML-to-markdown stack, which should not share ContextForge's dependency set. fetch_server.py
# runs in that venv and imports mcp-server-fetch's functions.
#
# Inputs (env; FETCH_* also read from $CONTEXTFORGE_HOME/.env, the environment wins):
#   CONTEXTFORGE_HOME              runtime directory (default ../contextforge)
#   FETCH_SERVER_VERSION           mcp-server-fetch version to install (default 2026.8.18)
#   FETCH_HOST, FETCH_PORT         where the server listens (default 127.0.0.1:9100)
#   FETCH_IGNORE_ROBOTS_TXT        true: do not check robots.txt (default false: robots.txt is honoured)
#   FETCH_ALLOW_PRIVATE_ADDRESSES  true: also fetch loopback/private/link-local addresses (default false)
#   FETCH_USER_AGENT               User-Agent, printable ASCII (default: the reference's ModelContextProtocol/1.0 UA)
#   FETCH_TIMEOUT                  seconds per fetch call (default 30)
#   FETCH_LOG_LEVEL                log level (default info)
# Idempotent: installs only when the pinned version is missing. When something already answers on the
# port it exits 0 only if that is this server with exactly these settings (same code, versions, robots,
# address, user-agent and timeout settings); otherwise it says what differs and exits 1.
# The server gets a scrubbed environment (PATH, HOME, locale, TMPDIR, CA settings), not the caller's:
# a process that reads arbitrary web pages has no business holding API keys or tokens.
# Runs in the foreground; background it like run.sh.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
HOME_DIR=${CONTEXTFORGE_HOME:-$(cd "$REPO/.." && pwd)/contextforge}
cd "$HOME_DIR"

# Only the FETCH_* keys from the gateway's .env: the fetch server has no use for ContextForge's
# secrets, so they are not read into this environment.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$value" in \"*\"|\'*\') value=${value:1:${#value}-2} ;; esac  # quoted, as `. .env` would read it
    [ -n "${!key+x}" ] || export "$key=$value"
  done < <(grep -E '^FETCH_[A-Z_]+=' .env || true)
fi
VERSION=${FETCH_SERVER_VERSION:-2026.8.18}
HOST=${FETCH_HOST:-127.0.0.1}
PORT=${FETCH_PORT:-9100}

flag() {  # flag <NAME> <--option>: append --option to args when $NAME is true; refuse anything but a boolean
  local value=${!1:-false}
  case "$value" in
    true|1|yes) args+=("$2") ;;
    false|0|no|"") ;;
    *) echo "$1 must be true or false, got '$value'" >&2; exit 1 ;;
  esac
}
args=(--host "$HOST" --port "$PORT" --timeout "${FETCH_TIMEOUT:-30}" --log-level "${FETCH_LOG_LEVEL:-info}")
flag FETCH_IGNORE_ROBOTS_TXT --ignore-robots-txt
flag FETCH_ALLOW_PRIVATE_ADDRESSES --allow-private-addresses
[ -z "${FETCH_USER_AGENT:-}" ] || args+=(--user-agent "$FETCH_USER_AGENT")

installed=$(.venv-fetch/bin/python -c "import importlib.metadata as m; print(m.version('mcp-server-fetch'))" \
  2>/dev/null || true)
if [ "$installed" != "$VERSION" ]; then
  [ -d .venv-fetch ] || python3.12 -m venv .venv-fetch
  .venv-fetch/bin/pip install -q --upgrade pip
  .venv-fetch/bin/pip install -q "mcp-server-fetch==$VERSION"
  echo "mcp-server-fetch $VERSION installed in $HOME_DIR/.venv-fetch"
fi

# The environment the server runs with: an allowlist, passed through only when set. PATH matters
# (mcp-server-fetch converts HTML with Node.js's Readability when `node` is on it); proxy variables
# are deliberately not passed (the address guard would judge the proxy, not the page).
clean_env=()
for name in PATH HOME LANG LC_ALL LC_CTYPE TMPDIR SSL_CERT_FILE SSL_CERT_DIR; do
  [ -z "${!name+x}" ] || clean_env+=("$name=${!name}")
done
# Leaving the proxy variables out is not enough on macOS: with none set, Python's urllib (and so
# httpx) falls back to the *system* proxy settings, and a local proxy app sends every connection to
# 127.0.0.1, which the address guard then refuses as "a connection issue". no_proxy=* makes httpx
# dial every host directly, so the guard judges the real destination.
clean_env+=("no_proxy=*" "NO_PROXY=*")
PY="$HOME_DIR/.venv-fetch/bin/python"
SERVER="$REPO/scripts/fetch_server.py"

# What /healthz should report for these settings; also validates them (user agent, timeout).
expected=$(env -i "${clean_env[@]}" "$PY" "$SERVER" "${args[@]}" --print-config) || exit 1

if running=$(curl -sf --max-time 5 "http://$HOST:$PORT/healthz" 2>/dev/null); then
  EXPECTED=$expected RUNNING=$running python3 - "$HOST:$PORT" <<'PY'
import json
import os
import sys

where = sys.argv[1]
want = json.loads(os.environ["EXPECTED"])
try:
    have = json.loads(os.environ["RUNNING"]).get("settings")
except (ValueError, AttributeError):
    have = None
if have == want:
    print(f"the fetch server already answers on http://{where} with these settings; not starting another")
    sys.exit(0)
if not isinstance(have, dict):
    print(f"something else answers on http://{where}/healthz ({os.environ['RUNNING'][:60]!r}): not this fetch "
          "server (an mcpgateway.translate bridge answers 'ok'). Stop it, then run this again.", file=sys.stderr)
    sys.exit(1)
for key in sorted(set(want) | set(have)):
    if want.get(key) != have.get(key):
        print(f"  {key}: running {have.get(key)!r}, asked for {want.get(key)!r}", file=sys.stderr)
print(f"a fetch server with other settings answers on http://{where}: stop it (the process listening "
      "on that port), then run this again", file=sys.stderr)
sys.exit(1)
PY
  exit $?
fi

echo "fetch server: $SERVER ${args[*]} -> http://$HOST:$PORT/mcp"
exec env -i "${clean_env[@]}" "$PY" "$SERVER" "${args[@]}"
