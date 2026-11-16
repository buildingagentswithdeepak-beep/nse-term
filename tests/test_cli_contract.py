"""CLI contract tests: exit codes 0/2/3/4 and offline command wiring (spec 4.4)."""
import json

import pytest
from typer.testing import CliRunner

from nse_term import cli
from nse_term.config import Config
from nse_term.services import NSEService
from tests.conftest import FakeUpstream, make_client

runner = CliRunner()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Route the CLI at an in-process upstream and an isolated cache directory."""
    holder = {}

    def install(up: FakeUpstream):
        eod = make_client(up)
        cli.state.service = NSEService(eod, None)
        cli.state.cfg = Config(cache_dir=tmp_path, cache_enabled=False)
        cli.state.verbose = 0
        cli.state.stats = False
        holder["up"] = up

    monkeypatch.setattr(cli, "_setup", lambda *a, **k: None)
    return install


def _invoke(*args):
    return runner.invoke(cli.app, list(args), catch_exceptions=False)


def test_usage_error_exits_2(wired):
    wired(FakeUpstream())
    res = _invoke("compare", "TCS", "--months", "6")  # only one symbol
    assert res.exit_code == 2


def test_upstream_exhaustion_exits_3(wired):
    up = FakeUpstream(always_404=True, handlers={})
    wired(up)
    cli.state.service.eod.max_attempts = 1  # keep the test fast
    res = _invoke("watch", "TCS")
    assert res.exit_code == 3


def test_symbol_not_found_exits_4(wired):
    up = FakeUpstream(handlers={
        "get_52_week_high_low": lambda a: (json.dumps({"error": "Symbol not found: ZZZ"}), False),
        "search_symbols": lambda a: (json.dumps({"results": [{"symbol": "ZZZ1"}]}), False)})
    wired(up)
    res = _invoke("range52", "ZZZ")
    assert res.exit_code == 4


def test_watch_json_output_is_machine_readable(wired):
    quote = {"count": 1, "quotes": [{"symbol": "RELIANCE", "close": 1320.0, "pct_change": 0.5}]}
    up = FakeUpstream(handlers={"get_bulk_quote": lambda a: (json.dumps(quote), False)})
    wired(up)
    res = _invoke("watch", "RELIANCE", "--json")
    assert res.exit_code == 0
    data = json.loads(res.output[res.output.index("{"):]) if "{" in res.output else res.output
    assert data["quotes"][0]["symbol"] == "RELIANCE"


def test_bad_windows_option_is_usage_error(wired):
    wired(FakeUpstream())
    res = _invoke("sma", "TCS", "--windows", "50,abc")
    assert res.exit_code == 2


def test_tools_command_lists_schema_inventory_offline(wired):
    wired(FakeUpstream())
    res = _invoke("tools", "--json")
    assert res.exit_code == 0
    assert "get_bulk_quote" in res.output
