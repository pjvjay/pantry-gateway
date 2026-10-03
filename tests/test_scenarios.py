"""Checks for the scenario generator and the recipe-link scenario.

Run with mcp-sim's venv, so the scenario is validated by mcp-sim's own loader, matcher and tool
filter rather than by a copy of their rules:

    ../mcp-sim/.venv/bin/python -m pytest tests/

Scenario v2 (category, title, user_instructions, context, expected_behavior, agent) is newer than
the scenario model on mcp-sim's feat/execution-planner, which refuses keys it does not know. So
these tests check the v2 fields against the contract themselves (``v2_problems``) and hand mcp-sim
the rest of the file; once mcp-sim's model knows the v2 keys (feat/simulate), ``load`` gives it
the whole file, and ``v2_problems`` still runs. That loader reads agent.skill while it validates, so
RECIPE_SHOPPER_SKILL is pointed at pantry-api's recipe-shopper skill (or a stand-in) when unset.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

mcpsim_scenario = pytest.importorskip("mcpsim.scenario")
from mcpsim.matcher import match  # noqa: E402
from mcpsim.mcpclient import Catalog, ToolInfo  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scenarios" / "recipe-link-mala-chicken.yaml"
SCRIPT = ROOT / "scripts" / "make_scenarios.py"
MCP_SIM_PANTRY = ROOT.parent / "mcp-sim" / "scenarios" / "pantry"
RECIPE_SHOPPER = ROOT.parent / "pantry-platform" / "pantry-api" / "skills" / "recipe-shopper"
SERVER_ID = "2d405c155dea4b26b767fa7473ded966"
URL = "https://omnivorescookbook.com/mala-chicken/"

spec = importlib.util.spec_from_file_location("make_scenarios", SCRIPT)
make_scenarios = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_scenarios)

# --- scenario v2 (the contract the simulate skill reads) ----------------------------------------

V2_KEYS = ("category", "title", "user_instructions", "context", "expected_behavior", "agent")
MCPSIM_KNOWS_V2 = set(V2_KEYS) <= set(mcpsim_scenario.Scenario.model_fields)
CONTEXT_KEYS = {"device", "location", "language", "details", "agent_visible"}
AGENT_KEYS = {"skill", "notes"}
ENV_REF = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]*")


def v2_problems(data: dict) -> list[str]:
    """Contract A, field by field. Every v2 key is optional; a present one has the shape the
    simulate skill reads. Returns what is wrong, empty when nothing is."""
    problems: list[str] = []

    def text(where, value):
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where} must be a non-blank string")

    for key in ("category", "title", "user_instructions"):
        if key in data:
            text(key, data[key])
    if "context" in data:
        ctx = data["context"]
        if not isinstance(ctx, dict):
            problems.append("context must be a mapping")
        else:
            problems += [f"context.{k} is not a context key" for k in sorted(set(ctx) - CONTEXT_KEYS)]
            for k in ("device", "location", "language"):
                if k in ctx:
                    text(f"context.{k}", ctx[k])
            details = ctx.get("details", {})
            if not isinstance(details, dict):
                problems.append("context.details must be a mapping")
            elif not all(isinstance(k, str) and k.strip() for k in details):
                problems.append("context.details has a blank key")
            if not isinstance(ctx.get("agent_visible", False), bool):
                problems.append("context.agent_visible must be true or false")
    if "expected_behavior" in data:
        items = data["expected_behavior"]
        if not isinstance(items, list) or not items:
            problems.append("expected_behavior must be a non-empty list")
        else:
            for i, item in enumerate(items):
                text(f"expected_behavior[{i}]", item)
    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            problems.append("agent must be a mapping")
        else:
            problems += [f"agent.{k} is not an agent key" for k in sorted(set(agent) - AGENT_KEYS)]
            if "skill" in agent:
                text("agent.skill", agent["skill"])
                skill = agent["skill"]
                if isinstance(skill, str) and skill.startswith("env:") and not ENV_REF.fullmatch(skill):
                    problems.append("agent.skill: an env reference is env:NAME")
            if "notes" in agent:
                text("agent.notes", agent["notes"])
    return problems


def raw(path) -> dict:
    return yaml.safe_load(pathlib.Path(path).read_text())


def load(path):
    """The scenario through mcp-sim's loader: the whole file once mcp-sim knows scenario v2, its v1
    projection (the v2 keys checked here, then left out) until then."""
    data = raw(path)
    assert v2_problems(data) == []
    if MCPSIM_KNOWS_V2:
        return mcpsim_scenario.load_scenario(path)
    return mcpsim_scenario.parse_scenario({k: v for k, v in data.items() if k not in V2_KEYS}, source=str(path))


def strings(o):
    """Every string value in a parsed scenario."""
    if isinstance(o, dict):
        for v in o.values():
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)
    elif isinstance(o, str):
        yield o


def fake_skill(directory: pathlib.Path, name: str = "recipe-shopper") -> pathlib.Path:
    directory.mkdir(parents=True)
    md = directory / "SKILL.md"
    md.write_text(f"---\nname: {name}\ndescription: test double\n---\n\n# {name}\n")
    return md


@pytest.fixture(autouse=True)
def recipe_shopper_skill(tmp_path_factory, monkeypatch):
    """The env reference in agent.skill needs a SKILL.md behind it once mcp-sim's loader reads it:
    pantry-api's own when it is checked out next door, else a stand-in."""
    if not os.environ.get(make_scenarios.SKILL_ENV):
        md = RECIPE_SHOPPER / "SKILL.md"
        if not md.is_file():
            md = fake_skill(tmp_path_factory.mktemp("skill") / "recipe-shopper")
        monkeypatch.setenv(make_scenarios.SKILL_ENV, str(md))


