"""Config loading (spec 7): TOML file + NSE_TERM_* env vars. No secrets required."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from nse_mcp import BHAVCOPY_URL, CMMKT_URL

DEFAULT_CONFIG_PATH = Path("~/.config/nse-term/config.toml").expanduser()
DEFAULT_CACHE_DIR = Path("~/.cache/nse-term").expanduser()


@dataclass
class Config:
    bhavcopy_url: str = BHAVCOPY_URL
    cmmkt_url: str = CMMKT_URL
    max_attempts: int = 8
    base_delay_s: float = 2.0
    read_timeout_s: float = 90.0
    cache_enabled: bool = True
    cache_dir: Path = DEFAULT_CACHE_DIR
    latest_ttl_s: float = 900.0
    watchlist: list[str] = field(default_factory=lambda: ["RELIANCE", "TCS", "INFY"])
    ai_base_url: str = "http://localhost:11434/v1"
    ai_model: str = "gemma4:31b-cloud"
    insecure_dev: bool = False


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    p = path or Path(env.get("NSE_TERM_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        servers = data.get("servers", {})
        retry = data.get("retry", {})
        cache = data.get("cache", {})
        cfg.bhavcopy_url = servers.get("bhavcopy_url", cfg.bhavcopy_url)
        cfg.cmmkt_url = servers.get("cmmkt_url", cfg.cmmkt_url)
        cfg.max_attempts = int(retry.get("max_attempts", cfg.max_attempts))
        cfg.base_delay_s = float(retry.get("base_delay_s", cfg.base_delay_s))
        cfg.read_timeout_s = float(retry.get("read_timeout_s", cfg.read_timeout_s))
        cfg.cache_enabled = bool(cache.get("enabled", cfg.cache_enabled))
        if "dir" in cache:
            cfg.cache_dir = Path(cache["dir"]).expanduser()
        cfg.latest_ttl_s = float(cache.get("latest_ttl_s", cfg.latest_ttl_s))
        cfg.watchlist = list(data.get("watchlist", {}).get("symbols", cfg.watchlist))
        ai = data.get("ai", {})
        cfg.ai_base_url = ai.get("base_url", cfg.ai_base_url)
        cfg.ai_model = ai.get("model", cfg.ai_model)
    # Environment overrides (NSE_TERM_*)
    if "NSE_TERM_CACHE_DIR" in env:
        cfg.cache_dir = Path(env["NSE_TERM_CACHE_DIR"]).expanduser()
    if env.get("NSE_TERM_NO_CACHE") == "1":
        cfg.cache_enabled = False
    if "NSE_TERM_AI_BASE_URL" in env:
        cfg.ai_base_url = env["NSE_TERM_AI_BASE_URL"]
    if "NSE_TERM_AI_MODEL" in env:
        cfg.ai_model = env["NSE_TERM_AI_MODEL"]
    if env.get("NSE_TERM_INSECURE_DEV") == "1":
        cfg.insecure_dev = True
    return cfg
