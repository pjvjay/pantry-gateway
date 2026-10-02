#!/bin/bash
# Register the pantry MCP server in ContextForge as a federated gateway, expose its tools through a
# virtual server, and print the MCP endpoint an agent should use.
#
# Inputs (env): CF_URL (default http://127.0.0.1:4444), CF_JWT_FILE (admin JWT), PANTRY_MCP_URL
# (default http://127.0.0.1:8000/mcp), PANTRY_MCP_TOKEN (bearer the pantry server expects).
# Idempotent: re-running finds the existing gateway/server by name instead of duplicating them.
set -euo pipefail
CF_URL=${CF_URL:-http://127.0.0.1:4444}
PANTRY_MCP_URL=${PANTRY_MCP_URL:-http://127.0.0.1:8000/mcp}
: "${CF_JWT_FILE:?path to the ContextForge admin JWT}"
: "${PANTRY_MCP_TOKEN:?bearer token the pantry /mcp expects}"
JWT=$(cat "$CF_JWT_FILE")
auth=(-H "Authorization: Bearer $JWT" -H "Content-Type: application/json")
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "== gateway health"; curl -sf "$CF_URL/health"; echo

echo "== register pantry as a federated gateway (STREAMABLEHTTP, bearer to upstream)"
existing=$(curl -s "${auth[@]}" "$CF_URL/gateways" | j "next((g['id'] for g in (d if isinstance(d,list) else d.get('gateways', d.get('items', []))) if g.get('name')=='pantry'), '')")
if [ -z "$existing" ]; then
  resp=$(curl -s "${auth[@]}" -X POST "$CF_URL/gateways" -d "$(python3 - "$PANTRY_MCP_URL" "$PANTRY_MCP_TOKEN" <<'EOF'
import json, sys
print(json.dumps({
    "name": "pantry",
    "description": "Pantry planner MCP server (grocery planning, provenance, origin submissions)",
    "url": sys.argv[1],
    "transport": "STREAMABLEHTTP",
    "auth_type": "bearer",
    "auth_token": sys.argv[2],
}))
EOF
)")
  echo "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print('created gateway', d.get('id'), '| slug', d.get('slug'), '| reachable', d.get('reachable'))"
  gateway_id=$(echo "$resp" | j "d['id']")
else
  gateway_id=$existing; echo "gateway 'pantry' already registered: $gateway_id"
fi

echo "== tools federated from pantry"
curl -s "${auth[@]}" "$CF_URL/tools?gateway_id=$gateway_id&limit=0" > ${CF_STATE_DIR:-/tmp}/cf_tools.json
python3 - <<'EOF'
import json
d = json.load(open('${CF_STATE_DIR:-/tmp}/cf_tools.json'))
tools = d if isinstance(d, list) else d.get('tools', d.get('items', []))
print(len(tools), 'tools')
for t in tools:
    print(f"  {t.get('name'):42s} original={t.get('originalName') or t.get('original_name')!s:28s} id={t.get('id')}")
json.dump([t['id'] for t in tools], open('${CF_STATE_DIR:-/tmp}/cf_tool_ids.json', 'w'))
EOF

echo "== virtual server 'pantry-sim' with every pantry tool"
server_id=$(curl -s "${auth[@]}" "$CF_URL/servers" | j "next((s['id'] for s in (d if isinstance(d,list) else d.get('servers', d.get('items', []))) if s.get('name')=='pantry-sim'), '')")
if [ -z "$server_id" ]; then
  body=$(python3 <<'PY'
import json
ids = json.load(open('${CF_STATE_DIR:-/tmp}/cf_tool_ids.json'))
print(json.dumps({"server": {"name": "pantry-sim", "description": "All pantry tools for mcp-sim", "associated_tools": ids}}))
PY
)
  resp=$(curl -s "${auth[@]}" -X POST "$CF_URL/servers" -d "$body")
  server_id=$(echo "$resp" | j "d.get('id') or d.get('server',{}).get('id')")
  echo "created virtual server $server_id"
else
  echo "virtual server 'pantry-sim' exists: $server_id"
fi
echo
echo "MCP endpoint for agents: $CF_URL/servers/$server_id/mcp   (Authorization: Bearer <ContextForge JWT>)"
echo "$server_id" > ${CF_STATE_DIR:-/tmp}/cf_server_id.txt
