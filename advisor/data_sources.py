from __future__ import annotations

from urllib.parse import urlencode


class FmpSource:
    base_url = "https://financialmodelingprep.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def historical_prices_url(self, symbol: str) -> str:
        return self._url("/stable/historical-price-eod/full", {"symbol": symbol, "apikey": self.api_key})

    def historical_prices_light_url(self, symbol: str) -> str:
        return self._url("/stable/historical-price-eod/light", {"symbol": symbol, "apikey": self.api_key})

    def profile_url(self, symbol: str) -> str:
        return self._url("/stable/profile", {"symbol": symbol, "apikey": self.api_key})

    def key_metrics_url(self, symbol: str) -> str:
        return self._url("/stable/key-metrics-ttm", {"symbol": symbol, "apikey": self.api_key})

    def historical_key_metrics_url(self, symbol: str) -> str:
        return self._url(
            "/stable/key-metrics",
            {"symbol": symbol, "period": "annual", "limit": "5", "apikey": self.api_key},
        )

    def income_statement_growth_url(self, symbol: str) -> str:
        return self._url(
            "/stable/income-statement-growth",
            {"symbol": symbol, "period": "annual", "limit": "5", "apikey": self.api_key},
        )

    def ratios_url(self, symbol: str) -> str:
        return self._url("/stable/ratios-ttm", {"symbol": symbol, "apikey": self.api_key})

    def earnings_calendar_url(self, symbol: str) -> str:
        return self._url("/stable/earnings-calendar", {"symbol": symbol, "apikey": self.api_key})

    def _url(self, path: str, params: dict[str, str]) -> str:
        return f"{self.base_url}{path}?{urlencode(params)}"


class StooqSource:
    base_url = "https://stooq.com/q/d/l/"

    def daily_csv_url(self, symbol: str) -> str:
        return f"{self.base_url}?{urlencode({'s': symbol.lower() + '.us', 'i': 'd'})}"


class YahooChartSource:
    base_url = "https://query1.finance.yahoo.com"

    def daily_chart_url(self, symbol: str, range_value: str = "1y", interval: str = "1d") -> str:
        return f"{self.base_url}/v8/finance/chart/{symbol}?{urlencode({'range': range_value, 'interval': interval})}"


class BinanceSource:
    base_url = "https://fapi.binance.com"

    def klines_url(self, symbol: str, interval: str = "1d", limit: int = 500) -> str:
        return self._url("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": str(limit)})

    def funding_rate_url(self, symbol: str, limit: int = 100) -> str:
        return self._url("/fapi/v1/fundingRate", {"symbol": symbol, "limit": str(limit)})

    def funding_info_url(self) -> str:
        return f"{self.base_url}/fapi/v1/fundingInfo"

    def open_interest_url(self, symbol: str) -> str:
        return self._url("/fapi/v1/openInterest", {"symbol": symbol})

    def open_interest_history_url(self, symbol: str, period: str = "1d", limit: int = 30) -> str:
        return self._url(
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "limit": str(limit)},
        )

    def taker_long_short_url(self, symbol: str, period: str = "5m", limit: int = 100) -> str:
        return self._url(
            "/futures/data/takerlongshortRatio",
            {"symbol": symbol, "period": period, "limit": str(limit)},
        )

    def liquidation_orders_url(self, symbol: str, limit: int = 100) -> str:
        return self._url("/fapi/v1/allForceOrders", {"symbol": symbol, "limit": str(limit)})

    def _url(self, path: str, params: dict[str, str]) -> str:
        return f"{self.base_url}{path}?{urlencode(params)}"


class CoinGeckoSource:
    base_url = "https://api.coingecko.com/api/v3"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def markets_url(self, ids: list[str], vs_currency: str = "usd") -> str:
        params = {
            "vs_currency": vs_currency,
            "ids": ",".join(ids),
            "order": "market_cap_desc",
            "sparkline": "false",
        }
        return f"{self.base_url}/coins/markets?{urlencode(params)}"


class HyperliquidSource:
    def info_url(self) -> str:
        return "https://api.hyperliquid.xyz/info"

    def meta_and_asset_contexts_payload(self) -> dict[str, str]:
        return {"type": "metaAndAssetCtxs"}

    def candle_snapshot_payload(
        self,
        coin: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        interval: str = "1d",
    ) -> dict[str, object]:
        return {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_time_ms,
                "endTime": end_time_ms,
            },
        }


class CoinbaseSource:
    base_url = "https://api.coinbase.com"

    def public_candles_url(self, product_id: str) -> str:
        return f"{self.base_url}/api/v3/brokerage/market/products/{product_id}/candles"

    def public_product_url(self, product_id: str) -> str:
        return f"{self.base_url}/api/v3/brokerage/market/products/{product_id}"


class AlphaVantageSource:
    base_url = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def daily_adjusted_url(self, symbol: str) -> str:
        return f"{self.base_url}?{urlencode({'function': 'TIME_SERIES_DAILY_ADJUSTED', 'symbol': symbol, 'apikey': self.api_key})}"
