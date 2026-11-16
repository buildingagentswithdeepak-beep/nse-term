"""Service-layer and pager tests: CA guard, chunk paging, SMA payload quirks, error mapping."""
import json

import pytest

from nse_mcp import PayloadError, ToolError
from nse_mcp.cache import DiskCache, FOREVER, ttl_for
from nse_mcp.errors import SymbolNotFound, UsageError
from nse_mcp.pager import paged_history
from nse_term.services import NSEService, _subtract_months, suggest_symbols
from nse_mcp.validate import normalize_symbol
from datetime import date
from tests.conftest import FakeUpstream, make_client

# ---- cache policy (spec 4.6) ------------------------------------------------
def test_cache_policy_dated_eod_is_immutable_latest_expires_cmmkt_never():
    assert ttl_for("bhavcopy", "get_stock_history", {"symbol": "X", "months": 3, "endDate": "2026-10-01"}) is FOREVER
    assert ttl_for("bhavcopy", "get_stock_history", {"symbol": "X", "months": 3, "endDate": "today"}) == 900.0
    assert ttl_for("bhavcopy", "get_52_week_high_low", {"symbol": "X"}) == 900.0
    assert ttl_for("cmmkt", "cm_get_stock_quote", {"symbol": "X"}) is None


def test_disk_cache_roundtrip_and_key_is_canonical(tmp_path):
    c = DiskCache(tmp_path)
    c.put("bhavcopy", "t", {"b": 1, "a": 2}, '{"ok":1}')
    assert c.get("bhavcopy", "t", {"a": 2, "b": 1}, FOREVER) == '{"ok":1}'
    assert DiskCache.key("bhavcopy", "t", {"a": 2, "b": 1}) == DiskCache.key("bhavcopy", "t", {"b": 1, "a": 2})


def test_disk_cache_respects_ttl(tmp_path):
    now = [1000.0]
    c = DiskCache(tmp_path, clock=lambda: now[0])
    c.put("bhavcopy", "t", {}, "x")
    assert c.get("bhavcopy", "t", {}, 900.0) == "x"
    now[0] += 901
    assert c.get("bhavcopy", "t", {}, 900.0) is None


def test_disabled_cache_never_stores(tmp_path):
    c = DiskCache(tmp_path, enabled=False)
    c.put("bhavcopy", "t", {}, "x")
    assert c.get_stale("bhavcopy", "t", {}) is None


# ---- pager (spec 4.2) -------------------------------------------------------
def _history_handler(pages):
    """pages: list of (data_rows, next_end_date). Serves page i on the i-th call."""
    calls = {"i": 0, "args": []}

    def handler(args):
        calls["args"].append(args)
        i = calls["i"]
        calls["i"] += 1
        rows, nxt = pages[min(i, len(pages) - 1)]
        return json.dumps({"symbol": args["symbol"], "trading_days": len(rows), "data": rows,
                           "next_end_date": nxt}), False
    return handler, calls


SCHEMA_HISTORY = None  # filled from conftest SCHEMAS via make_client


def test_paged_history_chains_next_end_date_across_chunks():
    rows1 = [{"date": "2026-10-05", "close": 1.0}]
    rows2 = [{"date": "2026-07-05", "close": 2.0}]
    handler, calls = _history_handler([(rows1, "2026-10-04"), (rows2, "")])
    up = FakeUpstream(handlers={"get_stock_history": handler})
    client = make_client(up)
    chunks = paged_history(client, "INFY", 6)
    assert len(chunks) == 2
    assert calls["args"][0] == {"symbol": "INFY", "months": 3, "endDate": "today"}
    assert calls["args"][1] == {"symbol": "INFY", "months": 3, "endDate": "2026-10-04"}


def test_paged_history_stops_when_upstream_has_no_older_data():
    handler, calls = _history_handler([([{"date": "x"}], "")])
    up = FakeUpstream(handlers={"get_stock_history": handler})
    chunks = paged_history(make_client(up), "INFY", 9)
    assert len(chunks) == 1
    assert calls["i"] == 1


def test_paged_history_detects_cycle_in_next_end_date():
    handler, _ = _history_handler([([{"date": "x"}], "2026-01-01"), ([{"date": "y"}], "2026-01-01")])
    up = FakeUpstream(handlers={"get_stock_history": handler})
    with pytest.raises(PayloadError, match="cycle"):
        paged_history(make_client(up), "INFY", 9)


def test_paged_history_surfaces_tool_error_payload():
    up = FakeUpstream(handlers={"get_stock_history": lambda a: ("{}", True)})
    with pytest.raises(ToolError):
        paged_history(make_client(up), "INFY", 3)


# ---- services ----------------------------------------------------------------
def _service(up: FakeUpstream) -> NSEService:
    return NSEService(make_client(up))


def test_watchlist_separates_found_and_missing_symbols():
    payload = {"count": 2, "quotes": [
        {"symbol": "RELIANCE", "close": 1320.0, "pct_change": 0.5, "volume": 1, "date": "2026-11-05"},
        {"symbol": "NOTREAL", "error": "not found"}]}
    up = FakeUpstream(handlers={"get_bulk_quote": lambda a: (json.dumps(payload), False)})
    wl = _service(up).watchlist(["reliance", "notreal"])
    assert [q.symbol for q in wl.quotes] == ["RELIANCE"]
    assert wl.missing == ["NOTREAL"]


