"""Checks for scripts/fetch_server.py and scripts/run_fetch.sh, against local HTTP fixtures only.

The server runs as a subprocess in the fetch venv ($CONTEXTFORGE_HOME/.venv-fetch, where
mcp-server-fetch is installed; scripts/run_fetch.sh creates it). The client side is the MCP SDK of the
venv running pytest (mcp-sim's), so this is also a check across SDK versions. The reference server,
mcp-server-fetch over stdio from the same venv, answers the same calls for comparison. No test leaves
this machine: every page is served by a fixture on 127.0.0.1 or ::1.

    ../mcp-sim/.venv/bin/python -m pytest tests/
"""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytest.importorskip("mcp.client.streamable_http")
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "scripts" / "fetch_server.py"
RUN_FETCH = ROOT / "scripts" / "run_fetch.sh"
CF_HOME = pathlib.Path(os.environ.get("CONTEXTFORGE_HOME", ROOT.parent / "contextforge"))
FETCH_VENV = CF_HOME / ".venv-fetch"
FETCH_PY = FETCH_VENV / "bin" / "python"
REFERENCE = FETCH_VENV / "bin" / "mcp-server-fetch"

pytestmark = pytest.mark.skipif(
    not (FETCH_PY.exists() and REFERENCE.exists()),
    reason=f"needs mcp-server-fetch in {FETCH_VENV} (scripts/run_fetch.sh installs it)")

ROBOTS = "User-agent: *\nDisallow: /private/\n"
UA = "PantryBot/1.0 (+https://example.org/bot; it's me)"


def article(name: str, paragraphs: int = 4) -> str:
    body = "".join(f"<p>Paragraph {i} of the {name} page: marker-{name}. " + "Simmer and stir. " * 12 + "</p>"
                   for i in range(paragraphs))
    return (f"<html><head><title>The {name} page</title></head><body><nav>menu</nav>"
            f"<article><h1>Recipe {name}</h1>{body}</article><footer>footer</footer></body></html>")


