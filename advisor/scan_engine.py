from __future__ import annotations

from dataclasses import dataclass

from advisor.indicators import sma
from advisor.models import AssetSnapshot, Candle, MarketRegime
from advisor.regime import classify_crypto_regime, classify_stock_regime, recent_gap_percent, relative_strength


@dataclass(frozen=True)
class DerivedRegimes:
    stock: MarketRegime
    crypto: MarketRegime


def derive_market_regimes(
    *,
    snapshots: list[AssetSnapshot],
    benchmarks: dict[str, list[Candle]],
) -> DerivedRegimes:
    return DerivedRegimes(
        stock=_derive_stock_regime(snapshots, benchmarks),
        crypto=_derive_crypto_regime(snapshots),
    )


def derive_relative_strength(
    snapshot: AssetSnapshot,
    *,
    snapshots: list[AssetSnapshot],
    benchmarks: dict[str, list[Candle]],
) -> float | None:
    if snapshot.asset_type == "stock":
        benchmark = benchmarks.get("QQQ") or benchmarks.get("SPY")
        if not benchmark:
            return None
        return relative_strength(snapshot.candles, benchmark)

    if snapshot.asset_type == "crypto" and snapshot.symbol != "BTC":
        btc = next((item for item in snapshots if item.asset_type == "crypto" and item.symbol == "BTC"), None)
        if btc is None:
            return None
        return relative_strength(snapshot.candles, btc.candles)

    return None


def _derive_stock_regime(snapshots: list[AssetSnapshot], benchmarks: dict[str, list[Candle]]) -> MarketRegime:
    spy = benchmarks.get("SPY", [])
    qqq = benchmarks.get("QQQ", [])
    stock_snapshots = [snapshot for snapshot in snapshots if snapshot.asset_type == "stock"]
    if len(spy) < 200 or len(qqq) < 200 or not stock_snapshots:
        return MarketRegime("neutral", ["insufficient_stock_regime_data"])
    breadth = {
        snapshot.symbol: _above_sma(snapshot.candles, 50)
        for snapshot in stock_snapshots
    }
    max_gap = max((recent_gap_percent(snapshot.candles) for snapshot in stock_snapshots), default=0.0)
    return classify_stock_regime(spy, qqq, breadth, max_gap)


def _derive_crypto_regime(snapshots: list[AssetSnapshot]) -> MarketRegime:
    crypto = {snapshot.symbol: snapshot for snapshot in snapshots if snapshot.asset_type == "crypto"}
    btc = crypto.get("BTC")
    eth = crypto.get("ETH")
    sol = crypto.get("SOL")
    if (
        btc is None
        or eth is None
        or sol is None
        or len(btc.candles) < 200
        or len(eth.candles) < 31
        or len(sol.candles) < 31
    ):
        return MarketRegime("neutral", ["insufficient_crypto_regime_data"])
    funding = max(abs(snapshot.funding_rate or 0.0) for snapshot in crypto.values())
    open_interest_change = max(abs(snapshot.open_interest_change or 0.0) for snapshot in crypto.values())
    return classify_crypto_regime(
        btc_candles=btc.candles,
        eth_btc_relative_strength=relative_strength(eth.candles, btc.candles),
        sol_btc_relative_strength=relative_strength(sol.candles, btc.candles),
        funding_rate=funding,
        open_interest_change=open_interest_change,
    )


def _above_sma(candles: list[Candle], period: int) -> bool:
    closes = [candle.close for candle in candles]
    if len(closes) < period:
        return False
    average = sma(closes, period)[-1]
    return bool(average is not None and closes[-1] > average)