def test_watchlist_rejects_more_than_fifty_symbols_before_network():
    up = FakeUpstream()
    with pytest.raises(UsageError):
        _service(up).watchlist([f"S{i}" for i in range(51)])
    assert up.initialize_count == 0


def test_history_under_three_months_has_no_corporate_action_fetch():
    rows = [{"date": "2026-10-05", "close": 100.0}, {"date": "2026-10-06", "close": 110.0}]
    handler, _ = _history_handler([(rows, "")])
    up = FakeUpstream(handlers={"get_stock_history": handler})
    result = _service(up).history("INFY", 1)
    assert "get_corporate_actions" not in up.post_count
    assert result.summary["return_pct"] == 10.0
    assert result.corporate_actions == []


def test_history_over_three_months_triggers_ca_guard_and_flags_split():
    rows = [{"date": "2026-01-05", "close": 50.0}]
    handler, _ = _history_handler([(rows, "")])
    ca = {"symbol": "X", "count": 1, "actions": [
        {"exDate": "2026-06-01", "actionType": "BONUS", "purpose": "Bonus 1:1", "adjustmentFactor": 0.5}]}
    up = FakeUpstream(handlers={
        "get_stock_history": handler,
        "get_corporate_actions": lambda a: (json.dumps(ca), False)})
    result = _service(up).history("X", 6)
    assert up.post_count["get_corporate_actions"] == 1
    assert len(result.corporate_actions) == 1
    assert "unadjusted" in result.ca_warning


def test_sma_scalar_payload_is_parsed_and_verdict_computed():
    quote = {"count": 1, "quotes": [{"symbol": "HINDALCO", "close": 1000.0}]}
    up = FakeUpstream(handlers={
        "get_bulk_quote": lambda a: (json.dumps(quote), False),
        "moving_average": lambda a: ("950.428", False)})
    v = _service(up).sma_check("HINDALCO", (50,))
    assert v.windows[50] == pytest.approx(950.428)
    assert v.verdicts[50] == "ABOVE"


def test_sma_null_payload_means_unknown_symbol():
    quote = {"count": 1, "quotes": [{"symbol": "BAD", "close": 1.0}]}
    up = FakeUpstream(handlers={
        "get_bulk_quote": lambda a: (json.dumps(quote), False),
        "moving_average": lambda a: ("null", False)})
    with pytest.raises(SymbolNotFound):
        _service(up).sma_check("BAD", (50,))


def test_sma_implausible_value_is_a_payload_error():
    quote = {"count": 1, "quotes": [{"symbol": "X", "close": 1.0}]}
    up = FakeUpstream(handlers={
        "get_bulk_quote": lambda a: (json.dumps(quote), False),
        "moving_average": lambda a: ("-5", False)})
    with pytest.raises(PayloadError):
        _service(up).sma_check("X", (50,))


def test_unknown_symbol_returned_as_http200_error_payload_maps_to_symbol_not_found():
    up = FakeUpstream(handlers={
        "get_52_week_high_low": lambda a: (json.dumps({"error": "Symbol not found: ZZZ"}), False)})
    with pytest.raises(SymbolNotFound, match="not found"):
        _service(up).range52("ZZZ")


def test_range52_parses_live_shape():
    payload = {"symbol": "RELIANCE", "last_close": 1313.4, "52w_high": 1611.8,
               "52w_high_date": "2026-01-05", "52w_low": 1152.2, "52w_low_date": "2026-10-13",
               "position_in_range_pct": 35.07, "from_52w_high_pct": -18.51,
               "from_52w_low_pct": 13.99, "trading_days": 247}
    up = FakeUpstream(handlers={"get_52_week_high_low": lambda a: (json.dumps(payload), False)})
    r = _service(up).range52("reliance")
    assert r.high_52w == 1611.8
    assert r.position_in_range_pct == 35.07


def test_compare_accepts_live_max_drawdown_field_and_ca_guard_above_three_months():
    payload = {"stocks": [{"symbol": "HDFCBANK", "start_price": 1.0, "end_price": 2.0,
                           "return_pct": 100.0, "max_drawdown": 10.0, "trading_days": 100}],
               "months": 6}
    ca = {"symbol": "HDFCBANK", "count": 0, "actions": []}
    up = FakeUpstream(handlers={
        "compare_stocks": lambda a: (json.dumps(payload), False),
        "get_corporate_actions": lambda a: (json.dumps(ca), False)})
    out = _service(up).compare(["HDFCBANK", "ICICIBANK"], 6)
    assert out.raw.stocks[0].max_drawdown == 10.0
    assert up.post_count["get_corporate_actions"] == 2  # one per symbol (spec 4.3)


def test_compare_under_three_months_has_no_ca_fetch():
    payload = {"stocks": [], "months": 2}
    up = FakeUpstream(handlers={"compare_stocks": lambda a: (json.dumps(payload), False)})
    _service(up).compare(["A", "B"], 2)
    assert "get_corporate_actions" not in up.post_count


