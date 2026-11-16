"""Unit tests for retry classification, backoff, validation and errors (spec 4.1, 8.2, 11, 13)."""
import random

import httpx
import pytest
from mcp.shared.exceptions import MCPError

from nse_mcp.errors import UsageError
from nse_mcp.resilience import (
    CircuitBreaker,
    backoff_delay,
    exc_retryable,
    is_dns_failure,
    is_timeout,
    msg_retryable,
)
from nse_mcp.validate import (
    normalize_symbol,
    normalize_symbols,
    validate_args,
    validate_date,
    validate_int_range,
)
from tests.conftest import SCHEMAS


# ---- retry classifier -------------------------------------------------------
@pytest.mark.parametrize("msg", ["Session terminated", "Session not found (404)", "connection reset by peer"])
def test_session_and_transport_messages_are_retryable(msg):
    assert msg_retryable(msg)


@pytest.mark.parametrize("msg", ["Invalid params: symbols", "method not found", "validation error on field",
                                 "unknown tool foo"])
def test_validation_and_unknown_tool_are_never_retryable(msg):
    assert not msg_retryable(msg)


def test_non_retryable_marker_wins_over_retryable_marker():
    # "Session not found" plus "invalid params" must NOT retry (spec 13 allowlist precedence).
    assert not msg_retryable("Session not found: invalid params")


def test_exception_group_leaf_walk_retries_session_terminated():
    grp = BaseExceptionGroup("tg", [MCPError(-32600, "Session terminated")])
    assert exc_retryable(grp)


def test_exception_group_with_validation_leaf_is_not_retryable():
    grp = BaseExceptionGroup("tg", [ValueError("ok"), MCPError(-32602, "Invalid params")])
    assert not exc_retryable(grp)


def test_httpx_transport_error_is_retryable_but_dns_is_not():
    assert exc_retryable(httpx.ConnectError("connection refused"))
    dns = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    assert is_dns_failure(dns)
    assert not exc_retryable(dns)


def test_timeout_detection():
    assert is_timeout(httpx.ReadTimeout("slow"))
    assert not is_timeout(ValueError("x"))


# ---- backoff ----------------------------------------------------------------
def test_backoff_is_base_times_attempt_plus_bounded_jitter():
    rng = random.Random(42)
    for attempt in range(1, 9):
        d = backoff_delay(attempt, 2.0, rng)
        assert 2.0 * attempt <= d <= 2.0 * attempt + 1.0


def test_backoff_grows_with_attempt():
    rng = random.Random(1)
    assert backoff_delay(5, 2.0, rng) > backoff_delay(1, 2.0, random.Random(1)) + 7


# ---- circuit breaker --------------------------------------------------------
def test_circuit_opens_after_three_consecutive_failures_and_closes_on_success():
    cb = CircuitBreaker(threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open
    with pytest.raises(Exception) as ei:
        cb.allow()
    assert "circuit is open" in str(ei.value)
    cb.record_success()
    assert not cb.is_open


# ---- symbol & argument validation (spec 11, 8.2) ----------------------------
@pytest.mark.parametrize("raw,expected", [(" reliance ", "RELIANCE"), ("M&M", "M&M"), ("BAJAJ-AUTO", "BAJAJ-AUTO")])
def test_normalize_symbol_uppercases_and_accepts_allowed_chars(raw, expected):
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize("bad", ["", "RELI ANCE", "TCS;DROP", "a" * 21, "REL$"])
def test_normalize_symbol_rejects_bad_input(bad):
    with pytest.raises(UsageError):
        normalize_symbol(bad)


def test_normalize_symbols_enforces_count_bounds():
    with pytest.raises(UsageError):
        normalize_symbols(["TCS"], 2, 10)
    assert normalize_symbols(["tcs", "infy"], 2, 10) == ["TCS", "INFY"]


def test_validate_date_accepts_real_dates_and_today_only_when_allowed():
    assert validate_date("2026-11-05") == "2026-11-05"
    assert validate_date("today") == "today"
    with pytest.raises(UsageError):
        validate_date("today", allow_today=False)
    with pytest.raises(UsageError):
        validate_date("2026-02-30")  # not a real calendar day
    with pytest.raises(UsageError):
        validate_date("05-11-2026")


def test_validate_int_range_rejects_bools_and_out_of_range():
    assert validate_int_range("days", 30, 5, 252) == 30
    with pytest.raises(UsageError):
        validate_int_range("days", True, 5, 252)
    with pytest.raises(UsageError):
        validate_int_range("days", 4, 5, 252)


def test_validate_args_rejects_unknown_and_missing_arguments():
    schema = SCHEMAS["moving_average"]
    validate_args("moving_average", schema, {"symbol": "TCS", "days": 50})
    with pytest.raises(UsageError, match="unknown argument"):
        validate_args("moving_average", schema, {"symbol": "TCS", "days": 50, "extra": 1})
    with pytest.raises(UsageError, match="missing required"):
        validate_args("moving_average", schema, {"symbol": "TCS"})


def test_validate_args_checks_declared_types_and_array_items():
    schema = SCHEMAS["compare_stocks"]
    with pytest.raises(UsageError, match="must be integer"):
        validate_args("compare_stocks", schema, {"symbols": ["A", "B"], "months": "6"})
    with pytest.raises(UsageError, match="items of"):
        validate_args("compare_stocks", schema, {"symbols": ["A", 3], "months": 6})


def test_validate_args_unknown_tool_is_rejected_locally():
    with pytest.raises(UsageError, match="unknown tool"):
        validate_args("no_such_tool", None, {})
