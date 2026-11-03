"""Shared async HTTP client for rasmio.com (server-rendered pages plus the site's own JSON calls).

Every tool goes through `fetch`, which caps concurrency, caches answers and turns HTTP failures into `ToolError`
messages the model can act on.
"""

from __future__ import annotations

import asyncio
import copy
import json as json_lib
import os
import random
import time
from typing import Any
from urllib.parse import quote

import httpx
from curl_cffi.const import CurlOpt
from httpx_curl_cffi import AsyncCurlTransport
from mcp.server.mcpserver.exceptions import ToolError

SITE = "https://rasmio.com"

HEADERS = {
    # User-Agent, sec-ch-ua and TLS come from the impersonated Chrome profile (see _get_client).
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}
HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)

MAX_CONCURRENCY = 2
JITTER = (0.3, 0.8)  # seconds before each uncached call
MAX_RETRIES = 2
MAX_RETRY_WAIT = 10  # cap for Retry-After and backoff, seconds
CACHE_TTL = 1800  # seconds; long because anonymous company profile views are capped per IP
CACHE_MAX = 32
RETRY_DELAY = 0.5  # seconds, doubled per retry
MAX_BODY = 8_000_000  # bytes; a big company page is about 1.6 MB
HOSTS = {"rasmio.com"}

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_limit: asyncio.Semaphore | None = None
_proxy_index = 0
_retired: set[asyncio.Task] = set()
_cache: dict[tuple, tuple[float, Any]] = {}


class _SiteTransport(AsyncCurlTransport):
    """The Chrome profile sends navigation-only headers; a None value makes curl drop them on XHR calls."""

    def _create_request_params(self, req: httpx.Request) -> dict[str, Any]:
        # The parent drops "accept: */*" as an httpx default; curl would then send Chrome's page Accept instead.
        accept = req.headers.get("accept")
        p = super()._create_request_params(req)
        extra: list[tuple[bytes, bytes | None]] = [(b"cache-control", None)]  # Chrome sends max-age=0 only on reload
        if req.headers.get("sec-fetch-mode") != "navigate":
            extra += [(b"upgrade-insecure-requests", None), (b"sec-fetch-user", None)]
        p["headers"] = [*p["headers"], *extra, *([(b"accept", b"*/*")] if accept == "*/*" else [])]
        return p


class ApiError(ToolError):
    """A failed upstream call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client."""
    global _transport, _client, _limit, _proxy_index
    _transport, _client, _limit, _proxy_index = transport, None, None, 0
    _cache.clear()


def _proxies() -> list[str]:
    """RASMIO_MCP_PROXY: one proxy URL, or a comma-separated list used one at a time."""
    return [p.strip() for p in os.environ.get("RASMIO_MCP_PROXY", "").split(",") if p.strip()]


def _next_proxy(client: httpx.AsyncClient) -> None:
    """Sticky pool: move to the next proxy after a block or connect error; the next call rebuilds the client.

    Only the client that failed may rotate. The old client is closed late because other requests may still use it.
    """
    global _client, _limit, _proxy_index
    if len(_proxies()) < 2 or client is not _client:
        return
    _client, _limit, _proxy_index = None, None, _proxy_index + 1
    t = asyncio.create_task(_close_later(client))
    _retired.add(t)
    t.add_done_callback(_retired.discard)


async def _close_later(client: httpx.AsyncClient) -> None:
    await asyncio.sleep(30)  # longer than the 25 s request timeout
    await client.aclose()


def _get_client() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    global _client, _limit
    if _client is None:
        # System proxy settings are ignored; the proxy only comes from RASMIO_MCP_PROXY. curl_cffi presents
        # Chrome's TLS/HTTP2 fingerprint and default headers. The proxy goes to the transport.
        proxies = _proxies()
        _client = httpx.AsyncClient(
            transport=_transport
            or _SiteTransport(
                impersonate="chrome",
                default_headers=True,
                curl_options={
                    CurlOpt.NOPROXY: "*"
                },  # httpx-curl-cffi drops its own NOPROXY unless this dict is non-empty
                proxy=proxies[_proxy_index % len(proxies)] if proxies else None,
                max_connections=2 * MAX_CONCURRENCY,
            ),
            headers=HEADERS,
            timeout=httpx.Timeout(25, connect=8),
            limits=httpx.Limits(max_connections=2 * MAX_CONCURRENCY),
            follow_redirects=False,
            trust_env=False,
        )
        _limit = asyncio.Semaphore(MAX_CONCURRENCY)
    assert _limit is not None
    return _client, _limit


def url_for(path: str) -> str:
    """Absolute site URL for a path such as /company/123/direct/; non-ASCII characters are percent-encoded."""
    return SITE + quote(path, safe="/:?&=%#-_.~")


