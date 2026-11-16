"""Query service layer (spec 4.3). Business rules only: no Typer/Rich imports."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from nse_mcp import NSEMCPClient, PayloadError, ToolError
from nse_mcp.errors import SymbolNotFound
from nse_mcp.models import (
    Breadth,
    BulkQuote,
    CompareResult,
    CorporateActions,
    Week52,
    VolumeAnalysis,
    parse_model,
)
from nse_mcp.validate import normalize_symbol, normalize_symbols, validate_date, validate_int_range

CA_GUARD_MONTHS = 3          # spec 4.3: history/compare with months > 3 fetch corporate actions
WATCHLIST_MAX = 50
COMPARE_MIN, COMPARE_MAX = 2, 10
COMPARE_MONTHS = (1, 24)
VOLUME_DAYS = (5, 252)
DEFAULT_SMA_WINDOWS = (50, 200)
LIVE_STALE_SECONDS = 5 * 60  # spec 6: stale beyond expected 1-3 min delay + margin


def _subtract_months(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, _days_in_month(y, m))
    return date(y, m, day)


def _days_in_month(y: int, m: int) -> int:
    nxt = date(y + (m // 12), m % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def _ca_window(months: int) -> tuple[str, str]:
    today = date.today()
    return _subtract_months(today, months).isoformat(), today.isoformat()


def _json(text: str, tool: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"{tool}: non-JSON payload") from exc


def _raise_if_error(result, tool: str, sym: str | None = None) -> None:
    """Map a tool result to an exception.

    Verified live: NSE returns HTTP 200 with {"error": "Symbol not found: X"} and
    is_error=False for unknown symbols, so the payload is inspected too (spec 13).
    """
    if result.is_error:
        raise _symbol_error(result, sym or "?", tool)
    text = result.text.strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict) and "error" in payload:
            raise _symbol_error(result, sym or "?", tool)
    if text in ("null", ""):
        raise SymbolNotFound(f"{sym or '?'}: empty upstream result", tool=tool)


# --------------------------------------------------------------------- results
@dataclass
class Watchlist:
    quotes: list[BulkQuote]
    missing: list[str]


@dataclass
class HistoryResult:
    symbol: str
    series: list[dict]
    summary: dict
    corporate_actions: list[dict] = field(default_factory=list)
    ca_warning: str | None = None
    trading_days: int = 0


@dataclass
class SMAVerdict:
    symbol: str
    close: float
    windows: dict[int, float]
    verdicts: dict[int, str]          # ABOVE / BELOW


@dataclass
class CompareOutcome:
    raw: CompareResult
    corporate_actions: dict[str, list[dict]]
    warning: str | None


# ----------------------------------------------------------------------- services
class NSEService:
    def __init__(self, bhavcopy: NSEMCPClient, cmmkt: NSEMCPClient | None = None) -> None:
        self.eod = bhavcopy
        self.live = cmmkt

    # watchlist
    def watchlist(self, symbols: list[str]) -> Watchlist:
        syms = normalize_symbols(symbols, 1, WATCHLIST_MAX)
        r = self.eod.call("get_bulk_quote", {"symbols": syms})
        _raise_if_error(r, "get_bulk_quote")
        payload = _json(r.text, "get_bulk_quote")
        quotes, missing = [], []
        for q in payload.get("quotes", []):
            if q.get("error"):
                missing.append(q.get("symbol", "?"))
            else:
                quotes.append(parse_model(BulkQuote, q))
        return Watchlist(quotes, missing)

    # history with CA guard
    def history(self, symbol: str, months: int) -> HistoryResult:
        sym = normalize_symbol(symbol)
        validate_int_range("months", months, 1, 60)
        chunks = self.eod.paged_history(sym, months)
        series: list[dict] = []
        for ch in chunks:
            series.extend(ch.get("data", []))
        series.sort(key=lambda row: row.get("date", ""))
        summary = _history_summary(series)
        result = HistoryResult(sym, series, summary,
                               trading_days=len(series))
        if months > CA_GUARD_MONTHS:
            cas, warning = self._corporate_actions(sym, months)
            result.corporate_actions = cas
            result.ca_warning = warning
        return result

    def _corporate_actions(self, symbol: str, months: int) -> tuple[list[dict], str | None]:
        frm, to = _ca_window(months)
        r = self.eod.call("get_corporate_actions",
                          {"symbol": symbol, "fromDate": frm, "toDate": to})
        try:
            _raise_if_error(r, "get_corporate_actions", symbol)
        except ToolError as exc:
            return [], f"corporate actions unavailable for {symbol}: {str(exc)[:120]}"
        ca = parse_model(CorporateActions, _json(r.text, "get_corporate_actions"))
        events = [a.model_dump() for a in ca.actions if a.adjustmentFactor != 1.0]
        if events:
            return events, (f"{len(events)} split/bonus event(s) in window: prices before the "
                            "ex-date are unadjusted")
        return [], None

    # sma
    def sma_check(self, symbol: str, windows: tuple[int, ...] = DEFAULT_SMA_WINDOWS) -> SMAVerdict:
        sym = normalize_symbol(symbol)
        for w in windows:
            validate_int_range("sma window", w, 2, 252)
        close = self._close(sym)
        values: dict[int, float] = {}
        verdicts: dict[int, str] = {}
        for w in windows:
            r = self.eod.call("moving_average", {"symbol": sym, "days": w})
            _raise_if_error(r, "moving_average", sym)
            raw = _json(r.text, "moving_average")
            # Payload quirk (spec 4.3): bare JSON scalar, but accept {"value": ...} too.
            if raw is None:
                raise SymbolNotFound(f"{sym}: moving_average returned null", tool="moving_average")
            if isinstance(raw, bool) or not isinstance(raw, (int, float, dict)):
                raise PayloadError(f"moving_average({sym}): unexpected payload {raw!r}")
            sma = float(raw["value"] if isinstance(raw, dict) else raw)
            if not 1.0 < sma < 1_000_000.0:
                raise PayloadError(f"moving_average({sym},{w})={sma} outside plausible range")
            values[w] = sma
            verdicts[w] = "ABOVE" if close > sma else "BELOW"
        return SMAVerdict(sym, close, values, verdicts)

    def _close(self, sym: str) -> float:
        wl = self.watchlist([sym])
        if not wl.quotes:
            raise SymbolNotFound(f"no quote for {sym}", tool="get_bulk_quote")
        close = wl.quotes[0].close
        if close is None:
            raise PayloadError(f"no close price for {sym}")
        return close

    # 52 week
    def range52(self, symbol: str) -> Week52:
        sym = normalize_symbol(symbol)
        r = self.eod.call("get_52_week_high_low", {"symbol": sym})
        _raise_if_error(r, "get_52_week_high_low", sym)
        return parse_model(Week52, _json(r.text, "get_52_week_high_low"))

    # compare
    def compare(self, symbols: list[str], months: int) -> CompareOutcome:
        syms = normalize_symbols(symbols, COMPARE_MIN, COMPARE_MAX)
        validate_int_range("months", months, *COMPARE_MONTHS)
        r = self.eod.call("compare_stocks", {"symbols": syms, "months": months})
        _raise_if_error(r, "compare_stocks")  # per-symbol misses are rendered, not raised
        raw = parse_model(CompareResult, _json(r.text, "compare_stocks"))
        cas: dict[str, list[dict]] = {}
        warning = None
        if months > CA_GUARD_MONTHS:
            for s in syms:
                events, w = self._corporate_actions(s, months)
                cas[s] = events
                warning = warning or w
        return CompareOutcome(raw, cas, warning)

    # volume
    def volume(self, symbol: str, days: int = 30) -> VolumeAnalysis:
        sym = normalize_symbol(symbol)
        validate_int_range("days", days, *VOLUME_DAYS)
        r = self.eod.call("get_volume_analysis", {"symbol": sym, "days": days})
        _raise_if_error(r, "get_volume_analysis", sym)
        return parse_model(VolumeAnalysis, _json(r.text, "get_volume_analysis"))

    # breadth
    def breadth(self, on: str | None = None) -> Breadth:
        d = validate_date(on or date.today().isoformat(), allow_today=False)
        r = self.eod.call("get_market_breadth", {"date": d}, allow_stale=True)
        _raise_if_error(r, "get_market_breadth")
        return parse_model(Breadth, _json(r.text, "get_market_breadth"))

    # live (cm-market)
    def live_quote(self, symbol: str) -> dict:
        sym = normalize_symbol(symbol)
        client = self._require_live()
        status = self._data_status(client)
        r = client.call("cm_get_stock_quote", {"symbol": sym})
        _raise_if_error(r, "cm_get_stock_quote", sym)
        payload = _json(r.text, "cm_get_stock_quote")
        return {"quote": payload, "data_status": status,
                "stale": _is_stale(payload.get("updatedAt"))}

    def movers(self, top: int = 5) -> dict:
        validate_int_range("top", top, 1, 50)
        client = self._require_live()
        status = self._data_status(client)
        r = client.call("nse_get_market_movers", {"indexName": "", "limit": top})
        _raise_if_error(r, "nse_get_market_movers")
        payload = _json(r.text, "nse_get_market_movers")
        return {"movers": payload, "data_status": status,
                "stale": _is_stale(payload.get("gainersUpdatedAt"))}

    def _data_status(self, client: NSEMCPClient) -> dict:
        r = client.call("cm_get_data_status", {})
        _raise_if_error(r, "cm_get_data_status")
        return _json(r.text, "cm_get_data_status")

    def _require_live(self) -> NSEMCPClient:
        if self.live is None:
            raise ToolError("cmmkt client not configured")
        return self.live

    # demo
    def demo(self) -> dict[str, Any]:
        """The six video queries end-to-end (spec 4.3, 'build on top' segment)."""
        out: dict[str, Any] = {}
        out["watchlist"] = self.watchlist(["RELIANCE", "TCS", "HDFCBANK", "INFY", "TATAMOTORS"])
        out["history"] = self.history("INFY", 1)
        out["sma"] = self.sma_check("HINDALCO", (50,))
        out["range52"] = self.range52("RELIANCE")
        out["compare"] = self.compare(["HDFCBANK", "ICICIBANK"], 6)
        out["volume"] = self.volume("SUZLON", 30)
        return out


def _history_summary(series: list[dict]) -> dict:
    if not series:
        return {}
    closes = [row["close"] for row in series if isinstance(row.get("close"), (int, float))]
    if not closes:
        return {"trading_days": len(series)}
    first, last = closes[0], closes[-1]
    return {"trading_days": len(series), "first_close": first, "last_close": last,
            "period_high": max(closes), "period_low": min(closes),
            "return_pct": round((last / first - 1) * 100, 2) if first else None}


def _symbol_error(result, sym: str, tool: str = "upstream") -> Exception:
    """Build the right exception for a symbol / tool failure (exit 4 with suggestions)."""
    text = result.text or ""
    try:
        payload = json.loads(text)
        msg = payload.get("error") if isinstance(payload, dict) else None
        msg = msg or text
    except json.JSONDecodeError:
        msg = text
    if "not found" in str(msg).lower():
        return SymbolNotFound(f"{sym}: {msg}", suggestions=[], tool=tool)
    return ToolError(str(msg)[:300], tool)


def suggest_symbols(eod, query: str, limit: int = 5) -> list[str]:
    """Spec 13: on symbol-not-found, suggest close matches via search_symbols."""
    try:
        r = eod.call("search_symbols", {"query": query})
        payload = json.loads(r.text)
        return [row["symbol"] for row in payload.get("results", [])[:limit]]
    except Exception:  # suggestions are best-effort only
        return []


def _is_stale(iso_ts: str | None, now_s: float | None = None) -> bool:
    if not iso_ts:
        return True
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")[:32])
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc).timestamp() if now_s is None else now_s
    return now - ts.timestamp() > LIVE_STALE_SECONDS