# --- the recorded plans ---------------------------------------------------------------------------

# The 'pantry-recipes' virtual server's tools/list as ContextForge returned it on 2026-10-03, after
# register_fetch.sh kept only pantry's read-only tools next to fetch.
GATEWAY_TOOLS = [
    "pantry-list-recipes", "pantry-get-recipe", "pantry-list-products", "pantry-find-product",
    "pantry-get-product", "pantry-plan-recipe", "pantry-plan-from-text", "pantry-plan-week",
    "pantry-get-product-origins", "pantry-rank-products-by-origin", "pantry-origin-triage",
    "pantry-list-origin-submissions", "pantry-pipeline-status", "fetch-fetch",
]
WRITE_TOOLS = ["pantry-submit-origin-evidence", "pantry-review-origin-submission"]

# Real plan_from_text results for the page's 17 lines with allow_partial and max_km 5, recorded
# against pantry-api#24 (see each file's "recorded" block): from the server's default point, where
# the two Sichuan peppercorn lines came back as one purchase, and from the context's coordinates,
# where they did not. The final_result the scenario asks for copies the summary as returned.
FIXTURES = ROOT / "tests" / "fixtures"
RECORDED = {p.stem: json.loads(p.read_text()) for p in sorted(FIXTURES.glob("mala-chicken-plan-5km*.json"))}
DEFAULT_POINT, CONTEXT_POINT = "mala-chicken-plan-5km", "mala-chicken-plan-5km-latlon"
LINE_KEYS = ("ingredient", "product", "store", "price", "match", "also_lines")


def as_instructed(summary: dict) -> dict:
    return {"total_cost": summary["total_cost"],
            "lines": [{k: line[k] for k in LINE_KEYS} for line in summary["lines"]],
            **{k: summary[k] for k in ("not_stocked", "out_of_range", "skipped")}}


PLAN = RECORDED[DEFAULT_POINT]["summary"]
GOOD = as_instructed(PLAN)

# pantry-api's seeded stores (pantry_planner/storeseed.py) and default point (DEFAULT_LAT/LON), for
# checking the scenario's store list against the distance the persona gives.
STORES = {"Pantry Mart Downtown": (49.2820, -123.1180), "GreenLeaf Grocers Kitsilano": (49.2680, -123.1550),
          "ValueFoods East Van": (49.2620, -123.0700), "MegaSave Richmond": (49.1550, -123.1350)}
HOME = (49.28, -123.12)


def context_point() -> tuple[float, float]:
    lat, lon = re.search(r"\((-?[\d.]+), (-?[\d.]+)\)", raw(TEMPLATE)["context"]["location"]).groups()
    return float(lat), float(lon)


def km(a, b):
    """pantry-api's own distance (nlsearch/sql_builder.py DIST_EXPR): equirectangular, 111 km a degree."""
    return math.hypot((a[0] - b[0]) * 111.0, (a[1] - b[1]) * 111.0 * math.cos(math.radians(a[0])))


def failing(final_result) -> set[str]:
    scenario = load(TEMPLATE)
    return {m.path for m in match(scenario.expected_outcome.json, final_result) if not m.passed}


def changed(**edits):
    result = copy.deepcopy(GOOD)
    for key, fn in edits.items():
        fn(result) if callable(fn) else result.__setitem__(key, fn)
    return result


# --- the recipe-link template as scenario v2 -----------------------------------------------------


def test_the_template_is_scenario_v2():
    data = raw(TEMPLATE)
    assert v2_problems(data) == []
    assert (data["category"], data["title"]) == ("Recipe links", "Mala chicken from a recipe link, within 5 km")
    assert data["context"] == {
        "device": "desktop web", "location": "Vancouver, BC (49.2827, -123.1207)", "language": "en",
        "details": {"getting around": "on foot or by transit, so nearby means within 5 km of downtown",
                    "recipe": URL}}
    assert "agent_visible" not in data["context"]  # the user says where they are; the agent is not told
    assert data["agent"] == {
        "skill": "env:RECIPE_SHOPPER_SKILL",
        "notes": "This environment cannot run scripts; read the recipe page with the fetch-fetch tool "
                 "(markdown, max_length 20000) and use its ingredient lines verbatim."}


def test_the_user_instructions_brief_the_simulated_user_in_the_second_person():
    briefing = raw(TEMPLATE)["user_instructions"]
    paragraphs = briefing.split("\n")  # folded: one line per paragraph, however the file wraps it
    assert len(paragraphs) == 4 and all(p.startswith(("You ", "Open ", "A missing")) for p in paragraphs)
    assert not re.search(r"\b(I|me|my)\b", briefing)  # the user is told who they are, not voiced
    for must in (URL, 'Say "within 5 km"', "downtown Vancouver", "Richmond", "impatient with hedging",
                 "say yes in a few words", "what you will not find", "end the conversation"):
        assert must in briefing, must


def test_the_simulated_user_never_supplies_an_ingredient_list_it_was_not_given():
    """The user has only the link. Told to paste the list when the page cannot be read, an LLM
    playing them would have to invent one, and the run would go on planning made-up ingredients."""
    briefing = raw(TEMPLATE)["user_instructions"]
    assert "you do not know the ingredient list and never make one up" in briefing
    assert "If the assistant says it cannot read the page, say you cannot copy the list right now and end " \
           "the conversation" in briefing
    assert not re.search(r"\bpaste (it|the (ingredient )?list)\b", briefing)