async def fetch(path: str, params: dict[str, Any] | None = None, api: bool = False, referer: str | None = None) -> Any:
    """GET a page (returns HTML text) or, with `api=True`, one of the site's JSON endpoints (returns parsed JSON).

    Retried up to MAX_RETRIES times on timeout, network error, 5xx, with jittered exponential backoff or after
    Retry-After when sent. 403 and 429 are never retried (429 is the site's per-IP quota). One same-host redirect
    is followed.
    """
    query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    url = url_for(path)
    host = httpx.URL(url).host
    if host not in HOSTS:
        raise ApiError(f"Refusing to call unexpected host {host}.")
    key = (url, tuple(sorted((k, str(v)) for k, v in query.items())), api)
    if (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_TTL:
        return copy.deepcopy(hit[1])
    if api:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",  # the site's fetch wrapper sends it on GETs too
            "rasmio-locale": "fa-IR",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "priority": "u=1, i",
        }
        referer = referer or f"{SITE}/"
    else:
        headers = {"Accept": HTML_ACCEPT, "sec-fetch-dest": "document", "sec-fetch-mode": "navigate"}
        headers["sec-fetch-site"] = "same-origin" if referer else "none"
    if referer:
        headers["Referer"] = referer
    for _hop in range(2):
        r, content = await _with_retries(url, query, headers, host)
        loc = r.headers.get("location")
        if r.status_code in (301, 302, 307, 308) and loc and httpx.URL(url).join(loc).host in HOSTS:
            url, query = str(httpx.URL(url).join(loc)), {}
            continue
        break
    if r.status_code >= 400:
        raise ApiError(_status_message(r, content, host), r.status_code)
    if api:
        try:
            body: Any = json_lib.loads(content)
        except ValueError as e:
            raise ApiError(f"{host} returned a non-JSON response (HTTP {r.status_code}).", r.status_code) from e
    else:
        body = content.decode("utf-8", "replace")
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), copy.deepcopy(body))
    return body


async def _with_retries(url, query, headers, host) -> tuple[httpx.Response, bytes]:
    for attempt in range(MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        wait = RETRY_DELAY * 2**attempt * random.uniform(1, 1.5)
        try:
            r, content = await _request(url, query, headers, host)
        except ApiError as e:
            if last or e.status is not None:
                raise
            await asyncio.sleep(min(wait, MAX_RETRY_WAIT))
            continue
        if r.status_code >= 500 and not last:
            await asyncio.sleep(min(_retry_after(r) or wait, MAX_RETRY_WAIT))
            continue
        return r, content
    raise AssertionError("unreachable")


async def _send(client, limit, url, query, headers, host) -> tuple[httpx.Response, bytes]:
    try:
        async with limit:
            await asyncio.sleep(random.uniform(*JITTER))
            async with client.stream("GET", url, params=query, headers=headers) as r:
                return r, await _read_capped(r, host)
    except httpx.TimeoutException as e:
        raise ApiError(f"{host} did not answer in time. Try again in a moment.") from e
    except httpx.RequestError as e:
        if isinstance(e, httpx.ConnectError | httpx.ProxyError):
            _next_proxy(client)
        raise ApiError(
            f"Could not reach {host} ({type(e).__name__}). Check the internet connection "
            "(or the RASMIO_MCP_PROXY setting if you use one)."
        ) from e


async def _request(url, query, headers, host) -> tuple[httpx.Response, bytes]:
    client, limit = _get_client()
    r, content = await _send(client, limit, url, query, headers, host)
    if r.status_code == 403:
        _next_proxy(client)  # blocked: try the next proxy on the following call
    return r, content


async def _read_capped(r: httpx.Response, host: str) -> bytes:
    """Read the body, refusing anything over MAX_BODY so one huge answer cannot flood memory or context."""
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise ApiError(f"{host} sent a response over {MAX_BODY // 1_000_000} MB; refused.", r.status_code)
    return bytes(buf)


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After on a 503, else None (HTTP-date form is ignored)."""
    v = r.headers.get("retry-after", "")
    return float(v) if r.status_code == 503 and v.replace(".", "", 1).isdigit() else None


def _site_message(content: bytes) -> str:
    """The Persian `message` the site's JSON errors carry, or ''."""
    try:
        msg = json_lib.loads(content).get("message")
    except (ValueError, AttributeError):
        return ""
    return msg if isinstance(msg, str) else ""


def _status_message(r: httpx.Response, content: bytes, host: str) -> str:
    code = r.status_code
    if code == 403:
        return f"{host} blocked the request (HTTP 403). Try another network or set RASMIO_MCP_PROXY."
    if code == 404:
        return f"{host} found nothing at that address (HTTP 404). Check the id."
    if code == 429:
        site = _site_message(content)
        return (
            f"{host} limits anonymous use per IP address (HTTP 429){': ' + site if site else ''}. "
            "Wait a few days, or use another network."
        )
    if code >= 500:
        return f"{host} had a server error (HTTP {code}). Try again later."
    return f"{host} rejected the request (HTTP {code})."
