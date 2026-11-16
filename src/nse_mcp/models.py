"""Typed payload models (spec 7). extra='allow' so NSE can add fields without breaking us."""
from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import PayloadError

T = TypeVar("T", bound=BaseModel)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Week52(_Payload):
    symbol: str
    last_close: float | None = None
    high_52w: float | None = Field(None, alias="52w_high")
    high_52w_date: str | None = Field(None, alias="52w_high_date")
    low_52w: float | None = Field(None, alias="52w_low")
    low_52w_date: str | None = Field(None, alias="52w_low_date")
    position_in_range_pct: float | None = None
    from_52w_high_pct: float | None = None
    from_52w_low_pct: float | None = None
    trading_days: int | None = None


class BulkQuote(_Payload):
    symbol: str
    close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    pct_change: float | None = None
    volume: int | None = None
    date: str | None = None
    error: str | None = None


class CorporateAction(_Payload):
    exDate: str
    actionType: str = ""
    purpose: str = ""
    adjustmentFactor: float = 1.0


class CorporateActions(_Payload):
    symbol: str
    count: int = 0
    actions: list[CorporateAction] = Field(default_factory=list)


class CompareStock(_Payload):
    symbol: str
    start_price: float | None = None
    end_price: float | None = None
    return_pct: float | None = None
    # Live field name is "max_drawdown" (verified 2026-11-06); accept the spec alias too.
    max_drawdown: float | None = Field(None, alias="max_drawdown_pct")
    trading_days: int | None = None


class CompareResult(_Payload):
    from_date: str | None = None
    to_date: str | None = None
    months: int | None = None
    stocks: list[CompareStock] = Field(default_factory=list)
    best_performer: str | None = None
    worst_performer: str | None = None


class VolumeAnalysis(_Payload):
    symbol: str
    days_analysed: int | None = None
    avg_volume: float | None = None
    max_volume: float | None = None
    min_volume: float | None = None
    last_volume: float | None = None
    last_vs_avg_pct: float | None = None


class Breadth(_Payload):
    date: str
    total_stocks: int | None = None
    advances: int | None = None
    declines: int | None = None
    unchanged: int | None = None
    ad_ratio: float | None = None
    total_volume: int | None = None


class HistoryChunk(_Payload):
    symbol: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    trading_days: int = 0
    data: list[dict[str, Any]] = Field(default_factory=list)
    next_end_date: str | None = None


def parse_model(model: type[T], data: Any) -> T:
    """Validate a payload; shape mismatches surface as PayloadError (exit 4)."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise PayloadError(f"unexpected {model.__name__} payload: {exc.errors()[0]['msg']}") from exc
