"""Checks for the scenario generator and the recipe-link scenario.

Run with mcp-sim's venv, so the scenario is validated by mcp-sim's own loader, matcher and tool
filter rather than by a copy of their rules:

    ../mcp-sim/.venv/bin/python -m pytest tests/
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import pathlib

import pytest

mcpsim_scenario = pytest.importorskip("mcpsim.scenario")
from mcpsim.matcher import match  # noqa: E402
from mcpsim.mcpclient import Catalog, ToolInfo  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scenarios" / "recipe-link-mala-chicken.yaml"
MCP_SIM_PANTRY = ROOT.parent / "mcp-sim" / "scenarios" / "pantry"
SERVER_ID = "2d405c155dea4b26b767fa7473ded966"

spec = importlib.util.spec_from_file_location("make_scenarios", ROOT / "scripts" / "make_scenarios.py")
make_scenarios = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_scenarios)

# The 'pantry-recipes' virtual server's tools/list as ContextForge returned it on 2026-10-03, after
# register_fetch.sh kept only pantry's read-only tools next to fetch.
GATEWAY_TOOLS = [
    "pantry-list-recipes", "pantry-get-recipe", "pantry-list-products", "pantry-find-product",
    "pantry-get-product", "pantry-plan-recipe", "pantry-plan-from-text", "pantry-plan-week",
    "pantry-get-product-origins", "pantry-rank-products-by-origin", "pantry-origin-triage",
    "pantry-list-origin-submissions", "pantry-pipeline-status", "fetch-fetch",
]
WRITE_TOOLS = ["pantry-submit-origin-evidence", "pantry-review-origin-submission"]

# A real plan_from_text result for the page, recorded once against pantry-api#24 (see the file's
# "recorded" block), and the final_result the scenario asks for: the summary's lines, not_stocked and
# out_of_range copied as returned, each line with its ingredient, product, store, price and match.
RECORDED = json.loads((ROOT / "tests" / "fixtures" / "mala-chicken-plan-5km.json").read_text())
PLAN = RECORDED["summary"]
GOOD = {
    "total_cost": PLAN["total_cost"],
    "lines": [{k: line[k] for k in ("ingredient", "product", "store", "price", "match")} for line in PLAN["lines"]],
    "not_stocked": PLAN["not_stocked"],
    "out_of_range": PLAN["out_of_range"],
}

# pantry-api's seeded stores (pantry_planner/storeseed.py) and default point (DEFAULT_LAT/LON), for
# checking the scenario's store list against the distance the persona gives.
STORES = {"Pantry Mart Downtown": (49.2820, -123.1180), "GreenLeaf Grocers Kitsilano": (49.2680, -123.1550),
          "ValueFoods East Van": (49.2620, -123.0700), "MegaSave Richmond": (49.1550, -123.1350)}
HOME = (49.28, -123.12)


def km(a, b):
    """pantry-api's own distance (nlsearch/sql_builder.py DIST_EXPR): equirectangular, 111 km a degree."""
    return math.hypot((a[0] - b[0]) * 111.0, (a[1] - b[1]) * 111.0 * math.cos(math.radians(a[0])))


def failing(final_result) -> set[str]:
    scenario = mcpsim_scenario.load_scenario(TEMPLATE)
    return {m.path for m in match(scenario.expected_outcome.json, final_result) if not m.passed}


def changed(**edits):
    result = copy.deepcopy(GOOD)
    for key, fn in edits.items():
        fn(result) if callable(fn) else result.__setitem__(key, fn)
    return result


# --- the generator ------------------------------------------------------------------------------


