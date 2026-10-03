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
scripts/run_fetch.sh        run the reference MCP fetch server behind ContextForge's stdio bridge on :9100
scripts/register_fetch.sh   register it as the gateway 'fetch' + create the virtual server 'pantry-recipes'
scripts/make_scenarios.py   make runnable gateway scenarios for mcp-sim (tool names differ, see below)
scenarios/                  gateway-only mcp-sim scenarios (they need the fetch tool), server id left blank
tests/                      the generator and the recipe-link scenario, checked with mcp-sim's own code
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
framework so one scenario file can run direct or through any gateway; when it lands, the renaming
half of this generator goes away.

## A fetch tool next to pantry: the `pantry-recipes` server

An agent asked for "the cheapest ingredients near me for <recipe link>" has to read the page before
it can call `plan_from_text`, and the pantry server deliberately never fetches arbitrary URLs. In the
gateway topology the fetch is a separate MCP server that ContextForge federates next to pantry, so
the agent sees both toolsets through one endpoint and the pantry server's attack surface stays as
it is. The server is the reference [`mcp-server-fetch`](https://pypi.org/project/mcp-server-fetch/)
(2026.8.18). It only speaks stdio, so ContextForge's own bridge exposes it over streamable HTTP:

```bash
scripts/run_fetch.sh &          # own venv $CONTEXTFORGE_HOME/.venv-fetch on first run, then the bridge:
#   $CONTEXTFORGE_HOME/.venv/bin/python -m mcpgateway.translate \
#     --stdio "$CONTEXTFORGE_HOME/.venv-fetch/bin/mcp-server-fetch" \
#     --expose-streamable-http --host 127.0.0.1 --port 9100
curl -s http://127.0.0.1:9100/healthz                  # ok
CF_JWT_FILE=~/.contextforge_jwt scripts/register_fetch.sh
# … created gateway <id> | slug fetch | reachable True
# … tools: 15 from pantry, 1 from fetch
# … created virtual server <id>
# … the server now offers 16 tools: fetch-fetch, pantry-find-product, …
# … MCP endpoint for agents: http://127.0.0.1:4444/servers/<pantry-recipes id>/mcp
```

`register_fetch.sh` registers the bridge as the federated gateway `fetch` (Streamable HTTP, no
upstream auth) and creates the virtual server `pantry-recipes` with every pantry tool plus the fetch
tool; `pantry-sim` is not touched. Re-running it finds both by name and only rewrites the server's
tool list when the pantry or fetch tool set changed. After deploying a pantry-api whose tools
changed (new parameters on `plan_from_text`, say), run it with `REFRESH_PANTRY=true`: ContextForge
caches a gateway's tool list at registration, and the refresh updates the tools in place (same
ids), so `pantry-sim` sees the new schemas too. Settings (`FETCH_PORT`, `FETCH_SERVER_VERSION`,
`FETCH_IGNORE_ROBOTS_TXT`, `FETCH_USER_AGENT`) are in `.env.example`; `run_fetch.sh` reads only
the `FETCH_*` keys from `$CONTEXTFORGE_HOME/.env`, so ContextForge's secrets are not exported to
the process that fetches pages.

**Tool name and arguments.** The gateway is named `fetch` and so is the tool, so through ContextForge
it is **`fetch-fetch`**: `url` (required), `max_length` (characters, default 5000, below 1,000,000),
`start_index` (default 0) and `raw` (default false: readability-simplified markdown; true: the page's
HTML). It answers one text block, `Contents of <url>:` and the content, with no structured
content. A cut-off answer ends with `<error>Content truncated. Call the fetch tool with a
start_index of N to get more content.</error>`. The server's `fetch` prompt (a manual fetch that
skips robots.txt) is not part of the virtual server.

**robots.txt is honoured.** Before every fetch the server reads the site's robots.txt as
`ModelContextProtocol/1.0 (Autonomous; …)` and refuses a disallowed URL with `is_error` and the
robots.txt text. `FETCH_IGNORE_ROBOTS_TXT=true` passes `--ignore-robots-txt`; it is off by default,
`run_fetch.sh` prints a warning when it is on, and it is only for sites you are allowed to read
that way.

**What comes back for a recipe page**, measured on https://omnivorescookbook.com/mala-chicken/
(2026-10-02, through the gateway with the SDK client):

| | result |
| --- | --- |
| robots.txt | allows it (the site disallows only `/?s=`, `/page/*/?s=`, `/search/`); `/search/chicken/` is refused as above |
| markdown, default `max_length` | 14,392 characters in 3 pages (`start_index` 0, 5000, 10000), about 2-7 s per call (robots.txt, then the page) |
| the ingredient list | in the markdown, all 17 lines under its four headings, but it starts about 9,200 characters in, after the story, so it spans pages 2 and 3; one call with `max_length: 20000` returns the whole page |
| `raw: true` | 388,946 characters of HTML in one call; the schema.org `Recipe` JSON-LD (17 `recipeIngredient` strings) is one ~20,000-character block at about character 26,500 |

