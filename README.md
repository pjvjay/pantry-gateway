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
scenarios/                  gateway-only mcp-sim scenarios (scenario v2; they need the fetch tool), server id left blank
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
gateway (its allow/deny globs, instructions, observers and scenario v2 fields name the originals),
so the variants are generated:

```bash
python3 scripts/make_scenarios.py ../mcp-sim/scenarios/pantry "$(cat /tmp/cf_server_id.txt)" scenarios/generated
export CONTEXTFORGE_JWT=$(cat ~/.contextforge_jwt)
../mcp-sim/.venv/bin/mcpsim catalog scenarios/generated/cheapest-penne.yaml
MCPSIM_DRY_RUN=1 ../mcp-sim/.venv/bin/mcpsim run scenarios/generated/cheapest-penne.yaml
../mcp-sim/.venv/bin/mcpsim run scenarios/generated/cheapest-penne.yaml
```

The generator renames tool names wherever a scenario can mention one: `role`, `goal`,
`instructions`, `expected_outcome.text`, `observers`, and the v2 fields `title` (which also gains
" (gateway)", so the runner tells the variants apart), `user_instructions`, every string in
`context` (its `details` values included), `expected_behavior` and `agent.notes`. `category` is
kept, so a gateway variant sits next to its direct original in the runner. It drops every model pin
that is not an Anthropic model, under any key of `models` and on any observer (mcp-sim's pantry
scenarios before feat/simulate pin `planner: ollama:command-r7b`), and says so on stderr: all of a
simulation's model calls go to the Anthropic API, and the simulate skill's config chooses every
role's model. `agent.skill` is read the way mcp-sim reads it: an `env:` reference is kept, `~`
expands, and a relative path is made absolute against the source file, because the generated file
lives somewhere else. Long prose (`user_instructions`, checklist items) comes out as block scalars
wrapped at 100 columns, and a file is written only if it parses back to exactly the scenario.

The generated files are git-ignored because they embed this installation's server id.
[mcp-sim#5](https://github.com/pjvjay/mcp-sim/issues/5) tracks a `server.tool_names` mapping in the
framework so one scenario file can run direct or through any gateway; when it lands, the renaming
half of this generator goes away.

## A fetch tool next to pantry: the `pantry-recipes` server

An agent asked for "the cheapest ingredients near me for <recipe link>" has to read the page before
it can call `plan_from_text`, and the pantry server deliberately never fetches arbitrary URLs. In the
gateway topology the fetch is a separate MCP server that ContextForge federates next to pantry, so
the agent sees both toolsets through one endpoint. A fetched page is untrusted input: anyone can
write a recipe page, and what it says reaches the agent as tool output. So that endpoint carries
only pantry's read-only tools. The write tools (`submit_origin_evidence`, and
`review_origin_submission`, which approves a submission into the origin data the plan tools honour,
with no check that the reviewer is not the submitter) stay on servers without fetch, such as
`pantry-sim`, so instructions planted in a page cannot reach them through this endpoint.

