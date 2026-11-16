"""History chunk pager (spec 4.2).

get_stock_history serves at most 3 months per call and returns next_end_date for
older chunks. Chunks are chained sequentially (chunk N needs chunk N-1's date).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .errors import PayloadError, ToolError

if TYPE_CHECKING:  # pragma: no cover
    from .client import NSEMCPClient

MAX_CHUNK_MONTHS = 3


def paged_history(client: NSEMCPClient, symbol: str, total_months: int) -> list[dict]:
    """Fetch N months by chaining 3-month chunks via next_end_date."""
    chunks: list[dict] = []
    end_date = "today"
    remaining = total_months
    seen_ends: set[str] = set()
    while remaining > 0:
        months = min(remaining, MAX_CHUNK_MONTHS)
        r = client.call("get_stock_history",
                        {"symbol": symbol, "months": months, "endDate": end_date})
        if r.is_error:
            raise ToolError(f"get_stock_history({symbol}): {r.text[:200]}", "get_stock_history")
        try:
            payload = json.loads(r.text)
        except json.JSONDecodeError as exc:
            raise PayloadError("get_stock_history returned non-JSON payload") from exc
        chunks.append(payload)
        remaining -= months
        next_end = payload.get("next_end_date") or ""
        if remaining > 0 and not next_end:
            break  # upstream has no older data
        if next_end in seen_ends:
            raise PayloadError(f"pager cycle detected at next_end_date={next_end}")
        seen_ends.add(next_end)
        end_date = next_end
    return chunks
