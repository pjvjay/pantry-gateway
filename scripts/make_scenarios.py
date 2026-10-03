"""Make runnable gateway scenarios for mcp-sim; they embed this installation's server id.

Two sources:

* mcp-sim's pantry scenarios, written for the direct server. ContextForge names a federated tool
  ``<gateway-slug>-<tool-with-dashes>`` (``find_product`` -> ``pantry-find-product``), so they do
  not match through the gateway. This rewrites tool names and globs wherever a scenario names a
  tool (role, goal, instructions, expected_outcome.text, observers, and the scenario v2 fields:
  title, user_instructions, context, expected_behavior, agent.notes), swaps the server block for
  the virtual server's MCP endpoint ('pantry-sim'), and drops model pins that are not Anthropic's:
  every role's model comes from mcp-sim's simulate skill config, and mcp-sim's pantry scenarios
  pinned their planner to a local Ollama model. Everything else is kept as it was.
* ``--recipe``: this repository's own scenarios (``scenarios/*.yaml``), which are gateway-only
  (they need the fetch tool) and already use ContextForge's tool names. Only the placeholder
  server id is filled in, with the 'pantry-recipes' virtual server's id; comments are kept.

agent.skill (scenario v2) names the SKILL.md the agent under test runs on. An env reference
(``env:NAME``) is left for mcp-sim to resolve at run time; a relative path is made absolute
against the source file, because the generated file lives in another directory. ``--skill-dir
DIR`` writes DIR/SKILL.md in place of ``env:RECIPE_SHOPPER_SKILL`` (pantry-api's
skills/recipe-shopper), so the generated scenarios run without that variable.

Usage: make_scenarios.py [--skill-dir DIR] <mcp-sim>/scenarios/pantry <pantry-sim server-id> [out-dir]
       make_scenarios.py [--skill-dir DIR] --recipe <pantry-recipes server-id> [out-dir]
out-dir defaults to scenarios/generated/ in this repository (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

import yaml

TOOLS = ["list_recipes", "get_recipe", "list_products", "find_product", "get_product", "plan_recipe",
         "plan_from_text", "plan_week", "get_product_origins", "rank_products_by_origin", "origin_triage",
         "submit_origin_evidence", "list_origin_submissions", "review_origin_submission", "pipeline_status"]
SLUG = "pantry"
ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERATED = ROOT / "scenarios" / "generated"
PLACEHOLDER = "PANTRY_RECIPES_SERVER_ID"
SERVER_ID = re.compile(r"[A-Za-z0-9_-]+")
# The environment variable the recipe-link scenario's agent.skill names.
SKILL_ENV = "RECIPE_SHOPPER_SKILL"
SKILL_REF = f"env:{SKILL_ENV}"
ENV_REF = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]*")
# mcp-sim's providers besides Anthropic (mcpsim.llm.PROVIDERS): a spec is "provider:model" only
# when the prefix is a known provider; a bare name is an Anthropic model.
NON_ANTHROPIC = ("ollama",)
WIDTH = 100


def gw(name: str) -> str:
    return f"{SLUG}-" + name.replace("_", "-")


def rename_text(s: str) -> str:
    for t in sorted(TOOLS, key=len, reverse=True):
        s = re.sub(rf"\b{t}\b", gw(t), s)
    return s


def rename_glob(g: str) -> str:
    return g if g == "*" else f"{SLUG}-" + g.replace("_", "-")


def walk(o):
    """Rename tool names in every string value (mapping keys are left alone)."""
    if isinstance(o, dict):
        return {k: walk(v) for k, v in o.items()}
    if isinstance(o, list):
        return [walk(v) for v in o]
    return rename_text(o) if isinstance(o, str) else o


def is_anthropic(spec: str) -> bool:
    provider, sep, _ = spec.strip().partition(":")
    return not (sep and provider.strip().lower() in NON_ANTHROPIC)


def is_local(spec) -> bool:
    return isinstance(spec, str) and not is_anthropic(spec)


def local_models(d: dict) -> list[str]:
    """Every model pin in a scenario that is not an Anthropic model, as 'where: spec': any key of
    the models block (not only the roles mcp-sim knows today) and any observer's model."""
    models = d.get("models")
    found = [f"models.{role}: {spec.strip()}" for role, spec in models.items() if is_local(spec)] \
        if isinstance(models, dict) else []
    for i, obs in enumerate(d.get("observers") or []):
        if isinstance(obs, dict) and is_local(obs.get("model")):
            found.append(f"observers[{i}].model: {obs['model'].strip()}")
    return found


def drop_local_models(d: dict) -> list[str]:
    """Remove the pins local_models finds (and an emptied models block); returns what went."""
    dropped = local_models(d)
    models = d.get("models")
    if isinstance(models, dict):
        for role in [r for r, spec in models.items() if is_local(spec)]:
            del models[role]
        if not models:
            del d["models"]
    for obs in d.get("observers") or []:
        if isinstance(obs, dict) and is_local(obs.get("model")):
            del obs["model"]
    return dropped