The tool is the reference [`mcp-server-fetch`](https://pypi.org/project/mcp-server-fetch/)'s
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
# … tools: 13 read-only of 15 from pantry, 1 from fetch
# …   left out: pantry-review-origin-submission (readOnlyHint=False)
# …   left out: pantry-submit-origin-evidence (readOnlyHint=False)
# … created virtual server <id>
# … the server now offers 14 tools: fetch-fetch, pantry-find-product, …
# … MCP endpoint for agents: http://127.0.0.1:4444/servers/<pantry-recipes id>/mcp
```

`register_fetch.sh` registers the fetch server as the federated gateway `fetch` (Streamable HTTP, no
upstream auth) and creates the virtual server `pantry-recipes` with the fetch tool and every pantry
tool whose annotations say `readOnlyHint: true` (a new pantry tool without the hint stays out until
it declares itself read-only); `pantry-sim` is not touched. It stops with an error if the finished
server offers anything else next to fetch. Re-running it finds both by name, refreshes the `fetch`
gateway (ContextForge caches a gateway's tools and prompts, so a restarted or replaced fetch server
is read again) and only rewrites the server's tool list when the pantry or fetch tool set changed.
After deploying a pantry-api whose tools changed (new parameters on `plan_from_text`, say), run it
with `REFRESH_PANTRY=true`: the refresh updates the tools in place (same ids), so `pantry-sim` sees
the new schemas too.

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
downtown Vancouver pastes the mala chicken link and wants the cheapest basket at stores within 5 km
(they walk or take transit) and what they will not find. It is written for ContextForge's tool names
(`fetch-fetch`, `pantry-plan-from-text`) and for the plan_from_text contract of the pantry-api
recipe-link work, pjvjay/pantry-api#24 (`allow_partial`, `max_km`, `not_stocked` / `out_of_range` /
`skipped`, a per-line `match`, and one purchase per product with `also_lines`), so that pantry-api
has to be behind the `pantry` gateway before it runs, in live mode: DEMO_MODE's stand-in parser
reads 16 of the page's 17 lines as not stocked, and even given clean names its stand-in selector
prices salt as crushed tomatoes. The gateway's copy of the pantry tools must also be fresh enough
that `pantry-plan-from-text` lists `allow_partial` and `max_km`: the recipe-shopper skill the agent
runs on stops and reports a stale gateway without them (`REFRESH_PANTRY=true
scripts/register_fetch.sh` refreshes it). The committed file has a placeholder server id:

```bash
python3 scripts/make_scenarios.py --recipe "$(cat /tmp/cf_recipes_server_id.txt)"   # → scenarios/generated/
export CONTEXTFORGE_JWT=$(cat ~/.contextforge_jwt)
export RECIPE_SHOPPER_SKILL=../pantry-platform/pantry-api/skills/recipe-shopper/SKILL.md   # see "Scenario v2"
../mcp-sim/.venv/bin/mcpsim catalog scenarios/generated/recipe-link-mala-chicken.yaml   # 14 tools
../mcp-sim/.venv/bin/mcpsim run scenarios/generated/recipe-link-mala-chicken.yaml
```

What it checks follows from the 161-product catalog pantry-api#24 seeds (pantry-db's, 39077a1):
it stocks all 17 of the page's ingredients, and every product at all four stores. From the default
point downtown, or the context's 49.2827,-123.1207, with `max_km: 5`, a correct answer buys every
line at Pantry Mart Downtown (0.3 km), GreenLeaf Grocers Kitsilano (2.9) or ValueFoods East Van
(4.1), never at MegaSave Richmond (13.9 km), where the plan without `max_km` buys the sesame seeds.
Its `out_of_range` is empty, because Pantry Mart Downtown sells everything, so an entry there has
been made up. Its `not_stocked` and `skipped` are whatever the planner's parse and selector leave out
(nothing, in either recorded plan), so they are checked for shape, and the shopping buddy's
`hidden_gap` holds the answer to the plan's own lists. Recipe lines that chose the same product are
one purchase: whether the two Sichuan peppercorn lines (ground, whole) share one depends on the
selector's pick, so a correct answer has 16 or 17 lines (at least 15 are required), each copied
with its `also_lines`, and the shopping buddy's `double_counted_purchase` fails an answer that lists
or prices a shared purchase twice. Its `misquoted_total` fails a basket total that differs from
`summary.total_cost`, or the recommended trip's total (travel included, so always different) passed
off as it; the skill reports the trip too, so a trip total labelled as the trip's is allowed. A code
check fails a run whose last plan was not limited to 5 km (`stores ≤ 5 km` in the plan's notes). On
this catalog, distance
alone can never put an ingredient out of range from the default point: either a store within range
sells everything or no store is in range and the plan fails. So `out_of_range` honesty needs a
price or diet constraint, or a catalog where stores differ, and `not_stocked` honesty needs a page
with an ingredient the catalog lacks (it has no white pepper, lemongrass, MSG, potato starch or
saffron). Both are follow-ups, not this scenario.

The runs may overlap (`concurrency` is mcp-sim's default again): `concurrency: 1` was there only
for the old stdio bridge.

`tests/test_scenarios.py` checks the generator and the scenario with mcp-sim's own loader, matcher
and tool filter. Its correct answers are not hand-written. `tests/fixtures/mala-chicken-plan-5km.json`
and `mala-chicken-plan-5km-latlon.json` are real `plan_from_text` results for the 17 page lines with
`allow_partial` and `max_km: 5`, from the server's default point and from the context's coordinates,
each recorded once against pantry-api#24 (d2949ae) in live mode, and the test copies them into a
final_result as the scenario instructs. Both answers pass every check (one has the shared
peppercorn purchase, 16 lines; the other 17); honest partial answers pass; and each wrong answer
fails exactly the check that names it (a Richmond store, an invented out-of-range entry, dropped
lines, a line without `also_lines`, a missing `skipped` and so on).

```bash
../mcp-sim/.venv/bin/python -m pytest tests/
```

## Scenario v2 and the simulate skill

mcp-sim runs simulations through one skill, `skills/simulate`, with every LLM call (planner, agent,
simulated user, observers, judge) on the Anthropic API. Its scenarios add optional v2 fields, and
the recipe-link scenario uses all of them:

| field | in the recipe-link scenario |
| --- | --- |
| `category`, `title` | "Recipe links", "Mala chicken from a recipe link, within 5 km": the runner's group and display name |
| `user_instructions` | the simulated user's brief, in the second person: a downtown Vancouver home cook who pastes the link, wants what to buy where and the total within 5 km, and what they will not find; impatient with hedging, says yes to a check-in; has only the link, so never makes up an ingredient list, and ends the conversation if the page cannot be read |
| `context` | `desktop web`, `Vancouver, BC (49.2827, -123.1207)`, `en`, plus details; shown in the runner and given to the simulated user, not to the agent (no `agent_visible`): the user says where they are |
| `expected_behavior` | seven items the judge grades one by one: reads the page with fetch-fetch; plans the verbatim lines with `allow_partial` and `max_km 5`, at the default location or the place the user named; quotes `total_cost` exactly, any trip total labelled as the trip's; lists every line with product, store and price, never a shared purchase twice; names every not_stocked, out_of_range and skipped ingredient, or says there are none; never presents a generic match as the recipe's ingredient; invents nothing |
| `agent` | `skill: env:RECIPE_SHOPPER_SKILL`, the recipe-shopper SOP from pantry-api#24, and `notes` saying this environment cannot run the skill's extractor script, so the page is read with fetch-fetch (markdown, `max_length` 20000: one call returns all 17 ingredient lines) |

The instructions and the checklist follow the skill on location: the agent may pass the
coordinates of a place the user names ("downtown Vancouver" located as 49.2827,-123.1207, the
second recorded plan) and must not pass coordinates for a place nobody named.

The checklist is written so that a correct agent can pass it on the plans this page really gets.
mcp-sim's judge passes an item only on a quoted piece of the transcript, fails one with "no
evidence", and passes a prohibition ("never X") when the transcript shows the agent did not do X.
No real plan for this page has had a generic match, and one had no shared purchase, so those items
are prohibitions; an item phrased "calls every generic match a substitution" would have nothing to
quote and fail a correct agent. Likewise the skill reports the recommended trip, whose total adds
travel and never equals `total_cost`, so the total item and the shopping buddy's `misquoted_total`
accept a trip total labelled as the trip's.

There is no `models` block: the simulate skill's `config.yaml` chooses every role's model, and a
scenario pin would override it. `make_scenarios.py --recipe` refuses a scenario here that pins a
non-Anthropic model.

`agent.skill` names the SKILL.md the agent under test runs on. mcp-sim reads it when it loads the
scenario, so `RECIPE_SHOPPER_SKILL` has to point at pantry-api's skill whenever the scenario is
loaded (`mcpsim catalog`, `mcpsim run`, the runner):

```bash
export RECIPE_SHOPPER_SKILL=/path/to/pantry-api/skills/recipe-shopper/SKILL.md
```

or write the path into the generated file instead, so it runs without the variable:

```bash
python3 scripts/make_scenarios.py --skill-dir ../pantry-platform/pantry-api/skills/recipe-shopper \
  --recipe "$(cat /tmp/cf_recipes_server_id.txt)"
```

`--skill-dir` takes the skill's directory (or its SKILL.md), checks that the SKILL.md has
frontmatter naming the skill, and replaces `env:RECIPE_SHOPPER_SKILL` with the absolute path,
changing only that line (the comments stay). It works for the mcp-sim variants too. Without it, the
generator leaves the reference and, if `RECIPE_SHOPPER_SKILL` does not point at a file, says so on
stderr.

The tests check the v2 fields against that contract themselves (`v2_problems` in
`tests/test_scenarios.py`): mcp-sim's scenario model before feat/simulate refuses keys it does not
know, so against it the tests hand mcp-sim the v1 part of each file. Once mcp-sim's model knows the
v2 keys, the same tests load the whole file with mcp-sim's loader, with `RECIPE_SHOPPER_SKILL`
pointed at pantry-api's skill when it is checked out in `../pantry-platform` (a stand-in otherwise),
and check that the agent's SOP is the recipe-shopper skill. Until then, an mcp-sim older than
feat/simulate cannot load this scenario at all (`mcpsim catalog` and `mcpsim run` included). When
feat/simulate lands, run `tests/test_scenarios.py` against it again: the v2-loader check
(`test_the_agent_runs_on_pantry_apis_recipe_shopper`) skips on older mcp-sim.

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

## What was verified on 2026-10-03: pantry-recipes and the scenario

* `register_fetch.sh` → `tools: 13 read-only of 15 from pantry, 1 from fetch`, the submit and
  review tools `left out (readOnlyHint=False)`, `tool list updated (16 -> 14: +0 -2)`; `pantry-sim`
  still 15. `mcpsim catalog` on the generated scenario lists 14 tools, no write tool.
* pantry-api#24 (59bbe27) on a fresh 160-product SQLite DB: `plan_from_text` on the page's 17 lines
  with `allow_partial` and no `max_km` buys the toasted sesame seeds at MegaSave Richmond (13.9 km);
  with `max_km: 5` (live, the recorded fixture) all 17 lines are planned at the three stores
  within 5 km, `not_stocked` and `out_of_range` empty, notes `stores ≤ 5 km`, total 56.90.
  `store_products` has 640 rows: every one of the 160 products at every one of the 4 stores. In
  DEMO_MODE the same 17 lines with `max_km: 5` give 1 planned line and 16 not stocked.
* The reviewer's live plan without `max_km`, copied as instructed: the old spec failed it only on
  `not_stocked $len >= 1`, which no honest answer could meet; the new spec fails it only on
  `lines[*].store` (the Richmond line).
* `../mcp-sim/.venv/bin/python -m pytest tests/`: 59 passed.

## What was verified on 2026-10-03: scenario v2

* pantry-api#24 at d2949ae on a fresh SQLite DB: 161 products, 4 stores, 644 store offers.
  `plan_from_text` on the page's 17 lines, live, `allow_partial`: with `max_km: 5` from the default
  point, 16 lines (the selector chose the whole peppercorns for both Sichuan peppercorn lines, one
  purchase, `also_lines: [13]`), total 52.66; with `max_km: 5` at 49.2827,-123.1207, 17 lines
  (ground peppercorns for line 10), total 58.00. Both have `not_stocked`, `out_of_range` and
  `skipped` empty, the `stores ≤ 5 km` note, and only the three stores within 5 km (both recorded
  in `tests/fixtures/`). Without `max_km`: the toasted sesame seeds at MegaSave Richmond (2.64).
* `fetch` on the running fetch server (:9100) with `max_length: 20000`: 14,392 characters, not
  truncated, all 17 ingredient lines (three of them with the page's `\*Footnote` markdown escape).
* An independent probe, separate from the recordings: two more live `plan_from_text` calls on a
  fresh SQLite seed at d2949ae with the recorded `recipe_text`, `allow_partial` and `max_km: 5`, one
  from the default point and one at 49.2827,-123.1207. Both came back with 16 lines (the shared
  whole-peppercorn purchase), total 52.66, every recipe line 1-17 covered, only the three stores
  within 5 km, `not_stocked` / `out_of_range` / `skipped` empty, no generic match, and a trip total
  (56.47, 56.50) different from `total_cost`. Copied as instructed, each passes all 15 checks of
  `expected_outcome.json` under mcp-sim's matcher.
* `pantry-recipes` on this machine (a direct MCP `tools/list` through ContextForge) lists 14 tools,
  but its `pantry-plan-from-text` takes only `recipe_text`, `lat`, `lon`, `exclude_origin`,
  `preference` and `verbose`: no `allow_partial` or `max_km`, because the `pantry` gateway still
  fronts an older pantry-api. A live run of this scenario needs pantry-api#24 there, in live mode,
  and a tool refresh first.
* `tests/test_scenarios.py` against mcp-sim's feat/execution-planner (scenario model without v2):
  85 passed, 1 skipped (the v2-loader check). Against the feat/simulate scenario model in progress
  (v2 keys known, `agent.skill` read at load time): 86 passed. Each of 26 deliberate breakages fails
  a test: a v2 field left unrenamed (`user_instructions`, `expected_behavior`, `agent.notes`,
  `context`), local model pins kept, or dropped only under the roles mcp-sim knows today, prose not
  wrapped, a relative skill path left relative, `~` not expanded, the skill path unquoted, the
  `also_lines` or `skipped` check removed, an Ollama planner put back, "never invent coordinates"
  put back, the context's location moved, `agent_visible` set, the category or the agent's notes
  changed, Richmond allowed, the generic or shared-purchase item made a positive claim again, the
  trip total no longer told apart (instructions, checklist, `misquoted_total`), and the simulated
  user told to paste a list it does not have.
* The generated files, made the way the README says: `--recipe` (the env reference kept, a note on
  stderr while `RECIPE_SHOPPER_SKILL` is unset), `--skill-dir ../pantry-platform/pantry-api/skills/recipe-shopper
  --recipe` (exactly one line differs, the skill line), mcp-sim's six pantry scenarios at
  feat/execution-planner (six `dropped models.planner: ollama:command-r7b` notes) and the six in
  progress on feat/simulate (v2 fields renamed, no pins left to drop); no "ollama" anywhere in the
  output and no un-prefixed tool name in any string. With the feat/simulate loader in progress and
  no `RECIPE_SHOPPER_SKILL`, the `--skill-dir` file loads with category, title, context
  (`agent_visible` false) and seven checklist items intact, the agent's SOP `recipe-shopper`
  (frontmatter stripped), and every role on an Anthropic model.

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
* `POST /gateways/<id>/tools/refresh` writes back the gateway row it loaded, so a description
  changed by `PUT /gateways/<id>` just before it is silently lost. `register_fetch.sh` refreshes
  first and updates the description after.
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
