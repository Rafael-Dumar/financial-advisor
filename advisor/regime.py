from __future__ import annotations

from advisor.indicators import percent_returns, sma
from advisor.models import Candle, MarketRegime

HIGH_FUNDING_RATE_8H = 0.001
BALANCED_FUNDING_RATE_8H = 0.0003


def classify_stock_regime(
    spy: list[Candle],
    qqq: list[Candle],
    watchlist_above_sma50: dict[str, bool],
    recent_gap_percent: float,
) -> MarketRegime:
    reasons: list[str] = []
    score = 0

    if _is_above_sma50_and_sma200(spy):
        score += 2
        reasons.append("SPY_above_sma50_sma200")
    else:
        score -= 2
        reasons.append("SPY_below_key_averages")

    if _is_above_sma50_and_sma200(qqq):
        score += 2
        reasons.append("QQQ_above_sma50_sma200")
    else:
        score -= 2
        reasons.append("QQQ_below_key_averages")

    if watchlist_above_sma50:
        breadth = sum(1 for is_above in watchlist_above_sma50.values() if is_above) / len(watchlist_above_sma50)
        if breadth >= 0.60:
            score += 1
            reasons.append("watchlist_breadth_positive")
        elif breadth <= 0.40:
            score -= 1
            reasons.append("watchlist_breadth_weak")

    if recent_gap_percent > 0.06:
        score -= 1
        reasons.append("recent_gaps_elevated")

    return MarketRegime(_label_from_score(score), reasons)


def classify_crypto_regime(
    *,
    btc_candles: list[Candle],
    eth_btc_relative_strength: float,
    sol_btc_relative_strength: float,
    funding_rate: float,
    open_interest_change: float,
) -> MarketRegime:
    reasons: list[str] = []
    score = 0

    if _is_above_sma50_and_sma200(btc_candles):
        score += 2
        reasons.append("BTC_above_sma50_sma200")
    else:
        score -= 2
        reasons.append("BTC_below_key_averages")

    if eth_btc_relative_strength > 0:
        score += 1
        reasons.append("ETHBTC_relative_strength_positive")
    else:
        score -= 1
        reasons.append("ETHBTC_relative_strength_weak")

    if sol_btc_relative_strength > 0:
        score += 1
        reasons.append("SOLBTC_relative_strength_positive")
    else:
        score -= 1
        reasons.append("SOLBTC_relative_strength_weak")

    if abs(funding_rate) > HIGH_FUNDING_RATE_8H and open_interest_change > 0.25:
        score -= 2
        reasons.append("leverage_hot")
    elif abs(funding_rate) < BALANCED_FUNDING_RATE_8H:
        score += 1
        reasons.append("funding_balanced")

    return MarketRegime(_label_from_score(score), reasons)


def _is_above_sma50_and_sma200(candles: list[Candle]) -> bool:
    closes = [candle.close for candle in candles]
    if len(closes) < 200:
        return False
    sma50 = sma(closes, 50)[-1]
    sma200 = sma(closes, 200)[-1]
    return bool(sma50 is not None and sma200 is not None and closes[-1] > sma50 and closes[-1] > sma200)


def recent_gap_percent(candles: list[Candle], lookback: int = 10) -> float:
    if len(candles) < 2:
        return 0.0
    gaps = []
    for index in range(max(1, len(candles) - lookback), len(candles)):
        previous_close = candles[index - 1].close
        if previous_close:
            gaps.append(abs(candles[index].open - previous_close) / previous_close)
    return max(gaps) if gaps else 0.0


def relative_strength(asset_candles: list[Candle], benchmark_candles: list[Candle], lookback: int = 30) -> float:
    asset_returns = _window_return(asset_candles, lookback)
    benchmark_returns = _window_return(benchmark_candles, lookback)
    return asset_returns - benchmark_returns


def volatility(candles: list[Candle], lookback: int = 20) -> float:
    closes = [candle.close for candle in candles]
    returns = [value for value in percent_returns(closes)[-lookback:] if value is not None]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return variance ** 0.5


def _window_return(candles: list[Candle], lookback: int) -> float:
    if len(candles) <= lookback or candles[-lookback].close == 0:
        return 0.0
    return (candles[-1].close - candles[-lookback].close) / candles[-lookback].close


def _label_from_score(score: int) -> str:
    if score >= 3:
        return "risk_on"
    if score <= -3:
        return "risk_off"
    return "neutral"