def test_compare_rejects_single_symbol_locally():
    up = FakeUpstream()
    with pytest.raises(UsageError):
        _service(up).compare(["TCS"], 6)
    assert up.initialize_count == 0


def test_volume_days_bounds_enforced_locally():
    up = FakeUpstream()
    with pytest.raises(UsageError):
        _service(up).volume("SUZLON", 4)
    with pytest.raises(UsageError):
        _service(up).volume("SUZLON", 253)


def test_breadth_defaults_to_today_and_rejects_bad_date():
    payload = {"date": "2026-11-05", "total_stocks": 10, "advances": 6, "declines": 3,
               "unchanged": 1, "ad_ratio": 2.0, "total_volume": 5}
    up = FakeUpstream(handlers={"get_market_breadth": lambda a: (json.dumps(payload), False)})
    b = _service(up).breadth("2026-11-05")
    assert b.advances == 6
    with pytest.raises(UsageError):
        _service(up).breadth("2026-13-01")


def test_demo_runs_the_six_queries_in_order():
    seen = []
    def record(name, payload_text):
        def h(args):
            seen.append(name)
            return payload_text, False
        return h
    quote = json.dumps({"count": 1, "quotes": [{"symbol": "RELIANCE", "close": 100.0}]})
    hist = json.dumps({"symbol": "INFY", "trading_days": 1, "data": [{"date": "x", "close": 1.0}]})
    w52 = json.dumps({"symbol": "RELIANCE", "52w_high": 2.0, "52w_low": 1.0})
    cmp_ = json.dumps({"stocks": [{"symbol": "HDFCBANK", "return_pct": 1.0, "max_drawdown": 1.0}]})
    vol = json.dumps({"symbol": "SUZLON", "avg_volume": 1.0})
    handlers = {
        "get_bulk_quote": record("quote", quote),
        "get_stock_history": record("history", hist),
        "moving_average": record("sma", "950.0"),
        "get_52_week_high_low": record("w52", w52),
        "compare_stocks": record("compare", cmp_),
        "get_volume_analysis": record("volume", vol),
        "get_corporate_actions": record("ca", json.dumps({"symbol": "x", "actions": []})),
    }
    up = FakeUpstream(handlers=handlers)
    out = _service(up).demo()
    assert set(out) == {"watchlist", "history", "sma", "range52", "compare", "volume"}
    assert seen[:1] == ["quote"]
    assert "history" in seen and "compare" in seen


def test_suggest_symbols_returns_search_results_best_effort():
    payload = {"count": 2, "results": [{"symbol": "HDFCBANK"}, {"symbol": "HDFC"}]}
    up = FakeUpstream(handlers={"search_symbols": lambda a: (json.dumps(payload), False)})
    assert suggest_symbols(make_client(up), "HDFC") == ["HDFCBANK", "HDFC"]


def test_suggest_symbols_never_raises_on_upstream_failure():
    up = FakeUpstream(dns_fail=True)
    assert suggest_symbols(make_client(up), "HDFC") == []


# ---- helpers ------------------------------------------------------------------
def test_subtract_months_clamps_day_and_wraps_year():
    assert _subtract_months(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert _subtract_months(date(2026, 1, 15), 3) == date(2025, 10, 15)


def test_anthropic_endpoint_reads_anthropic_key_and_falls_back_to_generic_key():
    from nse_term.agent import AgentError, resolve_api_key, ANTHROPIC_OPENAI_BASE
    assert resolve_api_key(ANTHROPIC_OPENAI_BASE, {"ANTHROPIC_API_KEY": "a"}) == "a"
    assert resolve_api_key(ANTHROPIC_OPENAI_BASE, {"NSE_TERM_AI_API_KEY": "b"}) == "b"
    with pytest.raises(AgentError, match="ANTHROPIC_API_KEY"):
        resolve_api_key(ANTHROPIC_OPENAI_BASE, {})


def test_local_endpoint_needs_no_key():
    from nse_term.agent import resolve_api_key
    assert resolve_api_key("http://localhost:11434/v1", {}) == "not-needed-for-local"


def test_symbol_normalizer_in_services_path():
    assert normalize_symbol("hdfcbank") == "HDFCBANK"


def test_agent_drops_temperature_once_when_model_deprecates_it():
    from types import SimpleNamespace as NS
    from nse_term.agent import NSEAgent

    calls = []
    final = NS(choices=[NS(message=NS(content="ok", tool_calls=None))])

    class Completions:
        def create(self, **kw):
            calls.append(kw)
            if "temperature" in kw:
                raise RuntimeError("Error code: 400 - `temperature` is deprecated for this model.")
            return final

    model = NS(chat=NS(completions=Completions()))
    agent = NSEAgent(model, "claude-x", {})
    reply = agent.ask("q")
    assert reply.answer.startswith("ok") and "Not investment advice." in reply.answer
    assert "temperature" in calls[0] and "temperature" not in calls[1]
    agent.ask("q2")
    assert "temperature" not in calls[2]  # remembered for the rest of the run