So an agent should read recipes as markdown with a larger `max_length`. Reaching the JSON-LD
through the tool costs about 100k tokens of raw HTML; it is better extracted client side.

**Limits of the bridge** (ContextForge 1.0.11 `mcpgateway.translate`: read in its source, then
reproduced):

* **Concurrent calls can receive each other's answers.** `POST /mcp` writes the request to the one
  stdio process and returns the first line it reads back with the same JSON-RPC id. Every client
  session numbers its requests from the same start (ContextForge opens one upstream session per
  downstream session), so concurrent calls share ids. Four concurrent sessions fetching four
  different pages: in each of three rounds through ContextForge, and two rounds directly against
  the bridge, three of the four callers got another caller's page, with no error. Until the bridge
  correlates per session, fetch one page at a time: the recipe-link scenario sets `concurrency: 1`.
* **10 seconds per call.** The bridge waits 10 s for the answer, then replies `202 Accepted`, which
  reaches the agent as `MCP server error: server answered a request with 202 Accepted`
  (`is_error`). `https://httpbin.org/delay/8` already fails that way (robots.txt plus 8 s).
* `GET /mcp` answers 405 and no `Mcp-Session-Id` is issued: no server-to-client messages, which a
  fetch server does not need.

## The recipe-link scenario

`scenarios/recipe-link-mala-chicken.yaml` is a gateway-only mcp-sim scenario: a home cook in
Vancouver pastes the mala chicken link and wants the cheapest basket at nearby stores and what they
will not find. It is written for ContextForge's tool names (`fetch-fetch`, `pantry-plan-from-text`)
and for the plan_from_text contract of the pantry-api recipe-link work (`allow_partial`, `max_km`,
`not_stocked` / `out_of_range` and a per-line `match`), so that pantry-api has to be behind the
`pantry` gateway before it runs. The committed file has a placeholder server id:

```bash
python3 scripts/make_scenarios.py --recipe "$(cat /tmp/cf_recipes_server_id.txt)"   # → scenarios/generated/
export CONTEXTFORGE_JWT=$(cat ~/.contextforge_jwt)
../mcp-sim/.venv/bin/mcpsim catalog scenarios/generated/recipe-link-mala-chicken.yaml   # 16 tools
../mcp-sim/.venv/bin/mcpsim run scenarios/generated/recipe-link-mala-chicken.yaml
```

The checks on the generator and the scenario use mcp-sim's own loader, matcher and tool filter:

```bash
../mcp-sim/.venv/bin/python -m pytest tests/
```

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
* The fetch server: `register_fetch.sh` → gateway `fetch` `reachable: true`, 1 tool (`fetch-fetch`,
  `originalName: fetch`), virtual server `pantry-recipes` with 16 tools, `pantry-sim` still 15;
  a second run changes nothing, and with one tool removed from the server by hand it puts it back
  (`15 -> 16: +1 -0`). Through the gateway with the SDK client: `tools/list` (16, pantry and fetch)
  and the `fetch-fetch` calls in the table above, robots.txt refusal included.
  `mcpsim catalog` on the generated recipe-link scenario lists the same 16 tools.

## Things that bit, so you don't have to find them again

* Every secret has a strength check at startup; `DEFAULT_USER_PASSWORD` needs 12+ characters even
  though nothing uses it here. `install.sh` generates all of them.
* `SSRF_ALLOW_LOCALHOST=true` is required when the upstream runs on the same machine; without it
  `POST /gateways` answers a masked `422 {"detail": "An error occurred, please try again."}`. The
  cause is only visible in `mcpgateway/config.py`, not in the response or the log.
* The gateway initialises the upstream once and caches its tool list (`gateway_mode: cache`); a
  pantry deployment that adds tools needs `refresh_interval_seconds`, a manual refresh
  (`POST /gateways/<id>/tools/refresh`, which `REFRESH_PANTRY=true scripts/register_fetch.sh` does)
  or a re-registration before the gateway shows them.
* A loopback upstream is stored as `localhost` whatever it was registered as
  (`http://127.0.0.1:9100/mcp` reads back as `http://localhost:9100/mcp`); compare URLs with that
  in mind.
* `mcpgateway.translate --expose-streamable-http` is a POST shim over one stdio process; per its
  source, `--stateless` and `--jsonResponse` only configure an SDK session manager that no request
  reaches. See "Limits of the bridge" above.
* `DATABASE_URL` must be absolute (`sqlite:////abs/path/mcp.db`); the UI needs
  `MCPGATEWAY_UI_ENABLED=true` and lives at `/admin`.

## Relationship to the other repositories

| repo | role |
| --- | --- |
| [pantry-api](https://github.com/pjvjay/pantry-api) | the MCP server being fronted (`/mcp`, bearer via `MCP_AUTH_TOKENS`) |
| [mcp-sim](https://github.com/pjvjay/mcp-sim) | the simulation framework; knows nothing about ContextForge, only about an MCP endpoint |
| this repo | the gateway's configuration and registration of pantry; candidate for a pin in [pantry-platform](https://github.com/pjvjay/pantry-platform) once it is deployed rather than local |

MIT licensed.
