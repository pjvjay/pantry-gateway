"""The reference MCP fetch tool over streamable HTTP, every request answered on its own.

Replaces ContextForge's stdio bridge (``mcpgateway.translate --expose-streamable-http``) in front of
``mcp-server-fetch``. That bridge wrote every POST to one stdio process and handed back the first
reply carrying the same JSON-RPC id; independent sessions all number their requests from the same
start, so concurrent callers received each other's pages, and after its 10 s timeout a late reply
went to whichever later caller reused the id (README, "Why not mcpgateway.translate").

Here the MCP Python SDK's own streamable-HTTP transport runs in stateless mode: each POST gets a
fresh transport and server session, so a reply can only go back on the request that asked for it.
The tool itself is mcp-server-fetch's: its ``Fetch`` argument model and JSON schema, its robots.txt
check (``check_may_autonomously_fetch_url``) and its ``fetch_url`` (download, then readability +
markdownify for HTML), with the reference's paging (``max_length`` / ``start_index``) and answer
text. Differences, each on purpose:

* one deadline per call (``--timeout``, default 30 s) instead of the bridge's 10 s;
* ``fetch_url`` runs on a worker thread with its own event loop: its HTML conversion calls Node.js
  through a blocking ``subprocess.run`` (~1.5 s for a recipe page), which would otherwise stall
  every other request this server is answering;
* loopback, private, link-local and other non-public addresses are refused unless
  ``--allow-private-addresses``: checked on every TCP connection (robots.txt, the page and each
  redirect hop) against the address actually dialled, so a public name that resolves or redirects
  inward is refused too;
* the reference's ``fetch`` prompt (a manual fetch that skips robots.txt) is not served.

Run it with the venv that has mcp-server-fetch (``$CONTEXTFORGE_HOME/.venv-fetch``); use
``scripts/run_fetch.sh``, which also keeps the caller's environment away from this process.
``GET /healthz`` answers ``{"status": "ok", "settings": {...}}``; ``--print-config`` prints the
same settings for a command line without starting, which is how run_fetch.sh tells whether the
server already running on the port is this one with these settings.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import ipaddress
import json
import logging
import pathlib
import socket
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
import httpcore
import uvicorn
from httpcore._backends.auto import AutoBackend  # the backend every httpx AsyncClient dials with
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData, TextContent, Tool
from mcp_server_fetch.server import (
    DEFAULT_USER_AGENT_AUTONOMOUS,
    Fetch,
    check_may_autonomously_fetch_url,
    fetch_url,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

log = logging.getLogger("fetch_server")

# The reference server's tool, verbatim: same name, description and input schema, so ContextForge
# federates it exactly as before (tests/test_fetch_server.py compares tools/list with the reference).
TOOL = Tool(
    name="fetch",
    description="""Fetches a URL from the internet and optionally extracts its contents as markdown.

