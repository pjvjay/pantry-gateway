#!/bin/bash
# Run ContextForge from $CONTEXTFORGE_HOME (default ../contextforge): loads its .env, listens on 127.0.0.1:4444.
set -euo pipefail
HOME_DIR=${CONTEXTFORGE_HOME:-$(cd "$(dirname "$0")/../.." && pwd)/contextforge}
cd "$HOME_DIR"
set -a; . ./.env; set +a
exec ./.venv/bin/mcpgateway --host "${HOST:-127.0.0.1}" --port "${PORT:-4444}"