class Site(http.server.ThreadingHTTPServer):
    """Pages for the tests; every request is recorded as (path, User-Agent)."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.requests: list[tuple[str, str]] = []
        super().__init__(address, Handler)

    @property
    def base(self) -> str:
        host = self.server_address[0]
        return f"http://{'[' + host + ']' if ':' in host else host}:{self.server_address[1]}"


class Site6(Site):
    address_family = socket.AF_INET6


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8", **headers: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        self.server.requests.append((self.path, self.headers.get("User-Agent", "")))  # type: ignore[attr-defined]
        time.sleep(float(query.get("delay", 0)))
        if url.path == "/robots.txt":
            self.send(200, ROBOTS, "text/plain")
        elif url.path.startswith("/page/") or url.path.startswith("/private/"):
            self.send(200, article(url.path.rsplit("/", 1)[1]))
        elif url.path == "/long":
            self.send(200, article("long", paragraphs=40))
        elif url.path == "/data.json":
            self.send(200, json.dumps({"ingredients": ["1 lb chicken thigh", "1 tbsp light soy sauce"]}),
                      "application/json")
        elif url.path == "/redirect":
            self.send(302, "", Location=query["to"])
        else:
            self.send(404, "not here", "text/plain")


@pytest.fixture(scope="module")
def site():
    s = Site(("127.0.0.1", 0))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()


@pytest.fixture(scope="module")
def site6():
    s = Site6(("::1", 0))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def healthz(base: str) -> dict | None:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def wait_healthy(base: str, proc: subprocess.Popen, seconds: float = 90) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited with {proc.returncode}: {proc.stderr.read() if proc.stderr else ''}")
        if (h := healthz(base)) is not None:
            return h
        time.sleep(0.3)
    raise AssertionError(f"no /healthz on {base} after {seconds} s")


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


@contextlib.contextmanager
def fetch_server(*flags: str):
    port = free_port()
    proc = subprocess.Popen([str(FETCH_PY), str(SERVER), "--port", str(port), "--log-level", "warning", *flags],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        wait_healthy(base, proc)
        yield base
    finally:
        stop(proc)


@pytest.fixture(scope="module")
def private_server():
    """The server with loopback allowed, so it can read the fixtures; everything else default."""
    with fetch_server("--allow-private-addresses") as base:
        yield base


@pytest.fixture(scope="module")
def default_server():
    with fetch_server() as base:
        yield base


async def call(base: str, arguments: dict, name: str = "fetch"):
    async with streamable_http_client(base + "/mcp") as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def post(base: str, rid: int, arguments: dict) -> dict:
    """A bare JSON-RPC tools/call, no session: what the old bridge mixed up when ids collided."""
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": "fetch", "arguments": arguments}}).encode()
    req = urllib.request.Request(base + "/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def marker(text: str, names: list[str]) -> str | None:
    found = [n for n in names if f"marker-{n}" in text]
    return found[0] if len(found) == 1 else None


# --- the same tool as the reference server ------------------------------------------------------


CASES = ["markdown", "first window", "second window", "past the end", "raw html", "json", "robots.txt refusal",
         "404", "nothing listening", "max_length 0", "no url", "not a url"]


def comparison_cases(site: Site) -> dict[str, dict]:
    closed = free_port()
    cases = {
        "markdown": {"url": f"{site.base}/page/alpha"},
        "first window": {"url": f"{site.base}/long", "max_length": 1000},
        "second window": {"url": f"{site.base}/long", "max_length": 1000, "start_index": 1000},
        "past the end": {"url": f"{site.base}/long", "start_index": 999_999},
        "raw html": {"url": f"{site.base}/page/alpha", "raw": True},
        "json": {"url": f"{site.base}/data.json"},
        "robots.txt refusal": {"url": f"{site.base}/private/secret"},
        "404": {"url": f"{site.base}/missing"},
        "nothing listening": {"url": f"http://127.0.0.1:{closed}/page/x"},
        "max_length 0": {"url": f"{site.base}/page/alpha", "max_length": 0},
        "no url": {"max_length": 100},
        "not a url": {"url": "not a url"},
    }
    assert list(cases) == CASES
    return cases


@pytest.fixture(scope="module")
def both_answers(site, private_server):
    """Every comparison case answered by this server and by mcp-server-fetch over stdio."""
    cases = comparison_cases(site)

    async def run():
        ours, ref = {}, {}
        params = StdioServerParameters(command=str(REFERENCE), args=[])
        async with stdio_client(params) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                ref_tools = (await session.list_tools()).tools
                for case, args in cases.items():
                    ref[case] = await session.call_tool("fetch", args)
        async with streamable_http_client(private_server + "/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                our_tools = (await session.list_tools()).tools
                for case, args in cases.items():
                    ours[case] = await session.call_tool("fetch", args)
        return our_tools, ref_tools, ours, ref

    return asyncio.run(run())


def test_lists_the_reference_tool_unchanged(both_answers):
    our_tools, ref_tools, _, _ = both_answers
    assert [t.name for t in our_tools] == ["fetch"]
    assert [t.model_dump() for t in our_tools] == [t.model_dump() for t in ref_tools]


@pytest.mark.parametrize("case", CASES)
def test_answers_exactly_as_the_reference_does(both_answers, case):
    _, _, ours, ref = both_answers
    assert (ours[case].is_error, text_of(ours[case])) == (ref[case].is_error, text_of(ref[case]))
    assert ours[case].structured_content is None


def test_the_comparison_covers_success_paging_and_each_error(both_answers):
    """The cases above are only worth something if they really hit those paths."""
    _, _, ours, _ = both_answers
    assert "marker-alpha" in text_of(ours["markdown"]) and not ours["markdown"].is_error
    assert text_of(ours["first window"]).endswith(
        "<error>Content truncated. Call the fetch tool with a start_index of 1000 to get more content.</error>")
    assert "start_index of 2000" in text_of(ours["second window"])
    assert text_of(ours["past the end"]).endswith("<error>No more content available.</error>")
    assert "<article>" in text_of(ours["raw html"]) or "<h1>" in text_of(ours["raw html"])
    assert text_of(ours["json"]).startswith("Content type application/json cannot be simplified to markdown")
    for case in ("robots.txt refusal", "404", "nothing listening", "max_length 0", "no url", "not a url"):
        assert ours[case].is_error, case
    assert "specifies that autonomous fetching of this page is not allowed" in text_of(ours["robots.txt refusal"])
    assert text_of(ours["404"]).endswith("status code 404")


def test_get_on_the_endpoint_is_refused_and_no_session_is_issued(private_server):
    req = urllib.request.Request(private_server + "/mcp", headers={"Accept": "text/event-stream"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 405
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}).encode()
    req = urllib.request.Request(private_server + "/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers.get("Mcp-Session-Id") is None
        assert json.loads(r.read())["result"]["serverInfo"]["name"] == "mcp-fetch"


# --- every caller gets its own answer -----------------------------------------------------------

NAMES = ["alpha", "bravo", "charlie", "delta"]
# The first caller's page is the slowest, so the answers come back in the reverse order of the asks.
DELAYS = {"alpha": 1.5, "bravo": 1.0, "charlie": 0.5, "delta": 0.0}


@pytest.mark.parametrize("rnd", [1, 2, 3])
def test_four_concurrent_sessions_each_get_their_own_page(site, private_server, rnd):
    async def run():
        return await asyncio.gather(*(call(private_server, {"url": f"{site.base}/page/{n}?delay={DELAYS[n]}"})
                                      for n in NAMES))
    results = asyncio.run(run())
    assert [(r.is_error, marker(text_of(r), NAMES)) for r in results] == [(False, n) for n in NAMES]


def test_four_concurrent_requests_with_the_same_jsonrpc_id_each_get_their_own_page(site, private_server):
    """The old bridge's failure mode exactly: independent callers, all numbering their request 7."""
    out: dict[str, dict] = {}

    def ask(n: str) -> None:
        out[n] = post(private_server, 7, {"url": f"{site.base}/page/{n}?delay={DELAYS[n]}"})

    threads = [threading.Thread(target=ask, args=(n,)) for n in NAMES]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = {n: (out[n]["id"], out[n]["result"]["isError"],
               marker(out[n]["result"]["content"][0]["text"], NAMES)) for n in NAMES}
    assert got == {n: (7, False, n) for n in NAMES}


