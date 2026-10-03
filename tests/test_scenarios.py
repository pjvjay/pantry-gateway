"""Checks for the scenario generator and the recipe-link scenario.

Run with mcp-sim's venv, so the scenario is validated by mcp-sim's own loader, matcher and tool
filter rather than by a copy of their rules:

    ../mcp-sim/.venv/bin/python -m pytest tests/
"""
from __future__ import annotations

import copy
import importlib.util
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

# The 'pantry-recipes' virtual server's tools/list as ContextForge returned it on 2026-10-02.
GATEWAY_TOOLS = [
    "pantry-list-recipes", "pantry-get-recipe", "pantry-list-products", "pantry-find-product",
    "pantry-get-product", "pantry-plan-recipe", "pantry-plan-from-text", "pantry-plan-week",
    "pantry-get-product-origins", "pantry-rank-products-by-origin", "pantry-origin-triage",
    "pantry-submit-origin-evidence", "pantry-list-origin-submissions",
    "pantry-review-origin-submission", "pantry-pipeline-status", "fetch-fetch",
]

# A final_result shaped like the plan_from_text contract (summary.lines with match, not_stocked,
# out_of_range). Illustrative values, not catalog prices.
GOOD = {
    "total_cost": 17.46,
    "lines": [
        {"ingredient": "1 lb boneless skinless chicken thigh", "product": "Chicken Thighs Boneless 450g",
         "store": "Pantry Mart Downtown", "price": 8.99, "match": "form"},
        {"ingredient": "1 tablespoon light soy sauce", "product": "Soy Sauce 500ml",
         "store": "ValueFoods East Van", "price": 3.49, "match": "generic"},
        {"ingredient": "1 thumb ginger", "product": "Fresh Ginger",
         "store": "GreenLeaf Grocers Kitsilano", "price": 1.99, "match": "exact"},
        {"ingredient": "5 garlic cloves", "product": "Fresh Garlic",
         "store": "Pantry Mart Downtown", "price": 2.99, "match": "form"},
    ],
    "not_stocked": [
        {"ingredient": "2 teaspoons Sichuan peppercorns", "reason": "not in the catalog", "suggestions": []},
        {"ingredient": "1/4 cup cornstarch", "reason": "not in the catalog", "suggestions": []},
    ],
    "out_of_range": [],
}


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
    assert "# One run at a time: ContextForge 1.0.11's stdio bridge" in text
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
    assert sorted(allowed) == sorted(set(GATEWAY_TOOLS) - {"pantry-submit-origin-evidence",
                                                           "pantry-review-origin-submission"})
    assert [t.name for t in catalog.select(scenario.tools.initial)] == ["pantry-plan-from-text", "fetch-fetch"]
    assert scenario.concurrency == 1
    assert scenario.models.planner == "ollama:command-r7b"
    assert scenario.models.agent == mcpsim_scenario.DEFAULT_AGENT_MODEL


def test_a_contract_shaped_answer_passes_every_check():
    assert failing(GOOD) == set()


@pytest.mark.parametrize(("result", "paths"), [
    (changed(total_cost=0), {"total_cost"}),
    (changed(total_cost="17.46"), {"total_cost"}),  # no coercion: a quoted number is not a cost
    (changed(lines=lambda r: r.__setitem__("lines", r["lines"][:2])), {"lines"}),
    (changed(lines=lambda r: r["lines"][1].pop("store")), {"lines[*].store"}),
    (changed(lines=lambda r: r["lines"][2].__setitem__("price", 0)), {"lines[*].price"}),
    (changed(lines=lambda r: r["lines"][1].__setitem__("match", "relaxed")), {"lines[*].match"}),
    (changed(lines=lambda r: r["lines"][0].__setitem__("ingredient", "1 lb tofu")), {"lines[any].ingredient"}),
    (changed(not_stocked=[]), {"not_stocked"}),
    (changed(not_stocked=lambda r: r["not_stocked"][0].pop("reason")), {"not_stocked[*].reason"}),
    (changed(not_stocked=lambda r: r.pop("not_stocked")),
     {"not_stocked", "not_stocked[*].ingredient", "not_stocked[*].reason"}),
    (changed(out_of_range=lambda r: r.pop("out_of_range")), {"out_of_range", "out_of_range[*].ingredient"}),
    (changed(out_of_range=[{"reason": "beyond 5 km"}]), {"out_of_range[*].ingredient"}),
])
def test_each_wrong_answer_fails_exactly_the_check_that_names_it(result, paths):
    assert failing(result) == paths
