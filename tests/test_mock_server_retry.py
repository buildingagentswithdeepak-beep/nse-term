"""Mock-server integration tests: prove the two-level retry, DNS, timeout and circuit behaviour.

These run the real NSEMCPClient (raw transport) against the in-process FakeUpstream
that simulates the NSE load-balancer session bug. No network.
"""
import json

import httpx
import pytest

from nse_mcp import DNSFailure, UpstreamExhausted, UsageError
from nse_mcp.cache import DiskCache
from nse_mcp.metrics import Metrics
from nse_mcp.resilience import breaker_for
from tests.conftest import FakeUpstream, make_client


def _quote_handler(args):
    return json.dumps({"count": 1, "quotes": [{"symbol": args["symbols"][0], "close": 100.0}]}), False


def test_level_a_same_session_resend_recovers_without_new_session():
    up = FakeUpstream(handlers={"get_bulk_quote": _quote_handler}, fail_first_n_posts=2)
    client = make_client(up)
    r = client.call("get_bulk_quote", {"symbols": ["TCS"]})
    assert r.attempts == 1          # one fresh session
    assert r.resends == 2           # two identical re-sends on the SAME session
    assert "TCS" in r.text
    assert up.initialize_count == 1


def test_level_b_fresh_session_retry_after_resend_budget_exhausted():
    # 4 posts 404 on session 1 (1 + resend_limit 3), then session 2 succeeds.
    up = FakeUpstream(handlers={"get_bulk_quote": _quote_handler}, fail_first_n_posts=4,
                      fail_first_session_only=True)
    sleeps: list[float] = []
    client = make_client(up, sleeps=sleeps)
    r = client.call("get_bulk_quote", {"symbols": ["TCS"]})
    assert r.attempts == 2
    assert up.initialize_count == 2
    assert any(s >= 2.0 for s in sleeps)  # backoff between sessions


def test_exhaustion_after_max_attempts_raises_upstream_exhausted():
    up = FakeUpstream(handlers={"get_bulk_quote": _quote_handler}, always_404=True)
    client = make_client(up, max_attempts=3)
    with pytest.raises(UpstreamExhausted, match="failed after 3 sessions"):
        client.call("get_bulk_quote", {"symbols": ["TCS"]})


def test_dns_failure_is_non_retryable_and_raises_immediately():
    up = FakeUpstream(dns_fail=True)
    client = make_client(up, max_attempts=8)
    with pytest.raises(DNSFailure):
        client.call("get_bulk_quote", {"symbols": ["TCS"]})
    assert up.initialize_count == 0  # one handshake attempt, no session retries
    assert len(up.post_count) == 0


def test_read_timeout_is_retried_only_once_then_fails():
    up = FakeUpstream(timeout_always=True)
    client = make_client(up, max_attempts=8)
    with pytest.raises(UpstreamExhausted, match="read timeout x2"):
        client.call("get_bulk_quote", {"symbols": ["TCS"]})


def test_tool_request_rejected_is_not_retried():
    up = FakeUpstream(handlers={})  # unknown tool -> JSON-RPC error "unknown tool"
    client = make_client(up)
    with pytest.raises(Exception) as ei:
        client.call("get_bulk_quote", {"symbols": ["TCS"]})
    assert "rejected" in str(ei.value)
    assert up.initialize_count == 1


def test_local_validation_blocks_bad_args_before_any_network_io():
    up = FakeUpstream(handlers={"get_bulk_quote": _quote_handler})
    client = make_client(up)
    with pytest.raises(UsageError):
        client.call("get_bulk_quote", {"symbols": ["TCS"], "bogus": 1})
    assert up.initialize_count == 0


def test_circuit_opens_after_three_exhausted_calls_and_stops_calling_upstream():
    up = FakeUpstream(handlers={"get_bulk_quote": _quote_handler}, always_404=True)
    client = make_client(up, max_attempts=1)
    for _ in range(3):
        with pytest.raises(UpstreamExhausted):
            client.call("get_bulk_quote", {"symbols": ["TCS"]})
    before = up.initialize_count
    from nse_mcp.errors import CircuitOpenError
    with pytest.raises(CircuitOpenError):
        client.call("get_bulk_quote", {"symbols": ["TCS"]})
    assert up.initialize_count == before  # no new upstream traffic once open


def test_breaker_is_shared_per_upstream_url():
    assert breaker_for("https://a.test/mcp") is breaker_for("https://a.test/mcp")
    assert breaker_for("https://a.test/mcp") is not breaker_for("https://b.test/mcp")


def test_https_is_enforced_and_insecure_dev_opt_in():
    with pytest.raises(UsageError, match="non-HTTPS"):
        make_client(FakeUpstream()).__class__("http://plain.test/mcp")
    from nse_mcp import NSEMCPClient
    NSEMCPClient("http://plain.test/mcp", insecure_dev=True)


def test_metrics_record_attempts_resends_and_cache_hits(tmp_path):
    up = FakeUpstream(handlers={"get_bulk_quote": _quote_handler}, fail_first_n_posts=1)
    m = Metrics()
    cache = DiskCache(tmp_path)
    client = make_client(up, cache=cache, metrics=m)
    client.call("get_bulk_quote", {"symbols": ["TCS"]})
    client.call("get_bulk_quote", {"symbols": ["TCS"]})  # cached: dated? no -> latest TTL, hit
    s = m.summary()
    assert s["calls"] == 2
    assert s["resends"] == 1
    assert s["cache_hits"] == 1


def test_stale_fallback_served_only_when_explicitly_allowed(tmp_path):
    up = FakeUpstream(handlers={"get_bulk_quote": _quote_handler})
    cache = DiskCache(tmp_path, clock=lambda: 0.0)
    client = make_client(up, cache=cache)
    client.call("get_bulk_quote", {"symbols": ["TCS"]})
    # Expire the entry and make upstream dead.
    clock_now = [10**9]
    cache._clock = lambda: clock_now[0]
    up.always_404 = True
    client2 = make_client(up, cache=cache, max_attempts=1)
    with pytest.raises(UpstreamExhausted):
        client2.call("get_bulk_quote", {"symbols": ["TCS"]})
    r = client2.call("get_bulk_quote", {"symbols": ["TCS"]}, allow_stale=True)
    assert r.stale and r.cached


def test_raw_list_tools_returns_names_and_schemas_and_uses_resend_on_404():
    import json as _json
    from tests.conftest import SCHEMAS

    def fake_handle(request):
        body = _json.loads(request.content.decode("utf-8"))
        if body.get("method") == "tools/list":
            if not hasattr(fake_handle, "n"):
                fake_handle.n = 0
            fake_handle.n += 1
            if fake_handle.n == 1:  # first list attempt lands on the wrong backend
                return httpx.Response(404, text="Session not found")
            tools = [{"name": n, "inputSchema": s} for n, s in SCHEMAS.items()]
            return httpx.Response(200, text="data: " + _json.dumps(
                {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}) + "\n\n")
        return up.handle(request)

    up = FakeUpstream()
    from tests.conftest import make_client
    client = make_client(up)
    client._http_factory = lambda t: httpx.Client(transport=httpx.MockTransport(fake_handle), timeout=t)
    tools = client.list_tools()
    assert {t.name for t in tools} == set(SCHEMAS)
    assert tools[0].input_schema == SCHEMAS[tools[0].name]
    assert fake_handle.n == 2
