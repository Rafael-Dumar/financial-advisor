from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AdvisorConfig:
    stock_watchlist: list[str] = field(default_factory=list)
    crypto_watchlist: list[str] = field(default_factory=list)
    discovery_stock_candidates: list[str] = field(default_factory=list)
    discovery_crypto_candidates: list[str] = field(default_factory=list)
    minimum_stock_market_cap: float = 10_000_000_000
    minimum_crypto_market_cap: float = 5_000_000_000
    account_capital: float = 50_000
    risk_fraction: float = 0.005
    max_risk_fraction: float = 0.01
    max_daily_loss_fraction: float = 0.02
    max_weekly_loss_fraction: float = 0.05
    fmp_api_key: str = ""
    coingecko_api_key: str = ""
    alphavantage_api_key: str = ""
    coinbase_api_key: str = ""
    freshness_seconds: dict[str, int] = field(default_factory=dict)
    api_limits: dict[str, int] = field(default_factory=dict)
    api_run_limits: dict[str, int] = field(default_factory=dict)
    max_stocks_per_run: int | None = None

    @classmethod
    def default(cls, env_file: Path | str | None = None) -> "AdvisorConfig":
        env_file_path = env_file
        if env_file_path is None:
            env_file_path = os.getenv("ADVISOR_ENV_FILE", ".env")
        file_env = _load_env_file(env_file_path if env_file_path else None)
        default_stocks = ["INTC", "AMD", "NVDA", "HIMS", "MU", "MSFT", "USAR", "CRDO", "DELL", "MRVL", "HOOD"]
        default_cryptos = ["SOL", "HYPE", "BTC", "ETH"]
        return cls(
            stock_watchlist=_env_list("ADVISOR_STOCK_WATCHLIST", file_env, default_stocks),
            crypto_watchlist=_env_list("ADVISOR_CRYPTO_WATCHLIST", file_env, default_cryptos),
            discovery_stock_candidates=[
                "AAPL",
                "AVGO",
                "GOOGL",
                "META",
                "AMZN",
                "TSM",
                "ASML",
                "ORCL",
                "CRM",
                "NOW",
            ],
            discovery_crypto_candidates=["BNB", "XRP", "LINK", "AVAX"],
            account_capital=float(_env_value("ADVISOR_ACCOUNT_CAPITAL", file_env, "50000")),
            risk_fraction=float(_env_value("ADVISOR_RISK_FRACTION", file_env, "0.005")),
            max_risk_fraction=0.01,
            max_daily_loss_fraction=float(_env_value("ADVISOR_MAX_DAILY_LOSS_FRACTION", file_env, "0.02")),
            max_weekly_loss_fraction=float(_env_value("ADVISOR_MAX_WEEKLY_LOSS_FRACTION", file_env, "0.05")),
            fmp_api_key=_env_value("FMP_API_KEY", file_env, ""),
            coingecko_api_key=_env_value("COINGECKO_API_KEY", file_env, ""),
            alphavantage_api_key=_env_value("ALPHAVANTAGE_API_KEY", file_env, ""),
            coinbase_api_key=_env_value("COINBASE_API_KEY", file_env, ""),
            freshness_seconds={
                "prices": 60 * 60 * 6,
                "fundamentals": 60 * 60 * 24,
                "earnings": 60 * 60 * 12,
                "crypto_flow": 60 * 15,
                "news": 60 * 60 * 6,
            },
            api_limits={
                "fmp": 250,
                "coingecko": 10_000,
                "binance": 100_000,
                "hyperliquid": 100_000,
                "coinbase": 100_000,
                "alphavantage": 25,
                "sec": 100_000,
                "yahoo": 100_000,
            },
            api_run_limits=_run_limits_from_env(file_env),
            max_stocks_per_run=_env_int_optional("ADVISOR_MAX_STOCKS_PER_RUN", file_env),
        )

    def validate(self, *, allow_missing_keys: bool = False) -> list[str]:
        errors: list[str] = []
        if not self.stock_watchlist:
            errors.append("empty_stock_watchlist")
        if not self.crypto_watchlist:
            errors.append("empty_crypto_watchlist")
        if "HYPE" not in self.crypto_watchlist:
            errors.append("missing_hype_hyperliquid_watchlist_entry")
        for namespace, freshness in sorted(self.freshness_seconds.items()):
            if freshness <= 0:
                errors.append(f"invalid_freshness_{namespace}")
        for provider, limit in sorted(self.api_limits.items()):
            if limit < 0:
                errors.append(f"invalid_api_limit_{provider}")
        for provider, limit in sorted(self.api_run_limits.items()):
            if limit < 0:
                errors.append(f"invalid_api_run_limit_{provider}")
        if self.max_stocks_per_run is not None and self.max_stocks_per_run <= 0:
            errors.append("invalid_max_stocks_per_run")
        if self.risk_fraction <= 0 or self.risk_fraction > self.max_risk_fraction:
            errors.append("invalid_risk_fraction")
        if self.max_daily_loss_fraction <= 0:
            errors.append("invalid_max_daily_loss_fraction")
        if self.max_weekly_loss_fraction <= 0:
            errors.append("invalid_max_weekly_loss_fraction")
        if self.max_daily_loss_fraction > self.max_weekly_loss_fraction:
            errors.append("daily_loss_limit_exceeds_weekly_loss_limit")
        if self.account_capital <= 0:
            errors.append("invalid_account_capital")
        if self.minimum_stock_market_cap <= 0:
            errors.append("invalid_minimum_stock_market_cap")
        if self.minimum_crypto_market_cap <= 0:
            errors.append("invalid_minimum_crypto_market_cap")
        if "PETR4" in self.stock_watchlist:
            errors.append("petr4_outside_v1_without_dedicated_source")
        if _is_placeholder_key(self.fmp_api_key):
            errors.append("placeholder_fmp_api_key")
        if _is_placeholder_key(self.coingecko_api_key):
            errors.append("placeholder_coingecko_api_key")
        if not allow_missing_keys:
            if not self.fmp_api_key:
                errors.append("missing_fmp_api_key")
            if not self.coingecko_api_key:
                errors.append("missing_coingecko_api_key")
        return errors

    def has_live_keys(self) -> bool:
        return bool(
            self.fmp_api_key
            and self.coingecko_api_key
            and not _is_placeholder_key(self.fmp_api_key)
            and not _is_placeholder_key(self.coingecko_api_key)
        )

    def symbols_for_scan(self, *, include_discovery: bool) -> tuple[list[str], list[str]]:
        stocks = list(dict.fromkeys(self.stock_watchlist))
        cryptos = list(dict.fromkeys(self.crypto_watchlist))
        if include_discovery:
            stocks = list(dict.fromkeys([*stocks, *self.discovery_stock_candidates]))
            cryptos = list(dict.fromkeys([*cryptos, *self.discovery_crypto_candidates]))
        if self.max_stocks_per_run is not None:
            stocks = stocks[: self.max_stocks_per_run]
        return stocks, cryptos

    def estimated_live_calls(self, *, include_discovery: bool) -> dict[str, int]:
        stocks, cryptos = self.symbols_for_scan(include_discovery=include_discovery)
        non_hype_crypto_count = len([symbol for symbol in cryptos if symbol != "HYPE"])
        has_news_universe = bool(stocks or cryptos)
        return {
            "fmp": (len(stocks) * 7) + 2,
            "coingecko": len(cryptos),
            "binance": (non_hype_crypto_count * 5) + (1 if non_hype_crypto_count else 0),
            "hyperliquid": 2 if "HYPE" in cryptos else 0,
            "coinbase": non_hype_crypto_count,
            "alphavantage": 1 if self.alphavantage_api_key and has_news_universe else 0,
            "sec": len(stocks),
            "yahoo": 0,
        }


def _env_value(name: str, file_env: dict[str, str], default: str) -> str:
    return os.getenv(name) or file_env.get(name, default)


def _env_list(name: str, file_env: dict[str, str], default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = file_env.get(name)
    if raw is None:
        return list(default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _env_int_optional(name: str, file_env: dict[str, str]) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        raw = file_env.get(name)
    if raw is None or not raw.strip():
        return None
    return int(raw)


def _run_limits_from_env(file_env: dict[str, str]) -> dict[str, int]:
    fmp_limit = _env_int_optional("ADVISOR_FMP_CALL_BUDGET_PER_RUN", file_env)
    if fmp_limit is None:
        return {}
    return {"fmp": fmp_limit}


def _load_env_file(env_file: Path | str | None) -> dict[str, str]:
    if env_file is None:
        return {}
    path = Path(env_file)
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _is_placeholder_key(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(normalized and (normalized.startswith("your_") or normalized == "changeme"))