@pytest.mark.parametrize(("behavior", "words"), [
    ("reads the page with fetch-fetch", ["fetch-fetch", URL, "not raw"]),
    ("plans the page's lines with allow_partial and the distance",
     ["pantry-plan-from-text", "verbatim", "allow_partial true", "max_km 5", "default location",
      "the place the user named"]),
    ("quotes total_cost exactly, any trip total labelled", ["total_cost exactly", "trip's", "labelled"]),
    ("lists every line with product, store and price, a shared purchase once",
     ["every line", "product, store and price", "also_lines", "never lists or prices twice"]),
    ("reports what is not stocked, out of range or skipped", ["not_stocked", "out_of_range", "skipped", "reason"]),
    ("calls generic matches substitutions", ["Never presents", '"generic"', "substitution"]),
    ("never invents a product, price or store", ["Never invents", "product, price, store"]),
])
def test_the_judge_checklist_has_one_item_per_expected_behavior(behavior, words):
    items = raw(TEMPLATE)["expected_behavior"]
    assert len(items) == 7
    assert sum(all(w in item for w in words) for item in items) == 1, behavior


def test_items_about_lines_a_plan_may_not_have_are_prohibitions_the_judge_can_grade():
    """The judge passes an item only on evidence, and fails one with nothing to point at; a
    prohibition passes when the transcript shows the agent did not do it. Real plans for this page
    have no generic line, and one has no shared purchase, so those items must be prohibitions."""
    for recorded in RECORDED.values():
        assert [ln for ln in recorded["summary"]["lines"] if ln["match"] == "generic"] == []
    assert [ln for ln in RECORDED[CONTEXT_POINT]["summary"]["lines"] if ln["also_lines"]] == []
    items = raw(TEMPLATE)["expected_behavior"]
    [generic] = [i for i in items if '"generic"' in i]
    assert generic.startswith("Never presents a line whose match is \"generic\" as the ingredient")
    [listing] = [i for i in items if "also_lines" in i]
    assert "never lists or prices twice a purchase that covers several recipe lines" in listing


def test_the_template_names_tools_only_as_the_gateway_does():
    """Every string, the v2 fields included, already uses ContextForge's names (the generator's
    rename would change nothing); fetch-fetch and pantry-plan-from-text are what the agent sees."""
    data = raw(TEMPLATE)
    assert [s for s in strings(data) if make_scenarios.rename_text(s) != s] == []
    v2_text = " ".join(strings({k: data[k] for k in V2_KEYS}))
    assert "fetch-fetch" in v2_text and "pantry-plan-from-text" in v2_text


def test_no_model_is_pinned_so_the_simulate_skill_chooses_every_one():
    data = raw(TEMPLATE)
    assert "models" not in data and make_scenarios.local_models(data) == []
    assert "ollama" not in TEMPLATE.read_text().lower()
    assert load(TEMPLATE).models == mcpsim_scenario.Models()


@pytest.mark.skipif(not (MCPSIM_KNOWS_V2 and RECIPE_SHOPPER.is_dir()),
                    reason="needs mcp-sim's scenario v2 loader and a pantry-api checkout")
def test_the_agent_runs_on_pantry_apis_recipe_shopper(monkeypatch):
    monkeypatch.setenv(make_scenarios.SKILL_ENV, str(RECIPE_SHOPPER / "SKILL.md"))
    agent = load(TEMPLATE).agent
    assert (agent.skill_name, agent.skill_path) == ("recipe-shopper", str((RECIPE_SHOPPER / "SKILL.md").resolve()))
    assert not agent.skill_text.startswith("---") and "plan_from_text" in agent.skill_text


def test_locating_the_place_the_user_names_is_allowed_as_the_skill_says():
    """The skill passes lat/lon for "a place you can locate confidently"; the user says downtown
    Vancouver, and the second recorded plan is that call. Neither the agent's instructions nor the
    judge's checklist may call those coordinates made up."""
    data = raw(TEMPLATE)
    texts = " ".join(" ".join(data["instructions"] + data["expected_behavior"]).split())
    assert "never invent coordinates" not in texts.lower() and "made up" not in texts
    assert "a place you can locate confidently" in texts
    assert "Never pass coordinates for a place the person did not name" in texts
    assert "say downtown Vancouver" in data["user_instructions"]


@pytest.mark.parametrize(("data", "problem"), [
    ({"category": ""}, "category must be a non-blank string"),
    ({"title": None}, "title must be a non-blank string"),
    ({"user_instructions": 3}, "user_instructions must be a non-blank string"),
    ({"context": "desktop"}, "context must be a mapping"),
    ({"context": {"device": "web", "timezone": "PT"}}, "context.timezone is not a context key"),
    ({"context": {"language": " "}}, "context.language must be a non-blank string"),
    ({"context": {"details": ["walks"]}}, "context.details must be a mapping"),
    ({"context": {"details": {" ": "walks"}}}, "context.details has a blank key"),
    ({"context": {"agent_visible": "yes"}}, "context.agent_visible must be true or false"),
    ({"expected_behavior": []}, "expected_behavior must be a non-empty list"),
    ({"expected_behavior": "reads the page"}, "expected_behavior must be a non-empty list"),
    ({"expected_behavior": ["reads the page", " "]}, "expected_behavior[1] must be a non-blank string"),
    ({"agent": "recipe-shopper"}, "agent must be a mapping"),
    ({"agent": {"skill": "SKILL.md", "model": "claude-opus-5-5"}}, "agent.model is not an agent key"),
    ({"agent": {"skill": "env:recipe shopper"}}, "agent.skill: an env reference is env:NAME"),
    ({"agent": {"skill": ""}}, "agent.skill must be a non-blank string"),
    ({"agent": {"notes": ""}}, "agent.notes must be a non-blank string"),
])
def test_the_v2_check_names_each_broken_field(data, problem):
    assert v2_problems(data) == [problem]


