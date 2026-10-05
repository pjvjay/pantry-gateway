#!/bin/bash
# Mint an admin JWT for ContextForge's REST API and MCP endpoints; prints it to stdout (pipe to a 0600 file).
set -euo pipefail
HOME_DIR=${CONTEXTFORGE_HOME:-$(cd "$(dirname "$0")/../.." && pwd)/contextforge}
cd "$HOME_DIR"; set -a; . ./.env; set +a
.venv/bin/python -m mcpgateway.utils.create_jwt_token --username "${PLATFORM_ADMIN_EMAIL:-admin@example.com}" \
  --exp "${JWT_EXP_MINUTES:-10080}" --secret "$JWT_SECRET_KEY" 2>/dev/null | tail -1