def skill_file(arg: str) -> pathlib.Path:
    """--skill-dir: a skill directory (or its SKILL.md) with frontmatter that names the skill."""
    path = pathlib.Path(arg).expanduser().resolve()
    md = path / "SKILL.md" if path.is_dir() else path
    if md.name != "SKILL.md" or not md.is_file():
        sys.exit(f"--skill-dir: no SKILL.md at {md}")
    text = md.read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    meta = yaml.safe_load(front.group(1)) if front else None
    if not isinstance(meta, dict) or not meta.get("name"):
        sys.exit(f"--skill-dir: {md} has no frontmatter naming the skill")
    return md


def resolve_skill(value, base: pathlib.Path, skill_md: pathlib.Path | None):
    """agent.skill as the generated file needs it (see the module docstring). Read the way
    mcp-sim's resolve_skill_path reads it: any ``env:`` value is a reference, ``~`` expands, and
    only what is still relative after that is joined to the scenario's directory."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text == SKILL_REF and skill_md is not None:
        return str(skill_md)
    if text.startswith("env:"):
        return value
    path = pathlib.Path(text).expanduser()
    if path.is_absolute():
        return value if path == pathlib.Path(text) else str(path)
    return str((base / path).resolve())


def remind_env(source: str, skill) -> None:
    if isinstance(skill, str) and ENV_REF.fullmatch(skill):
        var = skill[len("env:"):]
        if not os.path.isfile(os.environ.get(var, "")):
            hint = ", or regenerate with --skill-dir <pantry-api>/skills/recipe-shopper" if var == SKILL_ENV else ""
            print(f"note: {source}: agent.skill is {skill}; set {var} to the skill's SKILL.md before "
                  f"mcpsim runs it{hint}", file=sys.stderr)


class _Dumper(yaml.SafeDumper):
    """Long prose (user_instructions, role, expected_behavior items) as block scalars, so the output
    stays readable: literal when every line already fits, folded (wrapped at WIDTH) otherwise."""


def _str(dumper: yaml.SafeDumper, s: str):
    lines = s.splitlines()
    if len(lines) > 1 and all(len(line) <= WIDTH for line in lines):
        style = "|"
    elif "\n" in s or len(s) > WIDTH:
        style = ">"
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=style)


_Dumper.add_representer(str, _str)


def dump(d: dict) -> str:
    """The scenario as YAML that parses back to exactly ``d`` (else the plain dumper's output)."""
    text = yaml.dump(d, Dumper=_Dumper, sort_keys=False, width=WIDTH, allow_unicode=True)
    if yaml.safe_load(text) != d:
        text = yaml.safe_dump(d, sort_keys=False, width=WIDTH, allow_unicode=True)
    if yaml.safe_load(text) != d:
        sys.exit("the generated scenario does not parse back to itself")
    return text


def gateway_variant(d: dict, source: pathlib.Path, server_id: str,
                    skill_md: pathlib.Path | None = None) -> tuple[dict, list[str]]:
    """One direct-server scenario as the gateway's 'pantry-sim' serves it; returns (scenario,
    the non-Anthropic model pins dropped)."""
    d["name"] = d["name"] + "-gateway"
    if d.get("title"):
        d["title"] = rename_text(d["title"]) + " (gateway)"
    d["server"] = {"http": {"url": f"http://127.0.0.1:4444/servers/{server_id}/mcp",
                            "bearer_env": "CONTEXTFORGE_JWT"}}
    tools = d.get("tools") or {}
    for key in ("allow", "deny", "initial"):
        if tools.get(key):
            tools[key] = [rename_glob(g) for g in tools[key]]
    for k in ("goal", "role", "user_instructions"):
        if isinstance(d.get(k), str):
            d[k] = rename_text(d[k])
    d["instructions"] = [rename_text(i) for i in d.get("instructions", [])]
    if d.get("expected_behavior"):
        d["expected_behavior"] = [rename_text(i) for i in d["expected_behavior"]]
    if d.get("context"):
        d["context"] = walk(d["context"])
    agent = d.get("agent")
    if isinstance(agent, dict):
        if isinstance(agent.get("notes"), str):
            agent["notes"] = rename_text(agent["notes"])
        if "skill" in agent:
            agent["skill"] = resolve_skill(agent["skill"], source.parent, skill_md)
    eo = d.get("expected_outcome", {})
    if eo.get("text"):
        eo["text"] = rename_text(eo["text"])
    if d.get("observers"):
        d["observers"] = walk(d["observers"])
    return d, drop_local_models(d)


def main(src: str, server_id: str, out: str, skill_md: pathlib.Path | None = None) -> list[pathlib.Path]:
    if not SERVER_ID.fullmatch(server_id):
        sys.exit(f"not a server id: {server_id!r}")
    src_dir, out_dir = pathlib.Path(src), pathlib.Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for f in sorted(src_dir.glob("*.yaml")):
        d, dropped = gateway_variant(yaml.safe_load(f.read_text()), f.resolve(), server_id, skill_md)
        for pin in dropped:
            print(f"{f.name}: dropped {pin} (the simulate skill's config chooses every role's model)",
                  file=sys.stderr)
        remind_env(f.name, (d.get("agent") or {}).get("skill"))
        header = (f"# Generated by pantry-gateway/scripts/make_scenarios.py from {f.name}: the same scenario\n"
                  f"# reached through ContextForge's virtual server. Tool names/globs carry the gateway prefix;\n"
                  f"# needs CONTEXTFORGE_JWT in the environment. Regenerate after re-registering the server.\n")
        target = out_dir / f.name
        target.write_text(header + dump(d))
        written.append(target)
        print("wrote", target)
    return written


def set_agent_skill(text: str, value: str) -> str:
    """Replace the value of agent.skill in a block-style ``agent:`` mapping, keeping the rest of
    the file (comments included) byte for byte."""
    lines = text.split("\n")
    try:
        start = lines.index("agent:")
    except ValueError:
        sys.exit("agent.skill must sit in a block-style 'agent:' mapping to be rewritten")
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.startswith((" ", "\t", "#")):
            break  # the next top-level key: agent has no skill line
        m = re.match(r"^([ \t]+skill:[ \t]*)\S", line)
        if m:
            lines[i] = m.group(1) + json.dumps(value, ensure_ascii=False)
            return "\n".join(lines)
    sys.exit("no 'skill:' line in the 'agent:' mapping")


def recipe(server_id: str, out: str | pathlib.Path, skill_md: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Fill the server id into this repository's gateway-only scenarios; returns the files written.

    A text substitution rather than a YAML round trip, so the scenario's comments survive; the
    result is parsed back to check that the id landed in ``server.http.url`` and, when agent.skill
    was rewritten, that nothing else changed.
    """
    if not SERVER_ID.fullmatch(server_id):
        sys.exit(f"not a server id: {server_id!r}")
    out_dir = pathlib.Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for f in sorted((ROOT / "scenarios").glob("*.yaml")):
        text = f.read_text()
        if PLACEHOLDER not in text:
            sys.exit(f"{f}: no {PLACEHOLDER} placeholder to fill")
        filled = text.replace(PLACEHOLDER, server_id)
        data = yaml.safe_load(filled)
        url = (data.get("server") or {}).get("http", {}).get("url", "")
        if f"/servers/{server_id}/mcp" not in url:
            sys.exit(f"{f}: the placeholder is not in server.http.url ({url!r})")
        if local_models(data):
            sys.exit(f"{f}: pins a model that is not Anthropic's ({'; '.join(local_models(data))}); "
                     f"the simulate skill's config chooses every role's model")
        agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
        skill = resolve_skill(agent.get("skill"), f.parent, skill_md)
        if skill != agent.get("skill"):
            filled = set_agent_skill(filled, skill)
            expected = {**data, "agent": {**agent, "skill": skill}}
            if yaml.safe_load(filled) != expected:
                sys.exit(f"{f}: rewriting agent.skill changed more than agent.skill")
        remind_env(f"scenarios/{f.name}", skill)
        header = (f"# Generated by pantry-gateway/scripts/make_scenarios.py --recipe from scenarios/{f.name}:\n"
                  f"# the server id of this installation's 'pantry-recipes' virtual server filled in. Needs\n"
                  f"# CONTEXTFORGE_JWT in the environment. Regenerate after re-registering the server.\n")
        target = out_dir / f.name
        target.write_text(header + filled)
        written.append(target)
        print("wrote", target)
    return written


def cli(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        usage="%(prog)s [--skill-dir DIR] <mcp-sim>/scenarios/pantry <pantry-sim server-id> [out-dir]\n"
              "       %(prog)s [--skill-dir DIR] --recipe <pantry-recipes server-id> [out-dir]")
    ap.add_argument("--recipe", metavar="SERVER_ID",
                    help="fill this 'pantry-recipes' server id into scenarios/*.yaml")
    ap.add_argument("--skill-dir", metavar="DIR",
                    help=f"skill directory written in place of {SKILL_REF} (pantry-api's skills/recipe-shopper); "
                         f"without it, set {SKILL_ENV} to its SKILL.md when mcp-sim runs the scenario")
    ap.add_argument("paths", nargs="*", metavar="ARG")
    ns = ap.parse_args(argv)
    skill_md = skill_file(ns.skill_dir) if ns.skill_dir else None
    if ns.recipe is not None:
        if len(ns.paths) > 1:
            ap.error("--recipe takes the server id and at most an out-dir")
        recipe(ns.recipe, ns.paths[0] if ns.paths else GENERATED, skill_md)
    elif len(ns.paths) in (2, 3):
        main(ns.paths[0], ns.paths[1], ns.paths[2] if len(ns.paths) > 2 else str(GENERATED), skill_md)
    else:
        ap.error("give <mcp-sim>/scenarios/pantry <server-id> [out-dir], or --recipe <server-id> [out-dir]")


if __name__ == "__main__":
    cli()