# --- the generator: --recipe --------------------------------------------------------------------


def test_recipe_fills_the_server_id_and_keeps_comments_and_every_v2_field(tmp_path):
    written = make_scenarios.recipe(SERVER_ID, tmp_path)
    assert [p.name for p in written] == ["recipe-link-mala-chicken.yaml"]
    text = written[0].read_text()
    assert make_scenarios.PLACEHOLDER not in text
    assert "# Matched against the agent's final_result, for the 161-product catalog" in text
    assert text.endswith(TEMPLATE.read_text().replace(make_scenarios.PLACEHOLDER, SERVER_ID))
    scenario = load(written[0])
    assert scenario.name == "recipe-link-mala-chicken"
    assert scenario.server.http.url == f"http://127.0.0.1:4444/servers/{SERVER_ID}/mcp"
    assert scenario.server.http.bearer_env == "CONTEXTFORGE_JWT"
    generated, template = raw(written[0]), raw(TEMPLATE)
    assert {k: generated[k] for k in V2_KEYS} == {k: template[k] for k in V2_KEYS}
    assert generated["agent"]["skill"] == "env:RECIPE_SHOPPER_SKILL"  # resolved by mcp-sim at run time


def test_recipe_with_skill_dir_writes_the_skill_path_and_nothing_else(tmp_path):
    md = fake_skill(tmp_path / 'pantry api "copy"' / "skills" / "recipe-shopper")
    written = make_scenarios.recipe(SERVER_ID, tmp_path / "out", make_scenarios.skill_file(str(md.parent)))
    generated, template = raw(written[0]), raw(TEMPLATE)
    assert generated["agent"] == {**template["agent"], "skill": str(md.resolve())}
    assert {k: v for k, v in generated.items() if k not in ("agent", "server")} == \
        {k: v for k, v in template.items() if k not in ("agent", "server")}
    # Only the skill line changed; the comments are all still there.
    before = TEMPLATE.read_text().replace(make_scenarios.PLACEHOLDER, SERVER_ID).splitlines()
    after = written[0].read_text().splitlines()[3:]
    assert [(a, b) for a, b in zip(before, after, strict=True) if a != b] == [
        ('  skill: "env:RECIPE_SHOPPER_SKILL"', "  skill: " + json.dumps(str(md.resolve()), ensure_ascii=False))]


def test_recipe_resolves_a_relative_skill_path_against_the_scenario(tmp_path, monkeypatch):
    """The generated file lives in another directory, so a relative agent.skill becomes absolute."""
    (tmp_path / "scenarios").mkdir()
    md = fake_skill(tmp_path / "skills" / "recipe-shopper")
    (tmp_path / "scenarios" / "r.yaml").write_text(TEMPLATE.read_text().replace(
        'skill: "env:RECIPE_SHOPPER_SKILL"', "skill: ../skills/recipe-shopper/SKILL.md"))
    monkeypatch.setattr(make_scenarios, "ROOT", tmp_path)
    [written] = make_scenarios.recipe(SERVER_ID, tmp_path / "scenarios" / "generated")
    assert raw(written)["agent"]["skill"] == str(md.resolve())


def test_recipe_refuses_a_local_model_pin(tmp_path, monkeypatch):
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "r.yaml").write_text(
        TEMPLATE.read_text() + "models: { planner: ollama:command-r7b }\n")
    monkeypatch.setattr(make_scenarios, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="models.planner: ollama:command-r7b"):
        make_scenarios.recipe(SERVER_ID, tmp_path / "out")
    assert not (tmp_path / "out" / "r.yaml").exists()


def test_recipe_refuses_a_server_id_that_is_not_one(tmp_path):
    with pytest.raises(SystemExit):
        make_scenarios.recipe("abc/../../etc", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_skill_dir_must_hold_a_skill(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="no SKILL.md"):
        make_scenarios.skill_file(str(tmp_path / "empty"))
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "SKILL.md").write_text("# no frontmatter\n")
    with pytest.raises(SystemExit, match="no frontmatter"):
        make_scenarios.skill_file(str(tmp_path / "plain"))
    md = fake_skill(tmp_path / "ok")
    assert make_scenarios.skill_file(str(tmp_path / "ok")) == make_scenarios.skill_file(str(md)) == md.resolve()


@pytest.mark.skipif(not RECIPE_SHOPPER.is_dir(), reason="needs a pantry-api checkout in ../pantry-platform")
def test_the_skill_dir_takes_pantry_apis_recipe_shopper():
    md = make_scenarios.skill_file(str(RECIPE_SHOPPER))
    text = md.read_text()
    assert re.match(r"---\nname: recipe-shopper\n", text)
    # The SOP the scenario is built against: the plan call, its arguments, and the report.
    for must in ("plan_from_text", "allow_partial", "max_km", "not_stocked", "out_of_range", "skipped",
                 "also_lines", "generic", "total_cost"):
        assert must in text, must