def test_a_page_slower_than_the_old_ten_second_limit_comes_back(site, private_server):
    result = asyncio.run(call(private_server, {"url": f"{site.base}/page/slowpoke?delay=11"}))
    assert (result.is_error, marker(text_of(result), ["slowpoke"])) == (False, "slowpoke")


def test_after_a_timeout_the_next_caller_with_the_same_id_gets_its_own_page(site):
    """The sequential case: a call that times out must not leave its late answer for the next one."""
    with fetch_server("--allow-private-addresses", "--timeout", "2") as base:
        t = time.time()
        late = post(base, 424242, {"url": f"{site.base}/page/late?delay=4"})
        assert time.time() - t < 3.5
        assert late["result"]["isError"] is True
        assert late["result"]["content"][0]["text"].endswith("no answer within 2 s")
        after = post(base, 424242, {"url": f"{site.base}/page/fresh"})
        time.sleep(2.5)  # the abandoned fetch of 'late' finishes in here
        again = post(base, 424242, {"url": f"{site.base}/page/again"})
    for reply, name in ((after, "fresh"), (again, "again")):
        assert (reply["id"], reply["result"]["isError"]) == (424242, False)
        assert marker(reply["result"]["content"][0]["text"], ["late", "fresh", "again"]) == name


# --- robots.txt and the address guard ----------------------------------------------------------


def test_robots_txt_can_be_ignored_only_by_asking(site):
    with fetch_server("--allow-private-addresses", "--ignore-robots-txt") as base:
        assert healthz(base)["settings"]["ignore_robots_txt"] is True
        result = asyncio.run(call(base, {"url": f"{site.base}/private/secret"}))
    assert (result.is_error, marker(text_of(result), ["secret"])) == (False, "secret")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:{port}/page/alpha",
    "http://localhost:{port}/page/alpha",
    "http://[::1]:{port6}/page/alpha",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.1.2.3/",
    "http://[::ffff:127.0.0.1]:{port}/page/alpha",
])
def test_by_default_non_public_addresses_are_refused_before_any_request(site, site6, default_server, url):
    url = url.format(port=site.server_address[1], port6=site6.server_address[1])
    seen = len(site.requests), len(site6.requests)
    result = asyncio.run(call(default_server, {"url": url}))
    assert result.is_error
    # The URL is echoed as pydantic normalised it ([::ffff:127.0.0.1] reads back as [::ffff:7f00:1]).
    assert text_of(result).startswith("Refusing to fetch http://")
    assert ": not a public internet address. This fetch server reads public pages only" in text_of(result)
    assert (len(site.requests), len(site6.requests)) == seen  # not even robots.txt was asked for