Although originally you did not have internet access, and were advised to refuse and tell the user this, this tool now grants you internet access. Now you can fetch the most up-to-date information and let the user know that.""",  # noqa: E501
    inputSchema=Fetch.model_json_schema(),
)
DEFAULT_TIMEOUT_S = 30.0
CONTEXTFORGE_TOOL_TIMEOUT_S = 60  # ContextForge's TOOL_TIMEOUT default: it stops waiting after this


@dataclass(frozen=True)
class Settings:
    user_agent: str = DEFAULT_USER_AGENT_AUTONOMOUS
    ignore_robots_txt: bool = False
    allow_private_addresses: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S

    def describe(self) -> dict[str, Any]:
        """What /healthz and --print-config report: the settings plus the code and versions behind them."""
        return {
            "server": "pantry-gateway scripts/fetch_server.py",
            "code_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()[:16],
            "mcp_server_fetch": importlib.metadata.version("mcp-server-fetch"),
            "mcp": importlib.metadata.version("mcp"),
            **asdict(self),
        }


# --- non-public addresses ------------------------------------------------------------------------


def address_refused(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything but a public unicast address: loopback, RFC 1918 and ULA private ranges,
    link-local (169.254.0.0/16, cloud metadata included), carrier-grade NAT, unspecified, reserved,
    documentation and multicast. An IPv4-mapped IPv6 address is judged by its IPv4 address."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: dict[str, ipaddress.IPv4Address | ipaddress.IPv6Address] = {}
    for *_, sockaddr in infos:
        text = str(sockaddr[0]).split("%", 1)[0]  # drop an IPv6 zone id
        seen.setdefault(text, ipaddress.ip_address(text))
    return list(seen.values())


def _refusal(host: str, refused: Sequence[object]) -> str:
    return (f"refused to connect to {host} ({', '.join(map(str, refused))}): not a public internet address. "
            "This fetch server reads public pages only (start it with FETCH_ALLOW_PRIVATE_ADDRESSES=true "
            "to allow loopback and private networks)")


_dial = AutoBackend.connect_tcp


async def _guarded_connect_tcp(self: AutoBackend, host: str, port: int, timeout: float | None = None,
                               local_address: str | None = None, socket_options: Any = None) -> Any:
    """AutoBackend.connect_tcp that dials only public addresses, by IP, after resolving the name itself.

    Dialling the checked IP (rather than the name again) leaves no window for a second DNS answer;
    TLS still verifies the certificate against the hostname, which httpcore passes to start_tls."""
    addresses = await _resolve(host, port)
    public = [ip for ip in addresses if not address_refused(ip)]
    if not public:
        raise httpcore.ConnectError(_refusal(host, addresses))
    last: Exception | None = None
    for ip in public:
        try:
            return await _dial(self, str(ip), port, timeout=timeout, local_address=local_address,
                               socket_options=socket_options)
        except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
            last = exc
    assert last is not None
    raise last


def install_address_guard() -> None:
    """Make every httpx/httpcore connection in this process go through the public-address check."""
    AutoBackend.connect_tcp = _guarded_connect_tcp  # type: ignore[method-assign]


async def _check_url_is_public(url: str) -> None:
    """A clear refusal up front for a URL that names a non-public host. The connection-level guard is
    what enforces the rule (it also covers redirects); without this check a loopback URL would surface
    as the robots.txt step's generic "connection issue"."""
    parts = urlsplit(url)
    if not parts.hostname:
        return
    try:
        addresses = await _resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        return  # unresolvable: let the fetch report it the way the reference does
    if addresses and all(address_refused(ip) for ip in addresses):
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Refusing to fetch {url}: "
                                 + _refusal(parts.hostname, addresses)))


# --- the tool ------------------------------------------------------------------------------------


def page(content: str, start_index: int, max_length: int) -> str:
    """The reference server's paging of the fetched content, with the same notes."""
    original_length = len(content)
    if start_index >= original_length:
        return "<error>No more content available.</error>"
    window = content[start_index:start_index + max_length]
    if not window:
        return "<error>No more content available.</error>"
    remaining = original_length - (start_index + len(window))
    if len(window) == max_length and remaining > 0:
        next_start = start_index + len(window)
        window += (f"\n\n<error>Content truncated. Call the fetch tool with a start_index of {next_start} "
                   "to get more content.</error>")
    return window


def _fetch_on_this_thread(url: str, user_agent: str, raw: bool) -> tuple[str, str]:
    return asyncio.run(fetch_url(url, user_agent, force_raw=raw))


async def fetch_tool(arguments: dict[str, Any], settings: Settings) -> list[TextContent]:
    """One fetch call, as the reference's call_tool handles it; raises McpError on any failure."""
    try:
        args = Fetch(**arguments)
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e
    url = str(args.url)
    if not url:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="URL is required"))
    try:
        with anyio.fail_after(settings.timeout_s):
            if not settings.allow_private_addresses:
                await _check_url_is_public(url)
            if not settings.ignore_robots_txt:
                await check_may_autonomously_fetch_url(url, settings.user_agent)
            # abandon_on_cancel: on the deadline the caller gets its error now; the thread finishes
            # (fetch_url has its own 30 s HTTP timeout) and its result is dropped.
            content, prefix = await anyio.to_thread.run_sync(
                _fetch_on_this_thread, url, settings.user_agent, args.raw, abandon_on_cancel=True)
    except TimeoutError:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=(
            f"Failed to fetch {url}: no answer within {settings.timeout_s:g} s"))) from None
    text = page(content, args.start_index, args.max_length)
    return [TextContent(type="text", text=f"{prefix}Contents of {url}:\n{text}")]


