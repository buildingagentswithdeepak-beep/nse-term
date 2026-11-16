"""nse-term CLI (spec 4.4). Typer commands + Rich rendering; exit codes per spec.

Exit codes: 0 success, 2 usage error, 3 upstream exhausted / DNS / circuit open,
4 upstream tool error (bad symbol etc.).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from nse_mcp import BHAVCOPY_URL, CMMKT_URL, DiskCache, NSEMCPClient
from nse_mcp.errors import (
    CircuitOpenError,
    DNSFailure,
    NSEError,
    PayloadError,
    RequestRejected,
    SymbolNotFound,
    ToolError,
    UpstreamExhausted,
    UsageError,
)
from nse_mcp.metrics import global_metrics
from nse_mcp.models import parse_model  # noqa: F401  (re-export for tests)
from nse_mcp.validate import validate_date

from .config import Config, load_config
from .services import CompareOutcome, NSEService, suggest_symbols

EXIT_OK, EXIT_USAGE, EXIT_UPSTREAM, EXIT_TOOL = 0, 2, 3, 4
DISCLAIMER = ("Data courtesy NSE India official MCP servers. Informational/educational use only; "
              "no commercial exploitation; no AI training on the data; not investment advice.")

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Terminal client for the official NSE India MCP servers.\n\n" + DISCLAIMER)
console = Console()
err_console = Console(stderr=True)


class CLIState:
    cfg: Config
    service: NSEService
    verbose: int = 0
    stats: bool = False


state = CLIState()


# ------------------------------------------------------------------ plumbing
def _setup(verbose: int, stats: bool, insecure_dev: bool, no_cache: bool) -> None:
    cfg = load_config()
    if insecure_dev or cfg.insecure_dev:
        cfg.insecure_dev = True
    if no_cache:
        cfg.cache_enabled = False
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    # Spec 12: the SDK's DELETE 501 "Session termination failed" log is cosmetic.
    logging.getLogger("mcp").setLevel(logging.ERROR)
    cache = DiskCache(cfg.cache_dir, cfg.latest_ttl_s, enabled=cfg.cache_enabled)
    common = dict(max_attempts=cfg.max_attempts, base_delay=cfg.base_delay_s,
                  read_timeout=cfg.read_timeout_s, cache=cache, insecure_dev=cfg.insecure_dev)
    eod = NSEMCPClient(cfg.bhavcopy_url, server="bhavcopy", **common)
    live = NSEMCPClient(cfg.cmmkt_url, server="cmmkt", **common)
    state.cfg = cfg
    state.service = NSEService(eod, live)
    state.verbose = verbose
    state.stats = stats


def _emit_json(obj: Any) -> None:
    print(json.dumps(_plain(obj), indent=2, default=str))


def _plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _plain(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True)
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _run(fn, *, as_json: bool = False) -> None:
    """Execute a service call, map errors to spec exit codes, print stats if asked."""
    try:
        out = fn()
        if as_json:
            _emit_json(out)
        else:
            _render(out)
        code = EXIT_OK
    except UsageError as e:
        err_console.print(f"[red]usage error:[/red] {e}")
        code = EXIT_USAGE
    except (UpstreamExhausted, DNSFailure) as e:
        msg = str(e) if isinstance(e, DNSFailure) else (
            "NSE MCP servers are flaky right now (load-balancer session issue). Re-run in a minute.")
        err_console.print(f"[red]upstream:[/red] {msg}")
        if state.verbose:
            err_console.print(f"[dim]{e}[/dim]")
        code = EXIT_UPSTREAM
    except CircuitOpenError as e:
        err_console.print(f"[red]upstream:[/red] {e}")
        code = EXIT_UPSTREAM
    except SymbolNotFound as e:
        err_console.print(f"[red]tool error:[/red] {e}")
        hints = e.suggestions or suggest_symbols(state.service.eod, _query_hint(e))
        if hints:
            err_console.print(f"Did you mean: {', '.join(hints)}?")
        code = EXIT_TOOL
    except (ToolError, PayloadError, RequestRejected) as e:
        err_console.print(f"[red]tool error:[/red] {e}")
        code = EXIT_TOOL
    except NSEError as e:
        err_console.print(f"[red]error:[/red] {e}")
        code = EXIT_TOOL
    if state.stats:
        err_console.print(json.dumps({"stats": global_metrics.summary()}))
    if code != EXIT_OK:
        raise typer.Exit(code)


def _query_hint(e: SymbolNotFound) -> str:
    """Pull the bad symbol back out of the error message ('SYM: Symbol not found...')."""
    return str(e).split(":", 1)[0].strip()


def _render(out: Any) -> None:
    if isinstance(out, dict) and ("quote" in out or "movers" in out):
        _render_live(out)
        return
    if isinstance(out, dict):  # demo: one section per video query
        for key, value in out.items():
            console.rule(key)
            _render(value)
        return
    if hasattr(out, "quotes"):  # Watchlist
        t = Table("symbol", "close", "pct", "volume", "date")
        for q in out.quotes:
            t.add_row(q.symbol, _n(q.close), _n(q.pct_change, "%"), _n(q.volume), q.date or "-")
        console.print(t)
        if out.missing:
            console.print(f"[yellow]not found: {', '.join(out.missing)}[/yellow]")
    elif hasattr(out, "ca_warning") and hasattr(out, "series"):  # HistoryResult
        s = out.summary
        console.print(f"{out.symbol}  days {s.get('trading_days', 0)}  "
                      f"return {_n(s.get('return_pct'), '%')}  "
                      f"low {_n(s.get('period_low'))}  high {_n(s.get('period_high'))}")
        _render_ca(out.corporate_actions, out.ca_warning)
    elif hasattr(out, "verdicts"):  # SMAVerdict
        for w, v in out.windows.items():
            pct = (out.close / v - 1) * 100
            console.print(f"{out.symbol}  close {out.close:.2f}  SMA({w}) {v:.2f}  "
                          f"→ {out.verdicts[w]} ({pct:+.1f}%)")
    elif hasattr(out, "high_52w") or hasattr(out, "position_in_range_pct"):  # Week52
        console.print(f"{out.symbol}  close {_n(out.last_close)}  52w high {_n(out.high_52w)} "
                      f"({out.high_52w_date})  low {_n(out.low_52w)} ({out.low_52w_date})  "
                      f"position {_n(out.position_in_range_pct, '%')}  "
                      f"from-high {_n(out.from_52w_high_pct, '%')}")
    elif isinstance(out, CompareOutcome):
        _render_compare(out)
    elif False:  # live/movers handled by _render_live above
        q = out["quote"].get("stock", {})
        t = Table("symbol", "ltp", "change", "change %", "volume", "updated", "stale")
        t.add_row(q.get("symbol", "-"), _n(q.get("lastTradedPrice")), _n(q.get("change")),
                  _n(q.get("perChange"), "%"), _n(q.get("volume")),
                  out["quote"].get("updatedAt", "-"), "[yellow]YES[/yellow]" if out["stale"] else "no")
        console.print(t)
        if out["stale"]:
            console.print("[yellow on red] ⚠ cm-market data is stale (>5 min); treat as delayed [/]")
    elif False:  # movers
        m = out["movers"]
        t = Table("direction", "symbol", "ltp", "change %")
        for row in m.get("gainers", []):
            t.add_row("gain", row.get("symbol"), _n(row.get("ltp")), _n(row.get("perChange"), "%"))
        for row in m.get("losers", []):
            t.add_row("loss", row.get("symbol"), _n(row.get("ltp")), _n(row.get("perChange"), "%"))
        console.print(t)
        if out["stale"]:
            console.print("[yellow]⚠ movers data is stale (>5 min)[/yellow]")
    elif hasattr(out, "avg_volume"):  # VolumeAnalysis
        console.print(f"{out.symbol}  avg {_n(out.avg_volume)}  max {_n(out.max_volume)}  "
                      f"last {_n(out.last_volume)}  last vs avg {_n(out.last_vs_avg_pct, '%')}")
    elif hasattr(out, "advances"):  # Breadth
        console.print(f"{out.date}  stocks {out.total_stocks}  adv {out.advances}  "
                      f"dec {out.declines}  unch {out.unchanged}  A/D {out.ad_ratio}")
    else:
        console.print_json(json.dumps(_plain(out), default=str))


def _render_live(out: dict) -> None:
    if "quote" in out:
        q = out["quote"].get("stock", {})
        t = Table("symbol", "ltp", "change", "change %", "volume", "updated", "stale")
        t.add_row(q.get("symbol", "-"), _n(q.get("lastTradedPrice")), _n(q.get("change")),
                  _n(q.get("perChange"), "%"), _n(q.get("volume")),
                  out["quote"].get("updatedAt", "-"), "[yellow]YES[/yellow]" if out["stale"] else "no")
        console.print(t)
    else:
        m = out["movers"]
        t = Table("direction", "symbol", "ltp", "change %")
        for row in m.get("gainers", []):
            t.add_row("gain", row.get("symbol"), _n(row.get("ltp")), _n(row.get("perChange"), "%"))
        for row in m.get("losers", []):
            t.add_row("loss", row.get("symbol"), _n(row.get("ltp")), _n(row.get("perChange"), "%"))
        console.print(t)
    if out["stale"]:
        console.print("[yellow]⚠ cm-market data is stale (>5 min); treat as delayed[/yellow]")


def _render_compare(out) -> None:
    t = Table("symbol", "start", "end", "return %", "max drawdown %", "CA events")
    for st in out.raw.stocks:
        t.add_row(st.symbol, _n(st.start_price), _n(st.end_price), _n(st.return_pct),
                  _n(st.max_drawdown), str(len(out.corporate_actions.get(st.symbol, []))))
    console.print(t)
    _render_ca_all(out.corporate_actions, out.warning)


def _render_ca(events: list, warning: str | None) -> None:
    if warning:
        console.print(f"[yellow]⚠ {warning}[/yellow]")
    for e in events:
        console.print(f"  {e.get('exDate')}  {e.get('purpose')}  factor {e.get('adjustmentFactor')}")


def _render_ca_all(by_symbol: dict, warning: str | None) -> None:
    if warning:
        console.print(f"[yellow]⚠ {warning}[/yellow]")
    for sym, events in by_symbol.items():
        for e in events:
            console.print(f"  {sym} {e.get('exDate')}  {e.get('purpose')}  "
                          f"factor {e.get('adjustmentFactor')}")


def _n(value, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{value:,}{suffix}" if isinstance(value, int) else f"{value}{suffix}"


VERBOSE = typer.Option(0, "-v", count=True, help="-v INFO, -vv DEBUG")
STATS = typer.Option(False, "--stats", help="Print per-run metrics to stderr")
INSECURE = typer.Option(False, "--insecure-dev", hidden=True, help="Allow http:// upstream URLs")
NOCACHE = typer.Option(False, "--no-cache", help="Bypass the on-disk cache")
JSON = typer.Option(False, "--json", help="Emit JSON instead of tables")


@app.callback()
def _root(verbose: int = VERBOSE, stats: bool = STATS, insecure_dev: bool = INSECURE,
          no_cache: bool = NOCACHE) -> None:
    _setup(verbose, stats, insecure_dev, no_cache)


@app.command()
def tools(server: str = typer.Option("bhavcopy", "--server", help="bhavcopy | cmmkt"),
          as_json: bool = JSON) -> None:
    """List live tool inventory (also the connectivity health check)."""
    def go():
        client = state.service.eod if server == "bhavcopy" else state.service.live
        if client is None:
            raise UsageError(f"unknown server {server!r}")
        names = sorted(client.schemas())
        if as_json:
            return {"server": server, "count": len(names), "tools": names}
        return {"server": server, "count": len(names), "tools": names}
    _run(go, as_json=as_json)


@app.command()
def watch(symbols: list[str] = typer.Argument(..., help="1-50 symbols"),
          as_json: bool = JSON) -> None:
    """Daily watchlist quotes."""
    _run(lambda: state.service.watchlist(symbols), as_json=as_json)


@app.command()
def history(symbol: str, months: int = typer.Option(1, "--months", min=1, max=60),
            as_json: bool = JSON) -> None:
    """Price history (3-month chunks, CA guard above 3 months)."""
    _run(lambda: state.service.history(symbol, months), as_json=as_json)


@app.command()
def sma(symbol: str, windows: str = typer.Option("50,200", "--windows"),
        as_json: bool = JSON) -> None:
    """Close vs simple moving averages."""
    try:
        ws = tuple(int(w) for w in windows.split(",") if w.strip())
    except ValueError:
        err_console.print("[red]usage error:[/red] --windows must be comma-separated integers")
        raise typer.Exit(EXIT_USAGE)
    _run(lambda: state.service.sma_check(symbol, ws), as_json=as_json)


@app.command()
def range52(symbol: str, as_json: bool = JSON) -> None:
    """52-week high/low and position in range."""
    _run(lambda: state.service.range52(symbol), as_json=as_json)


@app.command()
def compare(symbols: list[str] = typer.Argument(..., help="2-10 symbols"),
            months: int = typer.Option(6, "--months", min=1, max=24),
            as_json: bool = JSON) -> None:
    """Side-by-side return and drawdown (CA guard above 3 months)."""
    _run(lambda: state.service.compare(symbols, months), as_json=as_json)


@app.command()
def volume(symbol: str, days: int = typer.Option(30, "--days", min=5, max=252),
           as_json: bool = JSON) -> None:
    """Volume analysis over N trading days."""
    _run(lambda: state.service.volume(symbol, days), as_json=as_json)


@app.command()
def breadth(on: str | None = typer.Option(None, "--date", help="yyyy-MM-dd"),
            as_json: bool = JSON) -> None:
    """Market breadth for a date (holidays resolve upstream)."""
    _run(lambda: state.service.breadth(on), as_json=as_json)


@app.command()
def live(symbol: str, as_json: bool = JSON) -> None:
    """Intraday quote (cm-market) with freshness check."""
    _run(lambda: state.service.live_quote(symbol), as_json=as_json)


@app.command()
def movers(top: int = typer.Option(5, "--top", min=1, max=50), as_json: bool = JSON) -> None:
    """Intraday gainers/losers (cm-market)."""
    _run(lambda: state.service.movers(top), as_json=as_json)


@app.command()
def demo(as_json: bool = JSON) -> None:
    """Run the six video queries end-to-end."""
    _run(lambda: state.service.demo(), as_json=as_json)


@app.command()
def ask(question: str, base_url: str | None = typer.Option(None, "--base-url"),
        model: str | None = typer.Option(None, "--model")) -> None:
    """Natural-language question via an OpenAI-compatible model ([ai] extra)."""
    from .agent import NSEAgent, build_openai_model

    cfg = state.cfg
    client = build_openai_model(base_url or cfg.ai_base_url)
    agent = NSEAgent.build(client, model or cfg.ai_model,
                           [state.service.eod, state.service.live])
    reply = agent.ask(question)
    console.print(reply.answer)
    if state.verbose:
        err_console.print(f"[dim]rounds={reply.rounds} tool_calls={len(reply.tool_calls)}[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
