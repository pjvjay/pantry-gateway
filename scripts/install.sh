#!/bin/bash
# Install IBM ContextForge from PyPI into $CONTEXTFORGE_HOME/.venv (default ../contextforge). No Docker needed.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
HOME_DIR=${CONTEXTFORGE_HOME:-$(cd "$REPO/.." && pwd)/contextforge}
VERSION=${CONTEXTFORGE_VERSION:-1.0.11}
mkdir -p "$HOME_DIR" && cd "$HOME_DIR"
[ -d .venv ] || python3.12 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q "mcp-contextforge-gateway==$VERSION"
if [ ! -f .env ]; then
  cp "$REPO/.env.example" .env
  # generated secrets: ContextForge enforces minimum lengths at startup
  .venv/bin/python - <<'PY'
import pathlib, re, secrets
p = pathlib.Path(".env"); s = p.read_text()
for key, n in (("JWT_SECRET_KEY", 48), ("AUTH_ENCRYPTION_SECRET", 32), ("BASIC_AUTH_PASSWORD", 18),
               ("PLATFORM_ADMIN_PASSWORD", 18), ("DEFAULT_USER_PASSWORD", 18)):
    s = re.sub(rf"^{key}=.*$", f"{key}={secrets.token_urlsafe(n)}", s, flags=re.M)
s = s.replace("sqlite:///CONTEXTFORGE_HOME/mcp.db", f"sqlite:///{pathlib.Path.cwd()}/mcp.db")
p.write_text(s)
PY
  chmod 600 .env
  echo "wrote $HOME_DIR/.env with generated secrets (keep it out of git)"
fi
echo "ContextForge $VERSION installed in $HOME_DIR"