def build_server(settings: Settings) -> Server:
    server: Server = Server("mcp-fetch", version=importlib.metadata.version("mcp-server-fetch"))

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [TOOL]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name != TOOL.name:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown tool: {name}"))
        return await fetch_tool(arguments, settings)

    return server


def build_app(settings: Settings, host: str) -> Starlette:
    hosts = ["127.0.0.1", "localhost", "[::1]"] + ([] if host in ("127.0.0.1", "localhost", "::1") else [host])
    manager = StreamableHTTPSessionManager(
        app=build_server(settings),
        stateless=True,      # a fresh transport and session per request: nothing shared between callers
        json_response=True,  # one JSON reply per POST, as the bridge answered
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{h}:*" for h in hosts],
            allowed_origins=[f"http://{h}:*" for h in hosts]),
    )
    described = settings.describe()

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "settings": described})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", endpoint=_ASGIEndpoint(manager)), Route("/healthz", endpoint=healthz)],
                     lifespan=lifespan)


class _ASGIEndpoint:
    """The MCP endpoint as a plain ASGI app (a Route given a function would wrap it as a request handler).

    GET is refused with 405: in stateless mode the SDK would hold a server-to-client SSE stream open
    for every GET, and a fetch server never has anything to send on one."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET":
            await PlainTextResponse("Method Not Allowed: POST JSON-RPC requests to this endpoint", 405,
                                    headers={"Allow": "POST, DELETE"})(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


# --- command line --------------------------------------------------------------------------------


def _user_agent(value: str) -> str:
    # httpx sends header values as ASCII; anything else would fail on every fetch, so refuse it here.
    if not value or any(not 32 <= ord(ch) < 127 for ch in value):
        raise argparse.ArgumentTypeError(f"must be printable ASCII (it is sent as an HTTP header), got {value!r}")
    return value


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = float("nan")
    if not 0 < seconds <= 600:
        raise argparse.ArgumentTypeError(f"must be a number of seconds in (0, 600], got {value!r}")
    return seconds


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--user-agent", type=_user_agent, default=DEFAULT_USER_AGENT_AUTONOMOUS,
                   help="User-Agent for robots.txt and pages (default: the reference's autonomous UA)")
    p.add_argument("--ignore-robots-txt", action="store_true", help="do not check robots.txt (off by default)")
    p.add_argument("--allow-private-addresses", action="store_true",
                   help="also fetch loopback, private and link-local addresses (refused by default)")
    p.add_argument("--timeout", type=_timeout, default=DEFAULT_TIMEOUT_S,
                   help=f"seconds per fetch call, robots.txt and conversion included (default {DEFAULT_TIMEOUT_S:g})")
    p.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    p.add_argument("--print-config", action="store_true",
                   help="print the settings /healthz would report for these arguments, as JSON, and exit")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    a = parse_args(argv)
    settings = Settings(user_agent=a.user_agent, ignore_robots_txt=a.ignore_robots_txt,
                        allow_private_addresses=a.allow_private_addresses, timeout_s=a.timeout)
    if a.print_config:
        print(json.dumps(settings.describe(), sort_keys=True))
        return
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if settings.ignore_robots_txt:
        log.warning("--ignore-robots-txt: the fetch tool will not check robots.txt")
    if settings.allow_private_addresses:
        log.warning("--allow-private-addresses: the fetch tool can read loopback and private-network services")
    else:
        install_address_guard()
    if settings.timeout_s >= CONTEXTFORGE_TOOL_TIMEOUT_S:
        log.warning("--timeout %g s: ContextForge stops waiting for a tool after %d s by default (TOOL_TIMEOUT)",
                    settings.timeout_s, CONTEXTFORGE_TOOL_TIMEOUT_S)
    log.info("fetch server %s -> http://%s:%d/mcp", json.dumps(settings.describe(), sort_keys=True), a.host, a.port)
    uvicorn.run(build_app(settings, a.host), host=a.host, port=a.port, log_level=a.log_level)


if __name__ == "__main__":
    sys.exit(main())
