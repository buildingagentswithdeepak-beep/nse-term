"""Retry classification, backoff math and the per-process circuit breaker (spec 4.1, 13)."""
from __future__ import annotations

import random
import socket
import threading

import httpx
from mcp.shared.exceptions import MCPError

from .errors import CircuitOpenError

# Retry ONLY transport/session failures. Checked before retryable markers.
RETRYABLE_MARKERS = ("session terminated", "session not found", "timed out",
                     "connect", "reset")
NON_RETRYABLE_MARKERS = ("invalid params", "method not found", "validation",
                         "unknown tool", "invalid request params")
DNS_MARKERS = ("getaddrinfo", "nodename nor servname", "name or service not known")

CIRCUIT_THRESHOLD = 3
MAX_TIMEOUTS_PER_CALL = 2  # spec 13: retryable once, then circuit message


def backoff_delay(attempt: int, base_delay: float, rng: random.Random | None = None) -> float:
    """Spec 4.1: base_delay * attempt + U(0,1)."""
    r = rng or random
    return base_delay * attempt + r.uniform(0, 1)


def msg_retryable(msg: str) -> bool:
    m = msg.lower()
    if any(k in m for k in NON_RETRYABLE_MARKERS):
        return False
    return any(k in m for k in RETRYABLE_MARKERS)


def leaves(exc: BaseException):
    """Yield leaf exceptions, walking anyio/BaseExceptionGroup trees."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from leaves(sub)
    else:
        yield exc


def is_dns_failure(exc: BaseException) -> bool:
    """True when the failure is name resolution (non-retryable per spec 4.1)."""
    for leaf in leaves(exc):
        cur: BaseException | None = leaf
        depth = 0
        while cur is not None and depth < 6:
            if isinstance(cur, socket.gaierror):
                return True
            if any(k in str(cur).lower() for k in DNS_MARKERS):
                return True
            cur = cur.__cause__ or cur.__context__
            depth += 1
    return False


def is_timeout(exc: BaseException) -> bool:
    return any(isinstance(leaf, (httpx.TimeoutException, TimeoutError))
               for leaf in leaves(exc))


def exc_retryable(exc: BaseException) -> bool:
    """Classifier for the SDK transport. Validation/unknown-tool errors never retry."""
    if is_dns_failure(exc):
        return False
    for leaf in leaves(exc):
        msg = str(leaf).lower()
        if any(k in msg for k in NON_RETRYABLE_MARKERS):
            return False
        if isinstance(leaf, MCPError) and msg_retryable(msg):
            return True
        if isinstance(leaf, (httpx.TransportError, TimeoutError)):
            return True
    return False


class CircuitBreaker:
    """Opens after CIRCUIT_THRESHOLD consecutive exhausted calls; stays open for the process."""

    def __init__(self, threshold: int = CIRCUIT_THRESHOLD) -> None:
        self.threshold = threshold
        self._failures = 0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._failures >= self.threshold

    def allow(self) -> None:
        if self.is_open:
            raise CircuitOpenError(
                "NSE MCP circuit is open after repeated exhausted calls in this process; "
                "not calling NSE again. Re-run in a minute.")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1


_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def breaker_for(url: str) -> CircuitBreaker:
    """One breaker per upstream URL, shared by every client in the process."""
    with _registry_lock:
        if url not in _registry:
            _registry[url] = CircuitBreaker()
        return _registry[url]


def reset_breakers() -> None:
    with _registry_lock:
        _registry.clear()
