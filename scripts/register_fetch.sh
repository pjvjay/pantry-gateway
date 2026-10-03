#!/bin/bash
# Register the fetch MCP server (scripts/run_fetch.sh) in ContextForge as the federated gateway 'fetch',
# and expose it together with every pantry tool as the virtual server 'pantry-recipes': one endpoint
# where an agent can read a recipe page and then plan its basket. 'pantry-sim' is left unchanged.
#
# Inputs (env): CF_URL (default http://127.0.0.1:4444), CF_JWT_FILE (admin JWT), FETCH_MCP_URL
# (default http://127.0.0.1:${FETCH_PORT:-9100}/mcp), CF_STATE_DIR (default /tmp; the server id is
# written to $CF_STATE_DIR/cf_recipes_server_id.txt). Needs the 'pantry' gateway (register_pantry.sh).
# REFRESH_PANTRY=true first asks ContextForge to re-read the pantry server's tool list (it caches it at
# registration): needed after deploying a pantry-api whose tools changed. Tools are updated in place by
# name, so 'pantry-sim' keeps its members and simply sees the new schemas too.
# Idempotent: re-running finds the gateway and the server by name, refreshes the fetch gateway's cached
# tools and prompts (so a restarted or replaced fetch server is re-read) and its description, and when
# the tool set has changed since, updates the server's tool list instead of creating a second server.
set -euo pipefail
export REFRESH_PANTRY=${REFRESH_PANTRY:-false}
export CF_URL=${CF_URL:-http://127.0.0.1:4444}
export FETCH_MCP_URL=${FETCH_MCP_URL:-http://127.0.0.1:${FETCH_PORT:-9100}/mcp}
export CF_STATE_DIR=${CF_STATE_DIR:-/tmp}
: "${CF_JWT_FILE:?path to the ContextForge admin JWT}"
export CF_JWT_FILE

echo "== gateway health"
curl -sf "$CF_URL/health" >/dev/null || { echo "ContextForge does not answer on $CF_URL/health" >&2; exit 1; }
echo ok
echo "== fetch server health"
curl -sf "${FETCH_MCP_URL%/mcp}/healthz" || {
  echo "nothing answers on ${FETCH_MCP_URL%/mcp}/healthz; start scripts/run_fetch.sh first" >&2; exit 1; }
echo

# The JWT is read from the file inside Python, so it never appears on a command line.
python3 -u - <<'PY'
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

CF = os.environ["CF_URL"].rstrip("/")
JWT = pathlib.Path(os.environ["CF_JWT_FILE"]).expanduser().read_text().strip()
FETCH_URL = os.environ["FETCH_MCP_URL"]
STATE = pathlib.Path(os.environ["CF_STATE_DIR"])
SERVER_NAME = "pantry-recipes"
FETCH_DESCRIPTION = ("Fetch server (pantry-gateway scripts/fetch_server.py: mcp-server-fetch's fetch tool over "
                     "stateless streamable HTTP): reads a public web page as markdown or raw HTML; honours "
                     "robots.txt and refuses loopback and private addresses unless started otherwise")


def call(method, path, body=None):
    req = urllib.request.Request(CF + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {JWT}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"{method} {path}: {e.reason}")


def items(d, *keys):
    if isinstance(d, list):
        return d
    for k in keys:
        if k in d:
            return d[k]
    return []


def gateway(name):
    return next((g for g in items(call("GET", "/gateways"), "gateways", "items") if g.get("name") == name), None)


def same_url(a, b):
    """ContextForge stores a loopback upstream as localhost whatever it was registered as."""
    def norm(u):
        return (u or "").rstrip("/").replace("://127.0.0.1:", "://localhost:").replace("://[::1]:", "://localhost:")
    return norm(a) == norm(b)


def tools_of(gw_id):
    return items(call("GET", f"/tools?gateway_id={gw_id}&limit=0"), "tools", "items")


print("== register fetch as a federated gateway (STREAMABLEHTTP, no upstream auth)")
fetch = gateway("fetch")
if fetch is None:
    fetch = call("POST", "/gateways", {
        "name": "fetch",
        "description": FETCH_DESCRIPTION,
        "url": FETCH_URL,
        "transport": "STREAMABLEHTTP",
    })
    print("created gateway", fetch.get("id"), "| slug", fetch.get("slug"), "| reachable", fetch.get("reachable"))
else:
    print(f"gateway 'fetch' already registered: {fetch['id']} | url {fetch.get('url')} "
          f"| reachable {fetch.get('reachable')}")
    if not same_url(fetch.get("url"), FETCH_URL):
        print(f"WARNING: it points at {fetch.get('url')}, not {FETCH_URL}; delete it in /admin to re-register",
              file=sys.stderr)
    # ContextForge caches a gateway's tools and prompts; re-read them from whatever now serves FETCH_URL.
    r = call("POST", f"/gateways/{fetch['id']}/tools/refresh?include_prompts=true")
    print("== refreshed the fetch gateway:", {k: v for k, v in r.items() if k != "gatewayId"})
    # After the refresh, not before: ContextForge 1.0.11's refresh writes back the gateway row it loaded,
    # description included, so a description changed just before it is lost.
    if gateway("fetch").get("description") != FETCH_DESCRIPTION:
        updated = call("PUT", f"/gateways/{fetch['id']}", {"description": FETCH_DESCRIPTION})
        print("description updated" if updated.get("description") == FETCH_DESCRIPTION
              else f"WARNING: description not updated: {updated.get('description')!r}")

pantry = gateway("pantry")
if pantry is None:
    sys.exit("no gateway named 'pantry': run scripts/register_pantry.sh first")

if os.environ["REFRESH_PANTRY"].lower() in ("1", "true", "yes"):
    r = call("POST", f"/gateways/{pantry['id']}/tools/refresh")
    print("== refreshed pantry's tool list:", {k: v for k, v in r.items() if "tool" in k.lower()})

# Registration can come back 'pending' (async lifecycle): wait for the fetch tools to appear.
for _ in range(30):
    fetch_tools = tools_of(fetch["id"])
    if fetch_tools:
        break
    time.sleep(1)
else:
    sys.exit(f"gateway 'fetch' ({fetch['id']}) federated no tools after 30 s; check the bridge log")
pantry_tools = tools_of(pantry["id"])

print(f"== tools: {len(pantry_tools)} from pantry, {len(fetch_tools)} from fetch")
for t in pantry_tools + fetch_tools:
    print(f"  {t.get('name'):42s} original={t.get('originalName') or t.get('original_name')!s:28s} id={t.get('id')}")
wanted = [t["id"] for t in pantry_tools + fetch_tools]

print(f"== virtual server '{SERVER_NAME}' with every pantry tool and the fetch tool(s)")
server = next((s for s in items(call("GET", "/servers"), "servers", "items") if s.get("name") == SERVER_NAME), None)
if server is None:
    created = call("POST", "/servers", {"server": {
        "name": SERVER_NAME,
        "description": "Pantry tools plus a web fetch tool: read a recipe link, then plan its basket",
        "associated_tools": wanted}})
    server = created.get("server", created)
    print("created virtual server", server["id"])
else:
    have = set(server.get("associatedToolIds") or server.get("associated_tool_ids") or [])
    if have == set(wanted):
        print(f"virtual server '{SERVER_NAME}' exists with the same {len(wanted)} tools: {server['id']}")
    else:
        call("PUT", f"/servers/{server['id']}", {"associated_tools": wanted})
        print(f"virtual server '{SERVER_NAME}' exists: {server['id']}; tool list updated "
              f"({len(have)} -> {len(wanted)}: +{len(set(wanted) - have)} -{len(have - set(wanted))})")

check = call("GET", f"/servers/{server['id']}/tools")
names = sorted(t.get("name") for t in check)
print(f"== the server now offers {len(names)} tools: {', '.join(names)}")
(STATE / "cf_recipes_server_id.txt").write_text(server["id"] + "\n")
print(f"\nMCP endpoint for agents: {CF}/servers/{server['id']}/mcp   (Authorization: Bearer <ContextForge JWT>)")
print(f"server id written to {STATE / 'cf_recipes_server_id.txt'}")
PY
