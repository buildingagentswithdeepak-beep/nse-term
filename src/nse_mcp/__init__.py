"""nse_mcp: resilient client core for the official NSE India MCP servers."""
from .cache import DiskCache, ttl_for
from .client import BHAVCOPY_URL, CMMKT_URL, NSEMCPClient, ToolResult
from .errors import (
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
from .metrics import Metrics, global_metrics

__all__ = [
    "BHAVCOPY_URL", "CMMKT_URL", "NSEMCPClient", "ToolResult",
    "DiskCache", "ttl_for", "Metrics", "global_metrics",
    "NSEError", "UsageError", "RequestRejected", "DNSFailure", "UpstreamExhausted",
    "CircuitOpenError", "ToolError", "SymbolNotFound", "PayloadError",
]
__version__ = "1.0.0"
