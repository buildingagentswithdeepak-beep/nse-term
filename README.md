# nse-term

Pure-Python terminal client and resilient library for the official NSE India MCP
servers (bhavcopy EOD and cmmkt intraday). No Claude/ChatGPT subscription, no Node,
no Docker, and no NSE credentials are required.

Informational/educational use only. No order placement, no commercial exploitation,
no AI training on the data. Not investment advice.

## Install

    pip install nse-term            # or: pip install nse-term[ai] for the local-AI layer

## Commands

    nse-term tools [--server bhavcopy|cmmkt]
    nse-term watch RELIANCE TCS HDFCBANK INFY TATAMOTORS
    nse-term history INFY --months 1 [--json]
    nse-term sma HINDALCO --windows 50,200
    nse-term range52 RELIANCE
    nse-term compare HDFCBANK ICICIBANK --months 6
    nse-term volume SUZLON --days 30
    nse-term breadth [--date 2026-11-05]
    nse-term live TCS
    nse-term movers --top 5
    nse-term demo
    nse-term ask "Is Hindalco above its 200-DMA?"     # [ai] extra

Exit codes: 0 success, 2 usage error, 3 upstream exhausted / circuit open,
4 upstream tool error (for example an unknown symbol).

## Library

    from nse_mcp import NSEMCPClient, BHAVCOPY_URL
    client = NSEMCPClient(BHAVCOPY_URL)
    res = client.call("get_bulk_quote", {"symbols": ["RELIANCE", "TCS"]})

## Configuration

`~/.config/nse-term/config.toml` (override path with `NSE_TERM_CONFIG`). Environment
variables: `NSE_TERM_CACHE_DIR`, `NSE_TERM_NO_CACHE=1`, `NSE_TERM_AI_BASE_URL`,
`NSE_TERM_AI_MODEL`, `NSE_TERM_AI_API_KEY` (hosted endpoints only),
`NSE_TERM_METRICS_FILE` (JSONL sink), `NSE_TERM_INSECURE_DEV=1`.

## Development

    uv venv --python 3.11 && uv pip install -e ".[dev]"
    pytest                       # offline unit + mock-server tests
    pytest -m live               # live smoke against NSE (nightly / pre-release only)
