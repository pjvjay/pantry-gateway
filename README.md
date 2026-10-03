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
scripts/fetch_server.py     the reference fetch tool (mcp-server-fetch's code) as a stateless streamable-HTTP MCP server
scripts/run_fetch.sh        run it on :9100 in its own venv, with a scrubbed environment
scripts/register_fetch.sh   register it as the gateway 'fetch' + create the virtual server 'pantry-recipes'
scripts/make_scenarios.py   make runnable gateway scenarios for mcp-sim (tool names differ, see below)
scenarios/                  gateway-only mcp-sim scenarios (they need the fetch tool), server id left blank
tests/                      the generator and the recipe-link scenario (with mcp-sim's own code), the fetch server
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
it is. The tool is the reference [`mcp-server-fetch`](https://pypi.org/project/mcp-server-fetch/)'s
(2026.8.18), served over streamable HTTP by `scripts/fetch_server.py`, a small server on the MCP
Python SDK that imports mcp-server-fetch's own code (see "Why not mcpgateway.translate" below for
why it is not the reference server behind ContextForge's stdio bridge any more):

```bash
scripts/run_fetch.sh &          # own venv $CONTEXTFORGE_HOME/.venv-fetch on first run, then:
#   env -i PATH=… HOME=… $CONTEXTFORGE_HOME/.venv-fetch/bin/python scripts/fetch_server.py \
#     --host 127.0.0.1 --port 9100 --timeout 30 --log-level info
curl -s http://127.0.0.1:9100/healthz   # {"status":"ok","settings":{"ignore_robots_txt":false, …}}
CF_JWT_FILE=~/.contextforge_jwt scripts/register_fetch.sh
# … created gateway <id> | slug fetch | reachable True
# … tools: 15 from pantry, 1 from fetch
# … created virtual server <id>
# … the server now offers 16 tools: fetch-fetch, pantry-find-product, …
# … MCP endpoint for agents: http://127.0.0.1:4444/servers/<pantry-recipes id>/mcp
```

`register_fetch.sh` registers the fetch server as the federated gateway `fetch` (Streamable HTTP, no
upstream auth) and creates the virtual server `pantry-recipes` with every pantry tool plus the fetch
tool; `pantry-sim` is not touched. Re-running it finds both by name, refreshes the `fetch` gateway
(ContextForge caches a gateway's tools and prompts, so a restarted or replaced fetch server is read
again) and only rewrites the server's tool list when the pantry or fetch tool set changed. After
deploying a pantry-api whose tools changed (new parameters on `plan_from_text`, say), run it with
`REFRESH_PANTRY=true`: the refresh updates the tools in place (same ids), so `pantry-sim` sees the
new schemas too.

**Settings** (`.env.example`; `run_fetch.sh` reads only the `FETCH_*` keys from
`$CONTEXTFORGE_HOME/.env`, and the environment wins): `FETCH_PORT`, `FETCH_HOST`,
`FETCH_SERVER_VERSION` (the mcp-server-fetch to install), `FETCH_IGNORE_ROBOTS_TXT`,
`FETCH_ALLOW_PRIVATE_ADDRESSES`, `FETCH_USER_AGENT` (printable ASCII, since it is sent as an HTTP
header; anything else is refused at start), `FETCH_TIMEOUT` (seconds per call, default 30) and
`FETCH_LOG_LEVEL`. The settings go to the server as arguments, never through a shell string. The
server process gets a scrubbed environment: `PATH`, `HOME`, the locale, `TMPDIR` and CA settings
when set, nothing else, so neither ContextForge's secrets nor whatever the caller's shell holds
(API keys, tokens) reach the process that reads web pages. When something already answers on the
port, `run_fetch.sh` compares its `/healthz` settings (code hash, versions, robots, addresses, user
agent, timeout) with the ones asked for: the same, it exits 0; different, it names each difference
and exits 1; not this server at all (the old bridge answers a bare `ok`), it says so and exits 1.

**Tool name and arguments.** The gateway is named `fetch` and so is the tool, so through ContextForge
it is **`fetch-fetch`**: `url` (required), `max_length` (characters, default 5000, below 1,000,000),
`start_index` (default 0) and `raw` (default false: readability-simplified markdown; true: the page's
HTML). It answers one text block, `Contents of <url>:` and the content, with no structured
content. A cut-off answer ends with `<error>Content truncated. Call the fetch tool with a
start_index of N to get more content.</error>`. The name, description and input schema are the
reference server's, and so are the answers and error texts (`tests/test_fetch_server.py` runs the
same twelve calls against both and compares them). The only new answers are the address refusal and
the deadline below, and `Unknown tool` for another tool name (the reference treats any name as
fetch). The reference's `fetch` prompt, a manual fetch that skips robots.txt, is not served.

**robots.txt is honoured.** Before every fetch the server reads the site's robots.txt as
`ModelContextProtocol/1.0 (Autonomous; …)` and refuses a disallowed URL with `is_error` and the
robots.txt text. `FETCH_IGNORE_ROBOTS_TXT=true` passes `--ignore-robots-txt`; it is off by default,
the server logs a warning when it is on, and it is only for sites you are allowed to read that way.
As in the reference, only the requested URL's robots.txt is read, not a redirect target's.

**Only public addresses.** A fetch tool reads whatever URL it is given, and an agent takes URLs from
pages anyone can write. The reference server will read `http://127.0.0.1:11434/api/tags` (Ollama),
`http://127.0.0.1:4444/health` (ContextForge) or a cloud metadata address and hand the answer to
the agent; ContextForge's SSRF protection covers gateway registration, not the URLs a federated
server fetches. This server refuses loopback, private (RFC 1918, ULA), link-local (169.254.0.0/16,
metadata included), carrier-grade NAT, unspecified, reserved and multicast addresses. The check is
made on every TCP connection the fetch makes (robots.txt, the page, each redirect hop) against the
address actually dialled, after resolving the name itself, so a public name that resolves or
redirects inward is refused as well. A URL that names such a host directly is refused before any
request: `Refusing to fetch http://127.0.0.1:11434/api/tags: refused to connect to 127.0.0.1
(127.0.0.1): not a public internet address. …`. `FETCH_ALLOW_PRIVATE_ADDRESSES=true` turns this
off (the tests use it for their local fixtures); proxy variables are not passed to the server,
since the guard would then judge the proxy rather than the page.

**What comes back for a recipe page**, measured on https://omnivorescookbook.com/mala-chicken/
(2026-10-02 through the old bridge; the markdown re-checked on 2026-10-03 through the new server,
identical, 1.5 s for the whole page):

| | result |
| --- | --- |
| robots.txt | allows it (the site disallows only `/?s=`, `/page/*/?s=`, `/search/`); `/search/chicken/` is refused as above |
| markdown, default `max_length` | 14,335 characters of page (14,392 with the `Contents of …` line) in 3 pages (`start_index` 0, 5000, 10000), about 2-7 s per call through the old bridge (robots.txt, then the page) |
| the ingredient list | in the markdown, all 17 lines under its four headings, but it starts about 9,150 characters in, after the story, so it spans pages 2 and 3; one call with `max_length: 20000` returns the whole page |
| `raw: true` | 388,946 characters of HTML in one call; the schema.org `Recipe` JSON-LD (17 `recipeIngredient` strings) is one ~20,000-character block at about character 26,500 |

So an agent should read recipes as markdown with a larger `max_length`. Reaching the JSON-LD
through the tool costs about 100k tokens of raw HTML; it is better extracted client side.

**How the server answers.** The SDK's streamable-HTTP transport runs in stateless mode: every POST
gets its own transport and server session, so a reply can only go back on the request that asked
for it, whatever JSON-RPC id the caller chose. Replies are JSON (no SSE stream); `GET /mcp` answers
405 and no `Mcp-Session-Id` is issued, as before. Each call has one deadline (`FETCH_TIMEOUT`, 30 s:
robots.txt, download and conversion together, below ContextForge's own 60 s `TOOL_TIMEOUT`); past it
the caller gets `Failed to fetch <url>: no answer within 30 s` (`is_error`) and the abandoned fetch's
result is dropped. mcp-server-fetch's `fetch_url` runs on a worker thread, because its HTML
conversion calls Node.js's Readability through a blocking `subprocess.run` (about 1.5 s for the
recipe page above) that would otherwise stall every other request. That conversion needs `node` on
the `PATH` the server starts with; without it readabilipy falls back to its pure-Python mode and the
markdown differs. The server uses the SDK's low-level `Server`, as the reference does, rather than
FastMCP: FastMCP derives the input schema from a function signature and prefixes every error with
`Error executing tool fetch:`, and the aim is the reference's exact tool and answers.

**Why not mcpgateway.translate.** Until 2026-10-03 the reference server ran behind ContextForge
1.0.11's stdio bridge (`python -m mcpgateway.translate --stdio … --expose-streamable-http`). Its
`POST /mcp` writes the request to one stdio process and returns the first reply carrying the same
JSON-RPC id, and independent sessions number their requests from the same start, so callers
received each other's pages with `is_error=False` (#5, reproduced before the switch):

* concurrent callers: four POSTs with id 7 for four pages, and three of the four got the page that
  finished first (`mode=same: 3/4 callers got another caller's page`; with distinct ids 0/4). Four
  concurrent SDK sessions did the same in every round, three through ContextForge and two direct
  when PR #4 was written, and two more direct with this repo's tests pointed at the bridge;
* sequential callers, which `concurrency: 1` did not protect: the bridge answers `202 Accepted` after
  10 s but leaves the request in flight, and the late reply goes to whichever later request reuses
  the id. Through ContextForge, two strictly sequential sessions: `run 1: asked
  https://httpbin.org/delay/10 -> 10.1s isError=True … 202 Accepted`, then `run 2: asked
  https://httpbin.org/anything/second-run -> 0.7s isError=False got=delay/10`.

Its `--stateless` and `--jsonResponse` flags only configure an SDK session manager that no request
reaches. The same checks against the new server: 0 of 4 wrong in every round, and `run 2` gets its
own page (`run 1` now succeeds, in 10.6 s); see "What was verified on 2026-10-03: the fetch server".

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

## What was verified on 2026-10-03: the fetch server

* `tests/test_fetch_server.py` (36 tests, local fixtures on 127.0.0.1 and ::1 only): `tools/list` and
  twelve calls (markdown, both paging windows, past the end, raw, JSON, robots.txt refusal, 404,
  nothing listening, invalid arguments) answer exactly as mcp-server-fetch over stdio does; four
  concurrent sessions, three rounds, and four concurrent same-id POSTs each get their own page
  (pointed at the old bridge, the same checks gave every caller the page that finished first); an
  11 s page comes back; after a 2 s timeout the next two same-id callers get their own pages;
  non-public URLs are refused before any request, and a redirect into ::1 is refused at the
  connection; `run_fetch.sh` passes a user agent with spaces, a quote and parentheses intact from
  `.env` in a non-ASCII `CONTEXTFORGE_HOME`, keeps the caller's variables and ContextForge's secrets
  out of the server's environment (`ps eww`), exits 0 for the same settings and 1, naming the
  difference, for others, refuses something else on the port, and rejects a non-ASCII user agent,
  a non-boolean flag and a zero timeout before starting.
* On :9100: `run_fetch.sh` while the bridge still held the port → `something else answers … not
  this fetch server`, exit 1. Bridge stopped, `run_fetch.sh` started under `nohup`; the server's
  environment holds `PATH HOME TMPDIR` plus two variables macOS adds, nothing else.
  `register_fetch.sh` → the `fetch` gateway refreshed (`success: True`, 0 tools changed: the
  schema is the reference's), the bridge-era `fetch-fetch` prompt gone from ContextForge, gateway
  description updated.
* Four concurrent SDK sessions fetching httpbin pages due back in reverse order (3, 2, 1, 0 s):
  directly on :9100 and through `pantry-recipes`, three rounds each, 0 of 4 wrong every round.
  Four concurrent POSTs with id 7, two rounds: 0 of 4 wrong. The sequential case through
  ContextForge: `run 1: asked https://httpbin.org/delay/10 -> 10.6s isError=False got=delay/10`,
  `run 2: asked https://httpbin.org/anything/second-run -> 0.8s isError=False got=anything/second-run`.
* `http://127.0.0.1:11434/api/tags`, `http://127.0.0.1:4444/health`, `http://127.0.0.1:8000/openapi.json`
  and `http://localhost:11434/api/version` → `Refusing to fetch …` (the bridge had returned Ollama's
  model list and ContextForge's health). The mala chicken page through `pantry-recipes`: the same
  markdown as through the bridge, character for character; `/search/chicken/` refused by robots.txt.

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
* `mcpgateway.translate --expose-streamable-http` is a POST shim over one stdio process that
  correlates replies by JSON-RPC id alone; per its source, `--stateless` and `--jsonResponse` only
  configure an SDK session manager that no request reaches. Do not put a stdio server that more
  than one caller uses behind it; see "Why not mcpgateway.translate" above.
* In stateless mode the SDK's streamable-HTTP transport keeps a `GET /mcp` SSE stream open for good
  (nothing will ever be sent on it); `fetch_server.py` answers GET with 405 instead.
* `DATABASE_URL` must be absolute (`sqlite:////abs/path/mcp.db`); the UI needs
  `MCPGATEWAY_UI_ENABLED=true` and lives at `/admin`.

## Relationship to the other repositories

| repo | role |
| --- | --- |
| [pantry-api](https://github.com/pjvjay/pantry-api) | the MCP server being fronted (`/mcp`, bearer via `MCP_AUTH_TOKENS`) |
| [mcp-sim](https://github.com/pjvjay/mcp-sim) | the simulation framework; knows nothing about ContextForge, only about an MCP endpoint |
| this repo | the gateway's configuration and registration of pantry; candidate for a pin in [pantry-platform](https://github.com/pjvjay/pantry-platform) once it is deployed rather than local |

MIT licensed.
