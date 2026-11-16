"""Shared fixtures: an in-process fake upstream that simulates the NSE load-balancer bug.

No network is used by any offline test. The fake speaks the same JSON-RPC/SSE shape
as the live servers (verified in pyapp-verify/probe_raw.py).
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from nse_mcp import NSEMCPClient
from nse_mcp.resilience import reset_breakers

TEST_URL = "https://upstream.test/mcp"

SCHEMAS: dict[str, dict[str, Any]] = {
    "get_bulk_quote": {"type": "object", "properties": {"symbols": {"type": "array", "items": {"type": "string"}}},
                       "required": ["symbols"], "additionalProperties": False},
    "get_stock_history": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "months": {"type": "integer"}, "endDate": {"type": "string"}},
        "required": ["symbol", "months", "endDate"], "additionalProperties": False},
    "moving_average": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "days": {"type": "integer"}},
        "required": ["symbol", "days"], "additionalProperties": False},
    "get_52_week_high_low": {"type": "object", "properties": {"symbol": {"type": "string"}},
                             "required": ["symbol"], "additionalProperties": False},
    "compare_stocks": {"type": "object", "properties": {
        "symbols": {"type": "array", "items": {"type": "string"}}, "months": {"type": "integer"}},
        "required": ["symbols", "months"], "additionalProperties": False},
    "get_volume_analysis": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "days": {"type": "integer"}},
        "required": ["symbol", "days"], "additionalProperties": False},
    "get_corporate_actions": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "fromDate": {"type": "string"}, "toDate": {"type": "string"}},
        "required": ["symbol", "fromDate", "toDate"], "additionalProperties": False},
    "get_market_breadth": {"type": "object", "properties": {"date": {"type": "string"}},
                           "required": ["date"], "additionalProperties": False},
    "search_symbols": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"], "additionalProperties": False},
    "cm_get_data_status": {"type": "object", "properties": {}, "required": [],
                           "additionalProperties": False},
    "cm_get_stock_quote": {"type": "object", "properties": {"symbol": {"type": "string"}},
                           "required": ["symbol"], "additionalProperties": False},
    "nse_get_market_movers": {"type": "object", "properties": {
        "indexName": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["indexName", "limit"], "additionalProperties": False},
}


@dataclass
class FakeUpstream:
    """Scriptable upstream. handlers[tool](args) -> (status, payload_text) or raises."""
    handlers: dict[str, Any] = field(default_factory=dict)
    # Per-session 404 script: the first N tools/call POSTs on each session return 404.
    fail_first_n_posts: int = 0
    # Restrict the scripted 404s to the first session only (simulates one bad backend).
    fail_first_session_only: bool = False
    # If set, every request ignores session IDs and fails 404 for this many attempts.
    always_404: bool = False
    dns_fail: bool = False
    timeout_always: bool = False
    initialize_count: int = 0
    post_count: defaultdict = field(default_factory=lambda: defaultdict(int))
    _sessions: dict[str, int] = field(default_factory=dict)
    _next: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.dns_fail:
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")
        if self.timeout_always:
            raise httpx.ReadTimeout("read timed out")
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        if method == "initialize":
            self.initialize_count += 1
            self._next += 1
            sid = f"sess-{self._next}"
            self._sessions[sid] = 0
            result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake"}}
            return httpx.Response(200, headers={"mcp-session-id": sid},
                                  text=f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})}\n\n")
        if method == "notifications/initialized":
            return httpx.Response(202, text="")
        if method == "tools/call":
            sid = request.headers.get("mcp-session-id", "")
            if sid not in self._sessions or self.always_404:
                return httpx.Response(404, text="Session not found")
            self._sessions[sid] += 1
            is_first_session = sid == "sess-1" or (self._next and sid == f"sess-{self._next}" and self.initialize_count == 1)
            scripted = self.fail_first_n_posts if (not self.fail_first_session_only or sid == "sess-1") else 0
            if self._sessions[sid] <= scripted:
                return httpx.Response(404, text="Session not found")
            name = body["params"]["name"]
            args = body["params"]["arguments"]
            self.post_count[name] += 1
            handler = self.handlers.get(name)
            if handler is None:
                return _rpc_error(body["id"], "unknown tool")
            text, is_error = handler(args)
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            return httpx.Response(200, text=f"data: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})}\n\n")
        return httpx.Response(400, text="bad method")


def _rpc_error(req_id, message: str) -> httpx.Response:
    err = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": message}}
    return httpx.Response(200, text=f"data: {json.dumps(err)}\n\n")


def make_client(upstream: FakeUpstream, *, sleeps: list | None = None, **kwargs) -> NSEMCPClient:
    """Client wired to the fake upstream. Schemas injected so no tools/list round-trip."""
    transport = httpx.MockTransport(upstream.handle)
    sleep_log = sleeps if sleeps is not None else []

    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(transport=transport, timeout=timeout)

    defaults = dict(max_attempts=8, base_delay=2.0, resend_limit=3, server="bhavcopy",
                    schemas=SCHEMAS, http_factory=factory,
                    sleep=lambda s: sleep_log.append(s), rng=random.Random(0))
    defaults.update(kwargs)
    return NSEMCPClient(TEST_URL, **defaults)


@pytest.fixture(autouse=True)
def _fresh_breakers():
    reset_breakers()
    yield
    reset_breakers()


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


def ok_json(payload: Any) -> tuple[str, bool]:
    return json.dumps(payload), False
