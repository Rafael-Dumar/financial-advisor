from __future__ import annotations

from collections import Counter
from itertools import combinations

from advisor.indicators import percent_returns
from advisor.models import Candle, LeveragePolicy, RiskPlan


def calculate_trade_plan(
    *,
    entry: float,
    stop: float,
    account_capital: float,
    risk_fraction: float,
    atr_value: float | None,
    average_volume: float | None,
    allow_fractional: bool = False,
) -> RiskPlan:
    if account_capital <= 0:
        raise ValueError("account_capital must be positive")
    if not 0 < risk_fraction <= 0.01:
        raise ValueError("risk_fraction must be between 0 and 0.01")
    if stop >= entry:
        raise ValueError("stop must be below entry for long setups")

    per_unit_risk = entry - stop
    configured_risk_amount = round(account_capital * risk_fraction, 2)
    if allow_fractional:
        risk_based_units = configured_risk_amount / per_unit_risk
        capital_based_units = account_capital / entry
    else:
        risk_based_units = int(configured_risk_amount // per_unit_risk)
        capital_based_units = int(account_capital // entry)
    max_position_units = min(risk_based_units, capital_based_units)
    max_position_value = round(max_position_units * entry, 2)
    risk_amount = round(max_position_units * per_unit_risk, 2)
    effective_risk_fraction = round(risk_amount / account_capital, 6)
    target_2r = round(entry + (2 * per_unit_risk), 2)
    target_3r = round(entry + (3 * per_unit_risk), 2)
    alerts = []

    if risk_based_units > capital_based_units:
        alerts.append("position_capped_by_account_capital")
    if atr_value is not None and atr_value / entry > 0.08:
        alerts.append("high_volatility")
    if average_volume is not None and average_volume < 100_000:
        alerts.append("low_liquidity")
    if max_position_units <= 0:
        alerts.append("position_too_small_for_risk")

    return RiskPlan(
        entry=round(entry, 2),
        stop=round(stop, 2),
        target_2r=target_2r,
        target_3r=target_3r,
        per_unit_risk=round(per_unit_risk, 2),
        risk_amount=risk_amount,
        risk_fraction=effective_risk_fraction,
        max_position_units=round(max_position_units, 8) if allow_fractional else max_position_units,
        max_position_value=max_position_value,
        risk_reward_2r="2.00:1",
        alerts=alerts,
        position_size_display=_format_position_size(max_position_units, allow_fractional=allow_fractional),
    )


def evaluate_leverage_policy(
    *,
    decision_confidence_score: int,
    missing_data_severity: str,
) -> LeveragePolicy:
    reasons: list[str] = []
    if decision_confidence_score < 65:
        reasons.append("low_decision_confidence")
    if missing_data_severity in {"high", "blocking", "critical"}:
        reasons.append("blocking_missing_data")
    return LeveragePolicy(allowed=not reasons, reasons=reasons)


def rate_sample_quality(sample_size: int) -> str:
    if sample_size < 30:
        return "low"
    if sample_size < 100:
        return "medium"
    return "high"


def _format_position_size(units: float, *, allow_fractional: bool) -> str:
    if not allow_fractional:
        return str(int(units))
    if units >= 100:
        return f"{units:.2f}"
    if units >= 1:
        return f"{units:.4f}".rstrip("0").rstrip(".")
    return f"{units:.4f}"


def detect_theme_concentration(themes_by_symbol: dict[str, str], max_same_theme: int = 3) -> list[str]:
    counts = Counter(themes_by_symbol.values())
    return [
        f"theme_concentration:{theme}"
        for theme, count in sorted(counts.items())
        if theme and count > max_same_theme
    ]


def detect_return_correlation(
    candles_by_symbol: dict[str, list[Candle]],
    *,
    threshold: float = 0.85,
    minimum_observations: int = 30,
    lookback: int = 60,
) -> list[str]:
    alerts = []
    for first_symbol, second_symbol in combinations(sorted(candles_by_symbol), 2):
        first_returns, second_returns = _aligned_returns(
            candles_by_symbol[first_symbol],
            candles_by_symbol[second_symbol],
            lookback=lookback,
        )
        if len(first_returns) < minimum_observations:
            continue
        correlation = _pearson_correlation(first_returns, second_returns)
        if correlation is not None and correlation >= threshold:
            alerts.append(f"return_correlation:{first_symbol}:{second_symbol}:{correlation:.2f}")
    return alerts


def _aligned_returns(
    first: list[Candle],
    second: list[Candle],
    *,
    lookback: int,
) -> tuple[list[float], list[float]]:
    first_by_date = {candle.date: candle.close for candle in first}
    second_by_date = {candle.date: candle.close for candle in second}
    common_dates = sorted(set(first_by_date) & set(second_by_date))[-(lookback + 1) :]
    first_values = [first_by_date[date] for date in common_dates]
    second_values = [second_by_date[date] for date in common_dates]
    first_returns = percent_returns(first_values)
    second_returns = percent_returns(second_values)
    paired = [
        (first_value, second_value)
        for first_value, second_value in zip(first_returns, second_returns)
        if first_value is not None and second_value is not None
    ]
    return [pair[0] for pair in paired], [pair[1] for pair in paired]


def _pearson_correlation(first: list[float], second: list[float]) -> float | None:
    if len(first) != len(second) or not first:
        return None
    first_mean = sum(first) / len(first)
    second_mean = sum(second) / len(second)
    first_deviations = [value - first_mean for value in first]
    second_deviations = [value - second_mean for value in second]
    first_variance = sum(value**2 for value in first_deviations)
    second_variance = sum(value**2 for value in second_deviations)
    if first_variance == 0 or second_variance == 0:
        return None
    covariance = sum(
        first_value * second_value
        for first_value, second_value in zip(first_deviations, second_deviations)
    )
    return covariance / ((first_variance * second_variance) ** 0.5)
