"""Per-run metrics and optional JSONL sink (spec 12)."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field


@dataclass
class Metrics:
    calls: int = 0
    retries: int = 0          # fresh sessions beyond the first + same-session re-sends
    resends: int = 0
    cache_hits: int = 0
    errors: int = 0
    elapsed_s: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _sink: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._sink = os.environ.get("NSE_TERM_METRICS_FILE") or None

    def record(self, result) -> None:
        with self._lock:
            self.calls += 1
            self.resends += getattr(result, "resends", 0)
            self.retries += max(getattr(result, "attempts", 1) - 1, 0) + getattr(result, "resends", 0)
            self.elapsed_s += getattr(result, "elapsed_s", 0.0)
        self._append({"evt": "tool_call", "tool": result.tool, "attempts": result.attempts,
                      "resends": result.resends, "ms": round(result.elapsed_s * 1000),
                      "cached": result.cached, "is_error": result.is_error})

    def record_cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def record_error(self) -> None:
        with self._lock:
            self.errors += 1

    def summary(self) -> dict:
        return {"calls": self.calls, "retries": self.retries, "resends": self.resends,
                "cache_hits": self.cache_hits, "errors": self.errors,
                "wall_time_s": round(self.elapsed_s, 3)}

    def _append(self, event: dict) -> None:
        if not self._sink:
            return
        try:
            with open(self._sink, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError:
            pass  # metrics must never break a user command


global_metrics = Metrics()