def test_recipe_fills_the_server_id_and_keeps_comments(tmp_path):
    written = make_scenarios.recipe(SERVER_ID, tmp_path)
    assert [p.name for p in written] == ["recipe-link-mala-chicken.yaml"]
    text = written[0].read_text()
    assert make_scenarios.PLACEHOLDER not in text
    assert "# Matched against the agent's final_result, for pantry-db's 160-product catalog" in text
    scenario = mcpsim_scenario.load_scenario(written[0])
    assert scenario.name == "recipe-link-mala-chicken"
    assert scenario.server.http.url == f"http://127.0.0.1:4444/servers/{SERVER_ID}/mcp"
    assert scenario.server.http.bearer_env == "CONTEXTFORGE_JWT"


def test_recipe_refuses_a_server_id_that_is_not_one(tmp_path):
    with pytest.raises(SystemExit):
        make_scenarios.recipe("abc/../../etc", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not MCP_SIM_PANTRY.is_dir(), reason="needs an mcp-sim checkout next to this repo")
def test_direct_scenarios_still_get_gateway_names(tmp_path):
    make_scenarios.main(str(MCP_SIM_PANTRY), "31a69179a9c9489daed673ab5eac40b1", str(tmp_path))
    penne = mcpsim_scenario.load_scenario(tmp_path / "cheapest-penne.yaml")
    assert penne.name == "cheapest-penne-gateway"
    assert penne.tools.deny == ["pantry-submit-*", "pantry-review-*"]
    assert penne.instructions[0].startswith("Use pantry-find-product ")
    assert penne.server.http.url.endswith("/servers/31a69179a9c9489daed673ab5eac40b1/mcp")


# --- the recipe-link scenario --------------------------------------------------------------------


def test_tool_policy_offers_fetch_and_every_pantry_read_and_plan_tool_but_no_write_tool():
    scenario = mcpsim_scenario.load_scenario(TEMPLATE)
    catalog = Catalog(tools=[ToolInfo(name=n) for n in GATEWAY_TOOLS])
    allowed = catalog.filtered(scenario.tools.allow, scenario.tools.deny).tool_names()
    assert sorted(allowed) == sorted(GATEWAY_TOOLS)
    # The deny list still keeps the write tools out on a server that offers them (pantry-recipes did).
    with_writes = Catalog(tools=[ToolInfo(name=n) for n in GATEWAY_TOOLS + WRITE_TOOLS])
    assert sorted(with_writes.filtered(scenario.tools.allow, scenario.tools.deny).tool_names()) == sorted(GATEWAY_TOOLS)
    assert [t.name for t in catalog.select(scenario.tools.initial)] == ["pantry-plan-from-text", "fetch-fetch"]
    assert scenario.models.planner == "ollama:command-r7b"
    assert scenario.models.agent == mcpsim_scenario.DEFAULT_AGENT_MODEL


def test_runs_may_overlap_now_that_fetches_cannot_cross():
    """concurrency: 1 was there only for the old stdio bridge; the scenario now takes mcp-sim's default."""
    assert "concurrency" not in TEMPLATE.read_text()
    assert mcpsim_scenario.load_scenario(TEMPLATE).concurrency == mcpsim_scenario.Scenario.model_fields[
        "concurrency"].default


def test_the_recorded_plan_is_the_one_the_scenario_asks_for():
    """GOOD is only evidence if the recorded call is what the instructions make the agent send."""
    args = RECORDED["arguments"]
    assert (args["allow_partial"], args["max_km"]) == (True, 5)
    page_lines = [ln for ln in args["recipe_text"].splitlines() if ln.startswith("- ")]
    assert len(page_lines) == 17 and page_lines[0].startswith("- 1 lb boneless skinless chicken thigh")
    assert "stores ≤ 5 km" in PLAN["notes"]
    assert (len(PLAN["lines"]), PLAN["not_stocked"], PLAN["out_of_range"]) == (17, [], [])


def test_the_store_list_is_exactly_the_stores_within_5_km():
    scenario = mcpsim_scenario.load_scenario(TEMPLATE)
    listed = scenario.expected_outcome.json["lines[*].store"]["$in"]
    assert sorted(listed) == sorted(name for name, at in STORES.items() if km(HOME, at) <= 5)
    assert round(km(HOME, STORES["MegaSave Richmond"]), 1) == 13.9


