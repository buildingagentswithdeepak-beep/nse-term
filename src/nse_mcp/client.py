"""nse_mcp.client: the only module that talks to the network (spec 4.1).

Transport policy (verified live, see Appendix A.2):
  * one short-lived MCP session per logical call
  * transport="raw" (default): level (a) identical-POST re-send on the same session
    (404 "Session not found"), then level (b) fresh session with backoff
  * transport="sdk": official mcp.Client, level (b) only
  * DNS failures raise immediately; read timeouts are retried once, then fail
  * circuit breaker per upstream URL after 3 consecutive exhausted calls
"""
from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx
from mcp import Client
from mcp.shared.exceptions import MCPError  # noqa: F401  (re-exported for callers/tests)

from .cache import DiskCache, ttl_for
from .errors import DNSFailure, RequestRejected, UpstreamExhausted, UsageError
from .metrics import Metrics, global_metrics
from .resilience import (
    MAX_TIMEOUTS_PER_CALL,
    backoff_delay,
    breaker_for,
    exc_retryable,
    is_dns_failure,
    is_timeout,
    msg_retryable,
)
from .validate import validate_args

log = logging.getLogger("nse_mcp")

BHAVCOPY_URL = "https://mcp.nseindia.in/bhavcopy/cm/mcp"   # EOD, 5y history
CMMKT_URL = "https://mcp.nseindia.in/cmmkt/mcp"            # intraday, 1-3 min delay

PROTOCOL_VERSION = "2025-03-26"  # verified: NSE servers negotiate this
_JSONRPC_HEADERS = {"Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json"}
# Custom UA: some edges reject default library user agents (spec 4.1 / jev notes).
USER_AGENT = "nse-term/1.0 (+https://pypi.org/project/nse-term/)"
RESEND_SLEEP_S = 1.5

HttpFactory = Callable[[float], httpx.Client]


@dataclass
class ToolResult:
    tool: str
    text: str
    is_error: bool
    attempts: int          # fresh sessions used (level b)
    resends: int           # same-session re-sends used (level a, raw only)
    elapsed_s: float
    cached: bool = False   # served from the on-disk cache
    stale: bool = False    # served from an expired cache entry after upstream failure
    server: str = ""


