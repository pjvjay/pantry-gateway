#!/bin/bash
# Run the reference MCP fetch server (PyPI mcp-server-fetch, stdio only) behind ContextForge's
# stdio -> streamable HTTP bridge, so the gateway can federate it next to pantry:
#   http://$FETCH_HOST:$FETCH_PORT/mcp   (POST, JSON responses; /healthz answers "ok")
#
# The fetch server gets its own venv ($CONTEXTFORGE_HOME/.venv-fetch): it pins its own mcp SDK and
# HTML-to-markdown stack, which should not share ContextForge's dependency set.
#
# Inputs (env; FETCH_* also read from $CONTEXTFORGE_HOME/.env, the environment wins):
#   CONTEXTFORGE_HOME        runtime directory (default ../contextforge)
#   FETCH_SERVER_VERSION     mcp-server-fetch version to install (default 2026.8.18)
#   FETCH_HOST, FETCH_PORT   where the bridge listens (default 127.0.0.1:9100)
#   FETCH_IGNORE_ROBOTS_TXT  true passes --ignore-robots-txt (default false: robots.txt is honoured)
#   FETCH_USER_AGENT         passes --user-agent (default: the server's own ModelContextProtocol/1.0 UA)
#   FETCH_LOG_LEVEL          bridge log level (default info)
# Idempotent: installs only when the pinned version is missing, and exits 0 without starting a second
# copy when a bridge already answers on the port. Runs in the foreground; background it like run.sh.
set -euo pipefail
HOME_DIR=${CONTEXTFORGE_HOME:-$(cd "$(dirname "$0")/../.." && pwd)/contextforge}
cd "$HOME_DIR"

# Only the FETCH_* keys from the gateway's .env: the bridge and the fetch subprocess have no use for
# ContextForge's secrets, so they are not exported into this environment.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$value" in \"*\"|\'*\') value=${value:1:${#value}-2} ;; esac  # quoted, as `. .env` would read it
    [ -n "${!key+x}" ] || export "$key=$value"
  done < <(grep -E '^FETCH_[A-Z_]+=' .env || true)
fi
VERSION=${FETCH_SERVER_VERSION:-2026.8.18}
HOST=${FETCH_HOST:-127.0.0.1}
PORT=${FETCH_PORT:-9100}

cmd=$(printf '%q' "$HOME_DIR/.venv-fetch/bin/mcp-server-fetch")
case "${FETCH_IGNORE_ROBOTS_TXT:-false}" in
  true|1|yes) cmd+=" --ignore-robots-txt" ;;
  false|0|no|"") ;;
  *) echo "FETCH_IGNORE_ROBOTS_TXT must be true or false, got '$FETCH_IGNORE_ROBOTS_TXT'" >&2; exit 1 ;;
esac
[ -z "${FETCH_USER_AGENT:-}" ] || cmd+=" --user-agent $(printf '%q' "$FETCH_USER_AGENT")"

if [ ! -x .venv/bin/python ]; then
  echo "no ContextForge venv in $HOME_DIR (run scripts/install.sh first): the bridge is part of ContextForge" >&2
  exit 1
fi
installed=$(.venv-fetch/bin/python -c "import importlib.metadata as m; print(m.version('mcp-server-fetch'))" \
  2>/dev/null || true)
if [ "$installed" != "$VERSION" ]; then
  [ -d .venv-fetch ] || python3.12 -m venv .venv-fetch
  .venv-fetch/bin/pip install -q --upgrade pip
  .venv-fetch/bin/pip install -q "mcp-server-fetch==$VERSION"
  echo "mcp-server-fetch $VERSION installed in $HOME_DIR/.venv-fetch"
fi

if curl -sf "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then
  echo "a bridge already answers on http://$HOST:$PORT/healthz; not starting another"
  exit 0
fi

case "$cmd" in
  *--ignore-robots-txt*) echo "WARNING: FETCH_IGNORE_ROBOTS_TXT=true; the fetch tool will not check robots.txt" >&2 ;;
esac
echo "fetch bridge: $cmd -> http://$HOST:$PORT/mcp"
exec ./.venv/bin/python -m mcpgateway.translate --stdio "$cmd" --expose-streamable-http \
  --host "$HOST" --port "$PORT" --logLevel "${FETCH_LOG_LEVEL:-info}"