def test_the_cli_takes_skill_dir_with_recipe(tmp_path):
    md = fake_skill(tmp_path / "recipe-shopper")
    out = tmp_path / "out"
    done = subprocess.run([sys.executable, str(SCRIPT), "--skill-dir", str(md.parent), "--recipe", SERVER_ID, str(out)],
                          capture_output=True, text=True, check=True)
    assert done.stdout.strip() == f"wrote {out / TEMPLATE.name}"
    assert raw(out / TEMPLATE.name)["agent"]["skill"] == str(md.resolve())
    bad = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
    assert bad.returncode == 2 and "--recipe <server-id>" in bad.stderr


def test_the_cli_reminds_about_the_env_var_only_when_it_is_unset(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    done = subprocess.run([sys.executable, str(SCRIPT), "--recipe", SERVER_ID, str(tmp_path)],
                          capture_output=True, text=True, check=True, env=env)
    assert "set RECIPE_SHOPPER_SKILL to the skill's SKILL.md" in done.stderr
    md = fake_skill(tmp_path / "recipe-shopper")
    done = subprocess.run([sys.executable, str(SCRIPT), "--recipe", SERVER_ID, str(tmp_path)],
                          capture_output=True, text=True, check=True, env={**env, "RECIPE_SHOPPER_SKILL": str(md)})
    assert done.stderr == ""


def test_generated_scenarios_stay_git_ignored():
    target = make_scenarios.GENERATED / TEMPLATE.name
    assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(target)], check=False).returncode == 0


# --- the generator: mcp-sim's direct-server scenarios -------------------------------------------

DIRECT_V2 = {
    "name": "cheap-plan",
    "category": "Planning",
    "title": "Cheapest basket with plan_from_text",
    "user_instructions": "You want the cheapest basket.\n\nIf the assistant uses find_product, ask why it "
                         "did not use plan_from_text.",
    "context": {"device": "mobile app", "location": "Vancouver", "language": "en", "agent_visible": True,
                "details": {"plan_from_text": "the tool the user heard of: plan_from_text", "budget": 40}},
    "role": "A shopper who knows plan_recipe.",
    "goal": "The cheapest basket.",
    "instructions": ["Use plan_from_text once."],
    "expected_behavior": ["Calls plan_from_text with allow_partial true.", "Never calls submit_origin_evidence."],
    "agent": {"skill": "skills/shopper/SKILL.md", "notes": "Prefer plan_from_text over find_product."},
    "expected_outcome": {"text": "A basket from plan_from_text.", "json": {"total_cost": {"$gt": 0}}},
    "server": {"stdio": {"command": "python", "args": ["-m", "pantry_planner.mcp_server"]}},
    "tools": {"allow": ["*"], "deny": ["submit_*"], "disclosure": "progressive", "initial": ["plan_from_text"]},
    "models": {"planner": "ollama:command-r7b", "user": "OLLAMA:llama3.2:3b", "judge": "claude-opus-5-5"},
    "observers": [{"name": "auditor", "identity": "Checks the plan_from_text result.", "model": "ollama:qwen2.5:7b",
                   "conditions": [{"id": "used_plan", "when": "the agent called plan_from_text"}]}],
}


def write_direct(src: pathlib.Path, scenario: dict) -> None:
    src.mkdir(parents=True, exist_ok=True)
    (src / f"{scenario['name']}.yaml").write_text(yaml.safe_dump(scenario, sort_keys=False))


def test_direct_v2_fields_get_gateway_names_and_local_models_go(tmp_path, capsys):
    src, out = tmp_path / "mcp-sim" / "scenarios" / "pantry", tmp_path / "out"
    write_direct(src, copy.deepcopy(DIRECT_V2))
    fake_skill(src / "skills" / "shopper", name="shopper")
    [written] = make_scenarios.main(str(src), "31a69179a9c9489daed673ab5eac40b1", str(out))
    g = raw(written)
    assert v2_problems(g) == []
    assert (g["name"], g["category"]) == ("cheap-plan-gateway", "Planning")
    assert g["title"] == "Cheapest basket with pantry-plan-from-text (gateway)"
    assert g["user_instructions"] == ("You want the cheapest basket.\n\nIf the assistant uses pantry-find-product, "
                                      "ask why it did not use pantry-plan-from-text.")
    assert g["context"] == {"device": "mobile app", "location": "Vancouver", "language": "en", "agent_visible": True,
                            "details": {"plan_from_text": "the tool the user heard of: pantry-plan-from-text",
                                        "budget": 40}}
    assert g["expected_behavior"] == ["Calls pantry-plan-from-text with allow_partial true.",
                                      "Never calls pantry-submit-origin-evidence."]
    assert g["agent"] == {"skill": str((src / "skills" / "shopper" / "SKILL.md").resolve()),
                          "notes": "Prefer pantry-plan-from-text over pantry-find-product."}
    assert g["models"] == {"judge": "claude-opus-5-5"}
    assert "model" not in g["observers"][0]
    assert g["observers"][0]["conditions"][0]["when"] == "the agent called pantry-plan-from-text"
    assert "ollama" not in written.read_text().lower()
    assert sorted(capsys.readouterr().err.splitlines()) == sorted(
        f"cheap-plan.yaml: dropped {pin} (the simulate skill's config chooses every role's model)"
        for pin in ("models.planner: ollama:command-r7b", "models.user: OLLAMA:llama3.2:3b",
                    "observers[0].model: ollama:qwen2.5:7b"))
    scenario = load(written)
    assert scenario.tools.initial == ["pantry-plan-from-text"] and scenario.tools.deny == ["pantry-submit-*"]


