"""Exception hierarchy shared by the client core and the query layer.

Exit-code mapping lives in nse_term.cli (spec 4.4):
  UsageError -> 2, UpstreamExhausted/DNSFailure -> 3, ToolError/RequestRejected -> 4.
"""
from __future__ import annotations


class NSEError(Exception):
    """Base class for every error raised by nse_mcp / nse_term."""


class UsageError(NSEError):
    """Caller supplied invalid input (bad symbol, out-of-range arg, insecure URL)."""


class RequestRejected(NSEError):
    """Upstream rejected the request with a non-retryable error (invalid params, unknown tool)."""


class DNSFailure(NSEError):
    """Upstream host cannot be resolved. Non-retryable by spec 4.1."""


class UpstreamExhausted(NSEError):
    """All retry levels failed for a logical call."""


class CircuitOpenError(UpstreamExhausted):
    """Circuit breaker is open: too many consecutive exhausted calls in this process."""


class ToolError(NSEError):
    """Upstream answered, but the tool reported an error (is_error=True or error payload)."""

    def __init__(self, message: str, tool: str | None = None) -> None:
        super().__init__(message)
        self.tool = tool


class SymbolNotFound(ToolError):
    """Symbol unknown to upstream. Carries search_symbols suggestions (spec 13)."""

    def __init__(self, message: str, suggestions: list[str] | None = None,
                 tool: str | None = None) -> None:
        super().__init__(message, tool)
        self.suggestions = suggestions or []


class PayloadError(ToolError):
    """Upstream payload did not match the expected shape."""
