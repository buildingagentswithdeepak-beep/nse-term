"""Local validation before any network I/O (spec 8.2, 11)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .errors import UsageError

SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_symbol(symbol: str) -> str:
    """Uppercase and validate against ^[A-Z0-9&-]{1,20}$ (spec 11)."""
    if not isinstance(symbol, str):
        raise UsageError("symbol must be a string")
    s = symbol.strip().upper()
    if not SYMBOL_RE.match(s):
        raise UsageError(f"invalid symbol {symbol!r}: expected ^[A-Z0-9&-]{{1,20}}$")
    return s


def normalize_symbols(symbols: list[str], min_count: int, max_count: int) -> list[str]:
    if not (min_count <= len(symbols) <= max_count):
        raise UsageError(f"expected {min_count}-{max_count} symbols, got {len(symbols)}")
    return [normalize_symbol(s) for s in symbols]


def validate_date(value: str, allow_today: bool = True) -> str:
    """Accepts yyyy-MM-dd (real calendar date) or 'today' when allowed."""
    if allow_today and value == "today":
        return value
    if not DATE_RE.match(value or ""):
        raise UsageError(f"invalid date {value!r}: expected yyyy-MM-dd")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise UsageError(f"invalid date {value!r}: {exc}") from exc
    return value


def validate_int_range(name: str, value: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise UsageError(f"{name} must be an integer in {lo}-{hi}, got {value!r}")
    return value


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate_args(tool: str, schema: dict[str, Any] | None, args: dict[str, Any]) -> None:
    """Validate args against an upstream JSON Schema (spec 8.2: additionalProperties false)."""
    if schema is None:
        raise UsageError(f"unknown tool {tool!r}")
    if not isinstance(args, dict):
        raise UsageError(f"{tool}: arguments must be an object")
    props: dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            raise UsageError(f"{tool}: missing required argument {key!r}")
    if schema.get("additionalProperties", True) is False:
        unknown = sorted(set(args) - set(props))
        if unknown:
            raise UsageError(f"{tool}: unknown argument(s) {unknown}")
    for key, value in args.items():
        if key in props:
            _check_type(tool, key, value, props[key])


def _check_type(tool: str, key: str, value: Any, spec: dict[str, Any]) -> None:
    t = spec.get("type")
    if t in _TYPE_CHECKS and not _TYPE_CHECKS[t](value):
        raise UsageError(f"{tool}: argument {key!r} must be {t}")
    if t == "array" and "items" in spec:
        item_type = spec["items"].get("type")
        if item_type in _TYPE_CHECKS:
            for item in value:
                if not _TYPE_CHECKS[item_type](item):
                    raise UsageError(f"{tool}: items of {key!r} must be {item_type}")