def test_direct_local_pins_go_under_any_models_key(tmp_path, capsys):
    """Not only the roles mcp-sim knows today: no Ollama model survives anywhere."""
    src = tmp_path / "src"
    write_direct(src, {**copy.deepcopy(DIRECT_V2), "observers": [],
                       "models": {"planner": "ollama:command-r7b", "scout": " Ollama:qwen2.5:7b",
                                  "judge": "anthropic:claude-opus-5-5", "agent": "claude-sonnet-5-5"}})
    fake_skill(src / "skills" / "shopper", name="shopper")
    [written] = make_scenarios.main(str(src), "abc", str(tmp_path / "out"))
    assert raw(written)["models"] == {"judge": "anthropic:claude-opus-5-5", "agent": "claude-sonnet-5-5"}
    assert "ollama" not in written.read_text().lower()
    assert sorted(capsys.readouterr().err.splitlines()) == sorted(
        f"cheap-plan.yaml: dropped {pin} (the simulate skill's config chooses every role's model)"
        for pin in ("models.planner: ollama:command-r7b", "models.scout: Ollama:qwen2.5:7b"))


def test_generated_prose_is_wrapped_and_parses_back_exactly(tmp_path):
    """A folded user_instructions (one long line per paragraph) comes out wrapped, and the file
    parses back to the same strings; short multi-line text stays literal."""
    paragraph = " ".join(["You compare plan_from_text with find_product prices before you trust either."] * 8)
    briefing = f"{paragraph}\n{paragraph}\n"
    behavior = "Calls plan_from_text once.\nThen stops.\n"
    src = tmp_path / "src"
    write_direct(src, {**copy.deepcopy(DIRECT_V2), "user_instructions": briefing,
                       "expected_behavior": [behavior, "Short item."]})
    fake_skill(src / "skills" / "shopper", name="shopper")
    [written] = make_scenarios.main(str(src), "abc", str(tmp_path / "out"))
    renamed = briefing.replace("plan_from_text", "pantry-plan-from-text").replace("find_product", "pantry-find-product")
    g = raw(written)
    assert g["user_instructions"] == renamed
    assert g["expected_behavior"] == ["Calls pantry-plan-from-text once.\nThen stops.\n", "Short item."]
    text = written.read_text()
    assert "user_instructions: >\n" in text and "- |\n" in text
    # Wrapped at the first space past WIDTH, so a line overshoots by one word at most.
    wrapped = [line for line in text.splitlines() if "You compare" in line or "before you trust" in line]
    assert len(wrapped) > 2 * 6
    assert max(map(len, wrapped)) <= make_scenarios.WIDTH + max(len(w) for w in renamed.split()) + 1


def test_direct_models_block_goes_when_only_local_pins_were_in_it(tmp_path):
    src = tmp_path / "src"
    write_direct(src, {**copy.deepcopy(DIRECT_V2), "models": {"planner": "ollama:command-r7b"}})
    fake_skill(src / "skills" / "shopper", name="shopper")
    [written] = make_scenarios.main(str(src), "abc", str(tmp_path / "out"))
    assert "models" not in raw(written)
    assert load(written).models == mcpsim_scenario.Models()


@pytest.mark.parametrize("with_skill_dir", [False, True])
def test_direct_env_skill_is_kept_or_filled_by_skill_dir(tmp_path, with_skill_dir):
    src = tmp_path / "src"
    write_direct(src, {**copy.deepcopy(DIRECT_V2), "agent": {"skill": "env:RECIPE_SHOPPER_SKILL"}})
    md = fake_skill(tmp_path / "recipe-shopper")
    skill_md = make_scenarios.skill_file(str(md.parent)) if with_skill_dir else None
    [written] = make_scenarios.main(str(src), "abc", str(tmp_path / "out"), skill_md)
    assert raw(written)["agent"] == {"skill": str(md.resolve()) if with_skill_dir else "env:RECIPE_SHOPPER_SKILL"}


@pytest.mark.parametrize(("skill", "expected"), [
    ("~/skills/recipe-shopper", str(pathlib.Path.home() / "skills" / "recipe-shopper")),  # mcp-sim expands ~
    ("/abs/skills/recipe-shopper/SKILL.md", "/abs/skills/recipe-shopper/SKILL.md"),
    ("env:RECIPE_SHOPPER_SKILL", "env:RECIPE_SHOPPER_SKILL"),
    ("env:pantry-skill", "env:pantry-skill"),  # any name after env: is a reference to mcp-sim
    ("skills/shopper", "<src>/skills/shopper"),
])
def test_direct_skill_values_resolve_as_mcp_sim_reads_them(tmp_path, skill, expected):
    src = tmp_path / "src"
    write_direct(src, {**copy.deepcopy(DIRECT_V2), "agent": {"skill": skill}})
    [written] = make_scenarios.main(str(src), "abc", str(tmp_path / "out"))
    assert raw(written)["agent"]["skill"] == expected.replace("<src>", str(src.resolve()))


