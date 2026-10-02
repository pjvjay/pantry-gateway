# pantry-gateway

An MCP gateway in front of the pantry platform's MCP server, built on
[IBM ContextForge](https://github.com/IBM/mcp-context-forge): one endpoint, one federated tool
catalog, upstream credentials held by the gateway, per-token access and usage in one place. It is
the topology an agent meets in deployment (client → gateway → servers), so it is also the
topology [mcp-sim](https://github.com/pjvjay/mcp-sim) simulations should run against.

This repository holds the configuration, the scripts and the documentation. The ContextForge
installation itself (its virtualenv, `.env` with secrets, SQLite database, logs) is a runtime
directory, `$CONTEXTFORGE_HOME` (default `../contextforge`), and is never committed.

```
scripts/install.sh          create $CONTEXTFORGE_HOME/.venv, install ContextForge, write .env with generated secrets
scripts/run.sh              start the gateway on 127.0.0.1:4444 (loads $CONTEXTFORGE_HOME/.env)
scripts/mint_jwt.sh         print an admin JWT (REST API and MCP endpoints use the same token)
scripts/register_pantry.sh  register the pantry server as a federated gateway + create the virtual server
scripts/make_scenarios.py   derive gateway variants of mcp-sim's pantry scenarios (tool names differ, see below)
.env.example                every setting the gateway needs, secrets left for install.sh to generate
docs/                       what was learned setting it up
```

## Quickstart (macOS or Linux, Python 3.12, no Docker)

```bash
git clone https://github.com/pjvjay/pantry-gateway && cd pantry-gateway
scripts/install.sh                         # ContextForge 1.0.11 into ../contextforge
scripts/run.sh &                           # http://127.0.0.1:4444/health → {"status":"healthy", …}
scripts/mint_jwt.sh > ~/.contextforge_jwt && chmod 600 ~/.contextforge_jwt
```

Start the pantry API with bearer auth on its `/mcp` (from a pantry-api checkout):

```bash
DEMO_MODE=1 DB_URL=sqlite:////tmp/pantry-gateway.db MCP_AUTH_TOKENS="contextforge:<32+ random chars>" \
  .venv/bin/uvicorn pantry_planner.api:app --host 127.0.0.1 --port 8000
```

Register it and expose a virtual server:

```bash
CF_JWT_FILE=~/.contextforge_jwt PANTRY_MCP_TOKEN=<the same 32+ chars> scripts/register_pantry.sh
# … created gateway <id> | slug pantry | reachable True
# … 15 tools
# … MCP endpoint for agents: http://127.0.0.1:4444/servers/<server-id>/mcp
```

Any MCP client can now use that endpoint with `Authorization: Bearer <JWT>`. For Claude Code:

```bash
claude mcp add --transport http pantry-gateway http://127.0.0.1:4444/servers/<server-id>/mcp \
  --header "Authorization: Bearer $(cat ~/.contextforge_jwt)"
```

## Running mcp-sim simulations through the gateway

ContextForge names a federated tool `<gateway-slug>-<tool-with-dashes>`: `find_product` becomes
`pantry-find-product`. A scenario written for the direct server therefore does not match through the
gateway (its allow/deny globs, instructions and observers name the originals), so the variants are
generated:

```bash
python3 scripts/make_scenarios.py ../mcp-sim/scenarios/pantry "$(cat /tmp/cf_server_id.txt)" scenarios/generated
export CONTEXTFORGE_JWT=$(cat ~/.contextforge_jwt)
../mcp-sim/.venv/bin/mcpsim catalog scenarios/generated/cheapest-penne.yaml
MCPSIM_DRY_RUN=1 ../mcp-sim/.venv/bin/mcpsim run scenarios/generated/cheapest-penne.yaml
../mcp-sim/.venv/bin/mcpsim run scenarios/generated/cheapest-penne.yaml --models planner=ollama:command-r7b
```

The generated files are git-ignored because they embed this installation's server id.
[mcp-sim#5](https://github.com/pjvjay/mcp-sim/issues/5) tracks a `server.tool_names` mapping in the
framework so one scenario file can run direct or through any gateway; when it lands, this generator
goes away.

## What was verified on 2026-10-02

* Registration: `POST /gateways` with `transport: STREAMABLEHTTP`, `auth_type: bearer` and the pantry
  token → `reachable: true`; `GET /tools?gateway_id=…` → 15 tools, each with `name` (prefixed) and
  `originalName`; `POST /servers` with all 15 tool ids → virtual server `pantry-sim`.
* MCP through the gateway with the SDK client: `initialize` (server `mcp-streamable-http`),
  `tools/list` (15), `tools/call pantry-find-product {"query": "penne", "limit": 1}` → the same
  answer the pantry server gives directly (`match: direct`, Penne Rigate 500g, GreenLeaf Grocers
  Kitsilano, $1.97). The pantry bearer never leaves the gateway; the client presents only the JWT.
* mcp-sim: `mcpsim catalog` and a dry run through the gateway, ten real tool calls, first one
  `pantry-find-product(query="penne")`, no write tool touched.

## Things that bit, so you don't have to find them again

* Every secret has a strength check at startup; `DEFAULT_USER_PASSWORD` needs 12+ characters even
  though nothing uses it here. `install.sh` generates all of them.
* `SSRF_ALLOW_LOCALHOST=true` is required when the upstream runs on the same machine; without it
  `POST /gateways` answers a masked `422 {"detail": "An error occurred, please try again."}`. The
  cause is only visible in `mcpgateway/config.py`, not in the response or the log.
* The gateway initialises the upstream once and caches its tool list (`gateway_mode: cache`); a
  pantry deployment that adds tools needs `refresh_interval_seconds` or a re-registration before
  the gateway shows them.
* `DATABASE_URL` must be absolute (`sqlite:////abs/path/mcp.db`); the UI needs
  `MCPGATEWAY_UI_ENABLED=true` and lives at `/admin`.

## Relationship to the other repositories

| repo | role |
| --- | --- |
| [pantry-api](https://github.com/pjvjay/pantry-api) | the MCP server being fronted (`/mcp`, bearer via `MCP_AUTH_TOKENS`) |
| [mcp-sim](https://github.com/pjvjay/mcp-sim) | the simulation framework; knows nothing about ContextForge, only about an MCP endpoint |
| this repo | the gateway's configuration and registration of pantry; candidate for a pin in [pantry-platform](https://github.com/pjvjay/pantry-platform) once it is deployed rather than local |

MIT licensed.