def test_the_guard_is_checked_on_every_connection_including_a_redirect_hop(site, site6):
    """A public first hop cannot be served without leaving the machine, so the guard is narrowed in-process
    to refuse only IPv6: 127.0.0.1 plays the public site, which redirects to [::1] (the private service)."""
    target = f"{site6.base}/page/inside"
    script = f"""
import asyncio, json, sys
sys.path.insert(0, {str(SERVER.parent)!r})
import fetch_server as fs
fs.address_refused = lambda ip: ip.version == 6
fs.install_address_guard()
settings = fs.Settings()
async def one(url):
    try:
        return False, (await fs.fetch_tool({{"url": url}}, settings))[0].text
    except Exception as e:
        return True, str(e)
print(json.dumps([asyncio.run(one({f"{site.base}/page/outside"!r})),
                  asyncio.run(one({f"{site.base}/redirect?to={urllib.parse.quote(target)}"!r}))]))
"""
    seen6 = len(site6.requests)
    out = subprocess.run([str(FETCH_PY), "-c", script], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    (direct_err, direct), (redirected_err, redirected) = json.loads(out.stdout.strip().splitlines()[-1])
    assert (direct_err, marker(direct, ["outside"])) == (False, "outside")
    assert redirected_err
    assert "refused to connect to ::1" in redirected and "not a public internet address" in redirected
    assert len(site6.requests) == seen6


# --- run_fetch.sh ------------------------------------------------------------------------------


@pytest.fixture
def cf_home(tmp_path):
    """A runtime directory with a non-ASCII name, the real fetch venv linked in, and a .env that holds a
    secret next to the FETCH_* keys."""
    home = tmp_path / "cöntextforge home"
    home.mkdir()
    (home / ".venv-fetch").symlink_to(FETCH_VENV.resolve())
    (home / ".env").write_text(f'JWT_SECRET_KEY=must-not-reach-the-fetch-server\nFETCH_USER_AGENT="{UA}"\n'
                               "FETCH_LOG_LEVEL=warning\n")
    return home


def fetch_env(cf_home: pathlib.Path, port: int, **env: str) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "CONTEXTFORGE_HOME": str(cf_home),
            "FETCH_PORT": str(port), **env}


def run_fetch(cf_home: pathlib.Path, port: int, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(RUN_FETCH)], env=fetch_env(cf_home, port, **env), capture_output=True,
                          text=True, timeout=120)


def test_run_fetch_starts_once_with_a_clean_environment_and_refuses_other_settings(site, cf_home):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = fetch_env(cf_home, port, FETCH_ALLOW_PRIVATE_ADDRESSES="true", DUMMY_TOKEN="must-not-reach-the-fetch-server")
    proc = subprocess.Popen(["/bin/bash", str(RUN_FETCH)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)
    try:
        settings = wait_healthy(base, proc)["settings"]
        # FETCH_USER_AGENT came from .env, with spaces, a quote and parentheses, and arrived intact.
        assert (settings["user_agent"], settings["allow_private_addresses"], settings["ignore_robots_txt"]) == (
            UA, True, False)
        n = len(site.requests)
        assert not asyncio.run(call(base, {"url": f"{site.base}/page/alpha"})).is_error
        assert {ua for _, ua in site.requests[n:]} == {UA}  # robots.txt and the page

        # exec chain: bash -> env -i -> python keeps the pid, so this is the server's own environment.
        environ = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(proc.pid)], capture_output=True,
                                 text=True).stdout
        assert "fetch_server.py" in environ and " PATH=" in environ
        assert "must-not-reach-the-fetch-server" not in environ
        assert "DUMMY_TOKEN" not in environ and "JWT_SECRET_KEY" not in environ

        same = run_fetch(cf_home, port, FETCH_ALLOW_PRIVATE_ADDRESSES="true")
        assert same.returncode == 0, same.stderr
        assert "already answers" in same.stdout and "with these settings" in same.stdout

        other = run_fetch(cf_home, port, FETCH_ALLOW_PRIVATE_ADDRESSES="true", FETCH_IGNORE_ROBOTS_TXT="true")
        assert other.returncode == 1
        assert "ignore_robots_txt: running False, asked for True" in other.stderr

        default = run_fetch(cf_home, port)
        assert default.returncode == 1
        assert "allow_private_addresses: running True, asked for False" in default.stderr
        assert healthz(base)["settings"] == settings  # still the first one, untouched
    finally:
        stop(proc)


def test_run_fetch_refuses_something_else_on_the_port(cf_home):
    """The old bridge (or anything) answering /healthz with 200 is no longer taken for the fetch server."""
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AlwaysOk)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        out = run_fetch(cf_home, other.server_address[1])
    finally:
        other.shutdown()
    assert out.returncode == 1
    assert "not this fetch server" in out.stderr


class AlwaysOk(Handler):
    def do_GET(self) -> None:  # noqa: N802
        self.send(200, "ok", "text/plain")


@pytest.mark.parametrize(("env", "message"), [
    ({"FETCH_USER_AGENT": "Bot—1.0"}, "must be printable ASCII"),
    ({"FETCH_USER_AGENT": "Bot\t1"}, "must be printable ASCII"),
    ({"FETCH_IGNORE_ROBOTS_TXT": "maybe"}, "FETCH_IGNORE_ROBOTS_TXT must be true or false, got 'maybe'"),
    ({"FETCH_ALLOW_PRIVATE_ADDRESSES": "yes please"}, "FETCH_ALLOW_PRIVATE_ADDRESSES must be true or false"),
    ({"FETCH_TIMEOUT": "0"}, "must be a number of seconds"),
])
def test_run_fetch_rejects_bad_settings_before_starting(cf_home, env, message):
    out = run_fetch(cf_home, free_port(), **env)
    assert out.returncode != 0
    assert message in out.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