@pytest.mark.skipif(not MCP_SIM_PANTRY.is_dir(), reason="needs an mcp-sim checkout next to this repo")
def test_direct_scenarios_still_get_gateway_names_and_no_local_model(tmp_path):
    written = make_scenarios.main(str(MCP_SIM_PANTRY), "31a69179a9c9489daed673ab5eac40b1", str(tmp_path))
    assert len(written) == len(list(MCP_SIM_PANTRY.glob("*.yaml"))) > 0
    for path in written:
        assert "ollama" not in path.read_text().lower(), path.name
        assert make_scenarios.local_models(raw(path)) == [], path.name
        load(path)
    penne = load(tmp_path / "cheapest-penne.yaml")
    assert penne.name == "cheapest-penne-gateway"
    assert penne.tools.deny == ["pantry-submit-*", "pantry-review-*"]
    assert penne.instructions[0].startswith("Use pantry-find-product ")
    assert penne.server.http.url.endswith("/servers/31a69179a9c9489daed673ab5eac40b1/mcp")
    assert penne.models.planner == mcpsim_scenario.Models().planner


# --- the recipe-link scenario --------------------------------------------------------------------


def test_tool_policy_offers_fetch_and_every_pantry_read_and_plan_tool_but_no_write_tool():
    scenario = load(TEMPLATE)
    catalog = Catalog(tools=[ToolInfo(name=n) for n in GATEWAY_TOOLS])
    allowed = catalog.filtered(scenario.tools.allow, scenario.tools.deny).tool_names()
    assert sorted(allowed) == sorted(GATEWAY_TOOLS)
    # The deny list still keeps the write tools out on a server that offers them (pantry-recipes did).
    with_writes = Catalog(tools=[ToolInfo(name=n) for n in GATEWAY_TOOLS + WRITE_TOOLS])
    assert sorted(with_writes.filtered(scenario.tools.allow, scenario.tools.deny).tool_names()) == sorted(GATEWAY_TOOLS)
    assert [t.name for t in catalog.select(scenario.tools.initial)] == ["pantry-plan-from-text", "fetch-fetch"]


def test_runs_may_overlap_now_that_fetches_cannot_cross():
    """concurrency: 1 was there only for the old stdio bridge; the scenario now takes mcp-sim's default."""
    assert "concurrency" not in TEMPLATE.read_text()
    assert load(TEMPLATE).concurrency == mcpsim_scenario.Scenario.model_fields["concurrency"].default


def test_the_budget_leaves_room_for_the_skills_two_check_ins():
    budgets = load(TEMPLATE).budgets
    assert (budgets.max_turns, budgets.max_tool_calls, budgets.max_cost_usd) == (12, 12, 1.0)


def test_both_recorded_plans_are_on_the_current_catalog():
    assert sorted(RECORDED) == [DEFAULT_POINT, CONTEXT_POINT]
    for recorded in RECORDED.values():
        assert recorded["recorded"]["catalog"].startswith("161 products at 4 stores, 644 store offers")
        assert recorded["recorded"]["mode"].startswith("live")


@pytest.mark.parametrize("name", [DEFAULT_POINT, CONTEXT_POINT])
def test_the_recorded_plan_is_one_the_scenario_asks_for(name):
    """A recording is only evidence if the call is what the instructions make the agent send."""
    args, summary = RECORDED[name]["arguments"], RECORDED[name]["summary"]
    assert (args["allow_partial"], args["max_km"]) == (True, 5)
    assert ("lat" in args) == (name == CONTEXT_POINT)
    if name == CONTEXT_POINT:
        assert (args["lat"], args["lon"]) == context_point()
    page_lines = [ln for ln in args["recipe_text"].splitlines() if ln.startswith("- ")]
    assert len(page_lines) == 17 and page_lines[0].startswith("- 1 lb boneless skinless chicken thigh")
    assert "stores ≤ 5 km" in summary["notes"]
    # Every recipe line is in exactly one place: a purchase (line_no or also_lines) or a dropped list.
    covered = sorted(n for line in summary["lines"] for n in [line["line_no"], *line["also_lines"]])
    dropped = summary["not_stocked"] + summary["out_of_range"] + summary["skipped"]
    assert (covered, dropped) == (list(range(1, 18)), [])
    assert summary["total_cost"] == round(sum(line["price"] for line in summary["lines"]), 2)


def test_one_recording_has_a_purchase_shared_by_two_recipe_lines():
    """So the copied final_result exercises also_lines: lines 10 and 13 bought once."""
    [shared] = [line for line in PLAN["lines"] if line["also_lines"]]
    assert (shared["line_no"], shared["also_lines"], shared["product"]) == (10, [13], "Sichuan Peppercorns Whole 50g")
    assert len(PLAN["lines"]) == 16 and len(RECORDED[CONTEXT_POINT]["summary"]["lines"]) == 17
    assert any(n.startswith("lines 10, 13 (") and n.endswith("bought once") for n in PLAN["notes"])


@pytest.mark.parametrize("home", [HOME, context_point()], ids=["server-default", "context"])
def test_the_store_list_is_exactly_the_stores_within_5_km(home):
    scenario = load(TEMPLATE)
    listed = scenario.expected_outcome.json["lines[*].store"]["$in"]
    assert sorted(listed) == sorted(name for name, at in STORES.items() if km(home, at) <= 5)
    assert km(home, STORES["MegaSave Richmond"]) > 13.5


def test_the_clerk_fails_a_plan_that_ignored_the_5_km():
    """The code check reads the plan's structured result; max_km shows up in its notes."""
    scenario = load(TEMPLATE)
    clerk = next(o for o in scenario.observers if o.name == "recipe_clerk")
    cond = next(c for c in clerk.conditions if c.id == "planned_within_5_km")
    where = cond.check.tool_result["where"]
    for recorded in RECORDED.values():
        assert all(m.passed for m in match(where, {"summary": recorded["summary"], "full": None}))
    no_km = copy.deepcopy(PLAN)
    no_km["notes"] = [n for n in no_km["notes"] if not n.startswith("stores ≤")]
    assert not all(m.passed for m in match(where, {"summary": no_km, "full": None}))
    ten_km = copy.deepcopy(PLAN)
    ten_km["notes"] = [n.replace("≤ 5 km", "≤ 10 km") for n in ten_km["notes"]]
    assert not all(m.passed for m in match(where, {"summary": ten_km, "full": None}))
    assert (cond.otherwise.flag, cond.otherwise.fail) == ("distance_ignored", True)


