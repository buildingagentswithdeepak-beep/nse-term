"""Live smoke suite: runs against production NSE. Excluded by default (pytest -m live).

Mirrors spec Appendix A.3 (the 6 video queries + call_many + paged_history + cmmkt
warm-up) and asserts on payload substrings / plausible ranges, not exit codes.
"""
import pytest

from nse_mcp import BHAVCOPY_URL, CMMKT_URL, NSEMCPClient
from nse_mcp.cache import DiskCache
from nse_mcp.resilience import reset_breakers

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _breakers():
    reset_breakers()
    yield


@pytest.fixture(scope="module")
def eod():
    return NSEMCPClient(BHAVCOPY_URL, cache=None)


def test_live_watchlist_quotes(eod):
    r = eod.call("get_bulk_quote", {"symbols": ["RELIANCE", "TCS", "HDFCBANK", "INFY", "TATAMOTORS"]})
    assert not r.is_error and '"symbol"' in r.text


def test_live_history_one_month(eod):
    r = eod.call("get_stock_history", {"symbol": "INFY", "months": 1, "endDate": "today"})
    assert "close" in r.text


def test_live_moving_average_plausible_range(eod):
    r = eod.call("moving_average", {"symbol": "HINDALCO", "days": 50})
    assert 100.0 < float(r.text) < 5000.0


def test_live_52_week(eod):
    r = eod.call("get_52_week_high_low", {"symbol": "RELIANCE"})
    assert "52w_high" in r.text


def test_live_compare_six_months(eod):
    r = eod.call("compare_stocks", {"symbols": ["HDFCBANK", "ICICIBANK"], "months": 6})
    assert "max_drawdown" in r.text


def test_live_volume(eod):
    r = eod.call("get_volume_analysis", {"symbol": "SUZLON", "days": 30})
    assert "avg_volume" in r.text


def test_live_call_many_fanout(eod):
    many = eod.call_many([("get_52_week_high_low", {"symbol": "TCS"}),
                          ("get_52_week_high_low", {"symbol": "INFY"})])
    assert all("52w_high" in m.text for m in many)


def test_live_paged_history_six_months(eod):
    chunks = eod.paged_history("INFY", 6)
    assert len(chunks) >= 2


def test_live_cmmkt_warmup_absorbed_by_retry_layer():
    cm = NSEMCPClient(CMMKT_URL, cache=None)
    r = cm.call("cm_get_data_status", {})
    assert not r.is_error and len(r.text) > 2
