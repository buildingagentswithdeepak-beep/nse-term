"""Disk cache (spec 4.6) and cache policy.

Policy:
  * dated EOD queries (any date argument other than 'today') -> immutable, TTL infinite
  * latest / 'today' EOD queries -> latest_ttl_s (default 900 s)
  * cm-market (cmmkt) tools -> never cached (live data)
Entries: JSON files named sha256(server|tool|canonical_args).json, see spec 7.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .validate import DATE_RE

NEVER = None
FOREVER = float("inf")


def ttl_for(server: str, tool: str, args: dict[str, Any], latest_ttl_s: float = 900.0) -> float | None:
    """Spec 4.6: FOREVER for dated EOD data, latest_ttl_s for latest/undated EOD, None for cmmkt.

    A dated argument is immutable only if it is a real yyyy-MM-dd date; any 'today' or
    undated call is 'latest' data and expires.
    """
    if server == "cmmkt":
        return NEVER
    values = [v for v in args.values() if isinstance(v, str)]
    if any(v == "today" for v in values):
        return latest_ttl_s
    if any(DATE_RE.match(v) for v in values):
        return FOREVER
    return latest_ttl_s


class DiskCache:
    def __init__(self, directory: str | os.PathLike[str], latest_ttl_s: float = 900.0,
                 enabled: bool = True, clock=time.time) -> None:
        self.dir = Path(directory).expanduser()
        self.latest_ttl_s = latest_ttl_s
        self.enabled = enabled
        self._clock = clock

    @staticmethod
    def key(server: str, tool: str, args: dict[str, Any]) -> str:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
        raw = f"{server}|{tool}|{canonical}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _path(self, server: str, tool: str, args: dict[str, Any]) -> Path:
        return self.dir / f"{self.key(server, tool, args)}.json"

    def _read(self, server, tool, args) -> dict | None:
        if not self.enabled:
            return None
        p = self._path(server, tool, args)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def get(self, server: str, tool: str, args: dict[str, Any], ttl: float) -> str | None:
        entry = self._read(server, tool, args)
        if entry is None:
            return None
        if ttl != FOREVER and self._clock() - entry["stored_at"] > ttl:
            return None
        return entry["payload"]

    def get_stale(self, server: str, tool: str, args: dict[str, Any]) -> str | None:
        """Any cached entry regardless of TTL (spec 13 fallback)."""
        entry = self._read(server, tool, args)
        return None if entry is None else entry["payload"]

    def put(self, server: str, tool: str, args: dict[str, Any], payload: str) -> None:
        if not self.enabled:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "key": f"{server}|{tool}|{json.dumps(args, sort_keys=True, separators=(',', ':'))}",
            "stored_at": self._clock(),
            "stored_at_iso": datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat(),
            "payload": payload,
        }
        tmp = self._path(server, tool, args).with_suffix(".tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(self._path(server, tool, args))