def test_the_shopping_buddy_fails_hidden_gaps_misquotes_and_double_counting():
    buddy = load(TEMPLATE).observer("shopping_buddy")
    flags = {c.id: (c.then.flag, c.then.fail) for c in buddy.conditions}
    assert flags == {k: (k, True) for k in ("hidden_gap", "unflagged_substitution", "misquoted_total",
                                             "double_counted_purchase")}
    assert "not_stocked, out_of_range or skipped" in " ".join(buddy.condition("hidden_gap").when.split())


def test_a_trip_total_labelled_as_the_trips_is_not_a_misquote():
    """The skill reports the recommended trip, whose total adds travel and so differs from
    total_cost in every recording; only a basket total that differs, or the trip's passed off as
    it, is a misquote."""
    for recorded in RECORDED.values():
        summary = recorded["summary"]
        assert summary["trip"]["total_cost"] != summary["total_cost"]
    when = " ".join(load(TEMPLATE).observer("shopping_buddy").condition("misquoted_total").when.split())
    assert "total for the planned lines differs from summary.total_cost" in when
    assert "gives the recommended trip's total (summary.trip, travel included) as that total" in when
    assert "a trip total labelled as the trip's is not a misquote" in when
    instructions = " ".join(" ".join(raw(TEMPLATE)["instructions"]).split())
    assert "label them as the trip's, never as total_cost" in instructions


@pytest.mark.parametrize("name", [DEFAULT_POINT, CONTEXT_POINT])
def test_the_recorded_plan_copied_as_instructed_passes_every_check(name):
    assert failing(as_instructed(RECORDED[name]["summary"])) == set()


def test_an_honest_partial_answer_passes_too():
    """If the planner's parse or selector misses an ingredient, an answer that reports it is right."""
    def drop_cilantro(r):
        r["lines"] = [ln for ln in r["lines"] if ln["ingredient"] != "cilantro"]
        r["not_stocked"] = [{"ingredient": "cilantro", "reason": "not in the catalog", "suggestions": []}]
    assert failing(changed(partial=drop_cilantro)) == set()

    def selector_gave_up(r):
        r["lines"] = [ln for ln in r["lines"] if ln["ingredient"] != "garlic"]
        r["skipped"] = [{"ingredient": "garlic", "reason": "no product chosen", "suggestions": []}]
    assert failing(changed(partial=selector_gave_up)) == set()


SESAME = next(i for i, ln in enumerate(GOOD["lines"]) if ln["product"] == "Toasted Sesame Seeds 100g")
SHARED = next(i for i, ln in enumerate(GOOD["lines"]) if ln["also_lines"])


@pytest.mark.parametrize(("result", "paths"), [
    (changed(total_cost=0), {"total_cost"}),
    (changed(total_cost=str(PLAN["total_cost"])), {"total_cost"}),  # no coercion: a quoted number is not a cost
    (changed(lines=lambda r: r.__setitem__("lines", r["lines"][:14])), {"lines"}),  # two more ingredients dropped
    (changed(lines=lambda r: r["lines"][1].pop("store")), {"lines[*].store"}),
    # The sesame seeds from Richmond (13.9 km), where the plan without max_km buys them.
    (changed(lines=lambda r: r["lines"][SESAME].update(store="MegaSave Richmond", price=2.64)), {"lines[*].store"}),
    (changed(lines=lambda r: r["lines"][2].__setitem__("price", 0)), {"lines[*].price"}),
    (changed(lines=lambda r: r["lines"][1].__setitem__("match", "relaxed")), {"lines[*].match"}),
    (changed(lines=lambda r: r["lines"][SHARED].pop("also_lines")), {"lines[*].also_lines"}),
    (changed(lines=lambda r: r["lines"][0].__setitem__("ingredient", "1 lb tofu")), {"lines[any].ingredient"}),
    (changed(not_stocked=[{"ingredient": "Sichuan peppercorns"}]), {"not_stocked[*].reason"}),
    (changed(not_stocked=[{"reason": "not in the catalog"}]), {"not_stocked[*].ingredient"}),
    (changed(not_stocked=lambda r: r.pop("not_stocked")),
     {"not_stocked", "not_stocked[*].ingredient", "not_stocked[*].reason"}),
    (changed(out_of_range=lambda r: r.pop("out_of_range")), {"out_of_range"}),
    # Nothing can be out of range within 5 km on this catalog: an entry there is made up.
    (changed(out_of_range=[{"ingredient": "toasted sesame seeds", "reason": "nearest offer 13.9 km away"}]),
     {"out_of_range"}),
    (changed(skipped=lambda r: r.pop("skipped")), {"skipped", "skipped[*].ingredient", "skipped[*].reason"}),
    (changed(skipped=[{"ingredient": "water"}]), {"skipped[*].reason"}),
    (changed(skipped=[{"reason": "never bought"}]), {"skipped[*].ingredient"}),
])
def test_each_wrong_answer_fails_exactly_the_check_that_names_it(result, paths):
    assert failing(result) == paths