class NSEMCPClient:
    """Sync facade with per-call sessions, two-level retry, validation and caching."""

    def __init__(
        self,
        url: str = BHAVCOPY_URL,
        max_attempts: int = 8,
        base_delay: float = 2.0,
        read_timeout: float = 90.0,
        transport: str = "raw",
        resend_limit: int = 3,
        *,
        server: str = "bhavcopy",
        cache: DiskCache | None = None,
        schemas: dict[str, dict[str, Any]] | None = None,
        insecure_dev: bool = False,
        http_factory: HttpFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        if transport not in ("raw", "sdk"):
            raise UsageError("transport must be 'raw' or 'sdk'")
        _enforce_https(url, insecure_dev)
        self.url = url
        self.server = server
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.read_timeout = read_timeout
        self.transport = transport
        self.resend_limit = resend_limit
        self.cache = cache
        self._schemas = schemas
        self._http_factory = http_factory or (
            lambda t: httpx.Client(timeout=t, headers={"User-Agent": USER_AGENT}))
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._metrics = metrics if metrics is not None else global_metrics
        self._breaker = breaker_for(url)

    # ------------------------------------------------------------------ public
    def call(self, tool: str, args: dict[str, Any], *, allow_stale: bool = False) -> ToolResult:
        schema = self._schema_for(tool)
        validate_args(tool, schema, args)
        ttl = ttl_for(self.server, tool, args)
        if self.cache is not None and ttl is not None:
            hit = self.cache.get(self.server, tool, args, ttl)
            if hit is not None:
                self._metrics.record_cache_hit()
                result = ToolResult(tool, hit, False, 0, 0, 0.0, cached=True, server=self.server)
                self._metrics.record(result)
                return result
        try:
            self._breaker.allow()
            result = self._dispatch(tool, args)
        except (UpstreamExhausted, DNSFailure):
            if allow_stale and self.cache is not None:
                stale_text = self.cache.get_stale(self.server, tool, args)
                if stale_text is not None:
                    log.warning("serving STALE cache for %s", tool)
                    result = ToolResult(tool, stale_text, False, 0, 0, 0.0,
                                        cached=True, stale=True, server=self.server)
                    self._metrics.record(result)
                    return result
            raise
        if self.cache is not None and ttl is not None and not result.is_error:
            self.cache.put(self.server, tool, args, result.text)
        self._metrics.record(result)
        return result

    def call_many(self, calls: list[tuple[str, dict[str, Any]]],
                  concurrency: int = 2) -> list[ToolResult]:
        """Fan out calls with bounded concurrency (politeness: 2)."""
        results: list[ToolResult | None] = [None] * len(calls)

        async def runner() -> None:
            sem = anyio.Semaphore(concurrency)

            async def one(i: int, tool: str, args: dict[str, Any]) -> None:
                async with sem:
                    results[i] = await anyio.to_thread.run_sync(self.call, tool, args)

            async with anyio.create_task_group() as tg:
                for i, (tool, args) in enumerate(calls):
                    tg.start_soon(one, i, tool, args)

        anyio.run(runner)
        return results  # type: ignore[return-value]

    def paged_history(self, symbol: str, total_months: int) -> list[dict]:
        from .pager import paged_history
        return paged_history(self, symbol, total_months)

    def list_tools(self) -> list[Any]:
        if self.transport == "raw":
            return self._list_tools_raw()
        return anyio.run(self._list_tools_async)

    def _list_tools_raw(self) -> list[Any]:
        """tools/list over the raw transport, with the same session re-send and
        fresh-session retry policy as tools/call (spec 4.1). Returns simple objects
        exposing .name and .input_schema, matching the SDK Tool shape."""
        from types import SimpleNamespace

        last_detail = ""
        with self._http_factory(self.read_timeout) as c:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    sid = self._raw_handshake(c)
                except httpx.HTTPError as e:
                    if is_dns_failure(e):
                        raise DNSFailure(f"cannot resolve {self.url}: {e!r}") from e
                    last_detail = f"handshake: {e!r}"
                    self._backoff(attempt)
                    continue
                headers = dict(_JSONRPC_HEADERS)
                headers["mcp-session-id"] = sid
                payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
                for resend in range(self.resend_limit + 1):
                    try:
                        r = c.post(self.url, headers=headers, json=payload)
                    except httpx.HTTPError as e:
                        if is_dns_failure(e):
                            raise DNSFailure(f"cannot resolve {self.url}: {e!r}") from e
                        last_detail = f"post: {e!r}"
                        break
                    if r.status_code == 200:
                        body = _parse_sse(r.text)
                        if "result" in body:
                            return [SimpleNamespace(name=t["name"],
                                                    input_schema=t.get("inputSchema", {}))
                                    for t in body["result"].get("tools", [])]
                        msg = str(body.get("error", {}).get("message", body))[:300]
                        if not msg_retryable(msg) and "internal" not in msg.lower():
                            raise RequestRejected(f"tools/list rejected: {msg}")
                        last_detail = msg
                        break
                    if r.status_code == 404 and resend < self.resend_limit:
                        self._sleep(RESEND_SLEEP_S)
                        continue
                    last_detail = f"HTTP {r.status_code}"
                    break
                self._backoff(attempt)
        raise UpstreamExhausted(f"tools/list failed after {self.max_attempts} sessions: {last_detail}")


    def schemas(self) -> dict[str, dict[str, Any]]:
        if self._schemas is None:
            self._schemas = {t.name: t.input_schema for t in self.list_tools()}
        return self._schemas

    # ----------------------------------------------------------------- helpers
    def _schema_for(self, tool: str) -> dict[str, Any] | None:
        return self.schemas().get(tool)

    def _dispatch(self, tool: str, args: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        if self.transport == "raw":
            result = self._call_raw(tool, args)
        else:
            result = anyio.run(self._call_sdk_async, tool, args)
        result.elapsed_s = time.monotonic() - start
        result.server = self.server
        return result

    # ------------------------------------------------------- raw transport
    def _call_raw(self, tool: str, args: dict[str, Any]) -> ToolResult:
        total_resends = 0
        timeouts = 0
        last_detail = ""
        with self._http_factory(self.read_timeout) as c:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    sid = self._raw_handshake(c)
                except httpx.HTTPError as e:
                    if is_dns_failure(e):
                        raise DNSFailure(f"cannot resolve {self.url}: {e!r}") from e
                    timeouts, stop = _count_timeout(e, timeouts)
                    if stop:
                        last_detail = f"read timeout x{timeouts} during handshake"
                        break
                    last_detail = f"handshake: {e!r}"
                    self._backoff(attempt)
                    continue
                headers = dict(_JSONRPC_HEADERS)
                headers["mcp-session-id"] = sid
                payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}}
                stop_all = False
                for resend in range(self.resend_limit + 1):
                    try:
                        r = c.post(self.url, headers=headers, json=payload)
                    except httpx.HTTPError as e:
                        if is_dns_failure(e):
                            raise DNSFailure(f"cannot resolve {self.url}: {e!r}") from e
                        timeouts, stop_all = _count_timeout(e, timeouts)
                        last_detail = f"post: {e!r}"
                        break  # new session
                    if r.status_code == 200:
                        body = _parse_sse(r.text)
                        if "result" in body:
                            res = body["result"]
                            text = "\n".join(blk.get("text", "")
                                             for blk in res.get("content", [])).strip()
                            return ToolResult(tool, text, bool(res.get("isError", False)),
                                              attempt, total_resends, 0.0)
                        err = body.get("error", {})
                        msg = str(err.get("message", body))[:300]
                        if not msg_retryable(msg):
                            raise RequestRejected(f"{tool} rejected: {msg}")
                        last_detail = msg
                        break  # retryable JSON-RPC error -> new session
                    if r.status_code == 404 and resend < self.resend_limit:
                        total_resends += 1
                        self._sleep(RESEND_SLEEP_S)
                        continue  # verified cheap recovery path (level a)
                    last_detail = f"HTTP {r.status_code}"
                    break
                if stop_all:
                    last_detail = f"read timeout x{timeouts}: {last_detail}"
                    break
                self._backoff(attempt)
        self._breaker.record_failure()
        raise UpstreamExhausted(
            f"{tool} failed after {self.max_attempts} sessions "
            f"({total_resends} same-session re-sends): {last_detail}")

    def _raw_handshake(self, c: httpx.Client) -> str:
        r = c.post(self.url, headers=_JSONRPC_HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "nse-term", "version": "1.0"}}})
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id", "")
        h = dict(_JSONRPC_HEADERS)
        h["mcp-session-id"] = sid
        c.post(self.url, headers=h,
               json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return sid

    def _backoff(self, attempt: int) -> None:
        if attempt < self.max_attempts:
            self._sleep(backoff_delay(attempt, self.base_delay, self._rng))

    # -------------------------------------------------- sdk transport (level b)
    async def _call_sdk_async(self, tool: str, args: dict[str, Any]) -> ToolResult:
        timeouts = 0
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with Client(self.url, read_timeout_seconds=self.read_timeout) as c:
                    r = await c.call_tool(tool, args)
                    text = "\n".join(getattr(b, "text", "") for b in r.content).strip()
                    return ToolResult(tool, text, r.is_error, attempt, 0, 0.0)
            except Exception as e:  # never swallow BaseException (cancellation)
                if is_dns_failure(e):
                    raise DNSFailure(f"cannot resolve {self.url}: {e!r}") from e
                if not exc_retryable(e):
                    raise RequestRejected(f"{tool} rejected: {e!r}") from e
                last_exc = e
                if is_timeout(e):
                    timeouts += 1
                    if timeouts >= MAX_TIMEOUTS_PER_CALL:
                        break
                if attempt < self.max_attempts:
                    await anyio.sleep(backoff_delay(attempt, self.base_delay, self._rng))
        self._breaker.record_failure()
        raise UpstreamExhausted(f"{tool} failed after {self.max_attempts} attempts: {last_exc!r}")

    async def _list_tools_async(self) -> list[Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with Client(self.url, read_timeout_seconds=self.read_timeout) as c:
                    return (await c.list_tools()).tools
            except Exception as e:
                if not exc_retryable(e):
                    raise UpstreamExhausted(f"list_tools rejected: {e!r}") from e
                last_exc = e
                if attempt < self.max_attempts:
                    await anyio.sleep(backoff_delay(attempt, self.base_delay, self._rng))
        raise UpstreamExhausted(f"list_tools failed: {last_exc!r}")


# ------------------------------------------------------------------- helpers
def _enforce_https(url: str, insecure_dev: bool) -> None:
    scheme = urlparse(url).scheme
    if scheme == "https" or (scheme == "http" and insecure_dev):
        return
    raise UsageError(f"refusing non-HTTPS upstream {url!r} (use insecure_dev only for dev)")


def _count_timeout(exc: BaseException, timeouts: int) -> tuple[int, bool]:
    """Return (timeouts, stop). A read timeout is retried once; the second ends the call."""
    if is_timeout(exc):
        timeouts += 1
        return timeouts, timeouts >= MAX_TIMEOUTS_PER_CALL
    return timeouts, False


def _parse_sse(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    try:
        return json.loads(text)
    except Exception:
        return {"error": {"message": text[:300]}}