def test_the_clerk_fails_a_plan_that_ignored_the_5_km():
    """The code check reads the plan's structured result; max_km shows up in its notes."""
    scenario = mcpsim_scenario.load_scenario(TEMPLATE)
    clerk = next(o for o in scenario.observers if o.name == "recipe_clerk")
    cond = next(c for c in clerk.conditions if c.id == "planned_within_5_km")
    where = cond.check.tool_result["where"]
    assert all(m.passed for m in match(where, {"summary": PLAN, "full": None}))
    no_km = copy.deepcopy(PLAN)
    no_km["notes"] = [n for n in no_km["notes"] if not n.startswith("stores ≤")]
    assert not all(m.passed for m in match(where, {"summary": no_km, "full": None}))
    ten_km = copy.deepcopy(PLAN)
    ten_km["notes"] = [n.replace("≤ 5 km", "≤ 10 km") for n in ten_km["notes"]]
    assert not all(m.passed for m in match(where, {"summary": ten_km, "full": None}))
    assert (cond.otherwise.flag, cond.otherwise.fail) == ("distance_ignored", True)


def test_the_recorded_plan_copied_as_instructed_passes_every_check():
    assert failing(GOOD) == set()


def test_an_honest_partial_answer_passes_too():
    """If the planner's parse misses an ingredient, an answer that reports it is right, not wrong."""
    def drop_cilantro(r):
        r["lines"] = [ln for ln in r["lines"] if ln["ingredient"] != "cilantro"]
        r["not_stocked"] = [{"ingredient": "cilantro", "reason": "not in the catalog", "suggestions": []}]
    assert failing(changed(partial=drop_cilantro)) == set()


SESAME = next(i for i, ln in enumerate(GOOD["lines"]) if ln["product"] == "Toasted Sesame Seeds 100g")


@pytest.mark.parametrize(("result", "paths"), [
    (changed(total_cost=0), {"total_cost"}),
    (changed(total_cost=str(PLAN["total_cost"])), {"total_cost"}),  # no coercion: a quoted number is not a cost
    (changed(lines=lambda r: r.__setitem__("lines", r["lines"][:14])), {"lines"}),  # three ingredients dropped
    (changed(lines=lambda r: r["lines"][1].pop("store")), {"lines[*].store"}),
    # The sesame seeds from Richmond (13.9 km), where the plan without max_km buys them.
    (changed(lines=lambda r: r["lines"][SESAME].update(store="MegaSave Richmond", price=2.64)), {"lines[*].store"}),
    (changed(lines=lambda r: r["lines"][2].__setitem__("price", 0)), {"lines[*].price"}),
    (changed(lines=lambda r: r["lines"][1].__setitem__("match", "relaxed")), {"lines[*].match"}),
    (changed(lines=lambda r: r["lines"][0].__setitem__("ingredient", "1 lb tofu")), {"lines[any].ingredient"}),
    (changed(not_stocked=[{"ingredient": "Sichuan peppercorns"}]), {"not_stocked[*].reason"}),
    (changed(not_stocked=[{"reason": "not in the catalog"}]), {"not_stocked[*].ingredient"}),
    (changed(not_stocked=lambda r: r.pop("not_stocked")),
     {"not_stocked", "not_stocked[*].ingredient", "not_stocked[*].reason"}),
    (changed(out_of_range=lambda r: r.pop("out_of_range")), {"out_of_range"}),
    # Nothing can be out of range within 5 km on this catalog: an entry there is made up.
    (changed(out_of_range=[{"ingredient": "toasted sesame seeds", "reason": "nearest offer 13.9 km away"}]),
     {"out_of_range"}),
])
def test_each_wrong_answer_fails_exactly_the_check_that_names_it(result, paths):
    assert failing(result) == paths
