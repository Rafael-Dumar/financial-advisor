from __future__ import annotations

from typing import Any

from advisor.indicators import atr, ema, rsi, sma
from advisor.models import BacktestOutcome, BacktestStats, Candle


def backtest_r_multiple(
    candles: list[Candle],
    *,
    entry: float,
    stop: float,
    max_days: int,
    cost_r: float = 0.0,
    slippage_r: float = 0.0,
) -> BacktestOutcome:
    if stop >= entry:
        raise ValueError("stop must be below entry")
    if max_days <= 0:
        raise ValueError("max_days must be positive")

    one_r = entry - stop
    target_2r = entry + (2 * one_r)
    target_3r = entry + (3 * one_r)
    friction_r = max(0.0, cost_r) + max(0.0, slippage_r)
    hit_2r = False
    days_to_2r = None

    for days_held, candle in enumerate(candles[1 : max_days + 1], start=1):
        if candle.open <= stop:
            return BacktestOutcome(
                hit_2r=hit_2r,
                hit_3r=False,
                stopped=True,
                expired=False,
                days_held=days_held,
                exit_reason="gap_stop",
                days_to_2r=days_to_2r,
                r_multiple=round(((candle.open - entry) / one_r) - friction_r, 4),
            )
        if candle.low <= stop:
            return BacktestOutcome(
                hit_2r=hit_2r,
                hit_3r=False,
                stopped=True,
                expired=False,
                days_held=days_held,
                exit_reason="stopped",
                days_to_2r=days_to_2r,
                r_multiple=round((2.0 if hit_2r else -1.0) - friction_r, 4),
            )
        if candle.high >= target_3r:
            return BacktestOutcome(
                hit_2r=True,
                hit_3r=True,
                stopped=False,
                expired=False,
                days_held=days_held,
                exit_reason="target_3r",
                days_to_2r=days_to_2r or days_held,
                days_to_3r=days_held,
                r_multiple=round(3.0 - friction_r, 4),
            )
        if not hit_2r and candle.high >= target_2r:
            hit_2r = True
            days_to_2r = days_held

    return BacktestOutcome(
        hit_2r=hit_2r,
        hit_3r=False,
        stopped=False,
        expired=True,
        days_held=min(max_days, max(0, len(candles) - 1)),
        exit_reason="expired",
        days_to_2r=days_to_2r,
        r_multiple=round((2.0 if hit_2r else 0.0) - friction_r, 4),
    )


def summarize_backtest_setups(
    setups: list[dict[str, Any]],
    *,
    cost_r: float = 0.0,
    slippage_r: float = 0.0,
    out_of_sample_fraction: float = 0.0,
) -> BacktestStats:
    if not setups:
        return BacktestStats(sample_size=0, win_rate_2r=None, win_rate_3r=None)
    outcomes = [
        backtest_r_multiple(
            setup["candles"],
            entry=float(setup["entry"]),
            stop=float(setup["stop"]),
            max_days=int(setup.get("max_days", 30)),
            cost_r=cost_r,
            slippage_r=slippage_r,
        )
        for setup in setups
    ]
    sample_size = len(outcomes)
    r_multiples = [outcome.r_multiple for outcome in outcomes]
    wins = [value for value in r_multiples if value > 0]
    losses = [value for value in r_multiples if value < 0]
    all_candles = [
        candle
        for setup in setups
        for candle in setup.get("candles", [])
        if isinstance(candle, Candle)
    ]
    benchmark_returns: dict[str, list[float]] = {}
    for setup in setups:
        benchmark = setup.get("benchmark")
        benchmark_return = setup.get("benchmark_return")
        if benchmark and benchmark_return is not None:
            benchmark_returns.setdefault(str(benchmark), []).append(float(benchmark_return))
    warnings = ["possible_lookahead_bias_check_required", "possible_survivorship_bias"]
    if sample_size < 30:
        warnings.append("sample_size_low")
    if out_of_sample_fraction:
        warnings.append(f"out_of_sample_fraction={out_of_sample_fraction:.2f}")
    return BacktestStats(
        sample_size=sample_size,
        win_rate_2r=sum(1 for outcome in outcomes if outcome.hit_2r) / sample_size,
        win_rate_3r=sum(1 for outcome in outcomes if outcome.hit_3r) / sample_size,
        median_days_to_2r=_median_days(
            [outcome.days_to_2r for outcome in outcomes if outcome.days_to_2r is not None]
        ),
        median_days_to_3r=_median_days(
            [outcome.days_to_3r for outcome in outcomes if outcome.days_to_3r is not None]
        ),
        expected_value_r=sum(r_multiples) / sample_size,
        avg_win_r=(sum(wins) / len(wins)) if wins else None,
        avg_loss_r=(sum(losses) / len(losses)) if losses else None,
        setup_quality=_sample_quality(sample_size),
        max_drawdown_r=_max_drawdown(r_multiples),
        period_start=min((candle.date for candle in all_candles), default=None),
        period_end=max((candle.date for candle in all_candles), default=None),
        benchmark_comparison={
            symbol: sum(values) / len(values)
            for symbol, values in benchmark_returns.items()
            if values
        },
        warnings=warnings,
    )


def backtest_similar_setups(candles: list[Candle], *, max_days: int = 30) -> BacktestStats:
    if len(candles) < 80:
        return BacktestStats(sample_size=0, win_rate_2r=None, win_rate_3r=None)
    current_signature = _setup_signature(candles)
    setups: list[dict[str, Any]] = []
    last_entry_index = len(candles) - max_days - 1
    for index in range(60, max(60, last_entry_index)):
        window = candles[: index + 1]
        if _setup_signature(window) != current_signature:
            continue
        entry = candles[index].close
        stop = _historical_stop(window)
        if stop >= entry:
            continue
        setups.append(
            {
                "candles": candles[index : index + max_days + 1],
                "entry": entry,
                "stop": stop,
                "max_days": max_days,
            }
        )
    return summarize_backtest_setups(setups)


def _setup_signature(candles: list[Candle]) -> tuple[bool, bool, str]:
    closes = [candle.close for candle in candles]
    latest = closes[-1]
    ema9 = _last_number(ema(closes, 9))
    ema21 = _last_number(ema(closes, 21))
    sma50 = _last_number(sma(closes, 50))
    latest_rsi = _last_number(rsi(closes, 14))
    trend_stack = bool(ema9 is not None and ema21 is not None and latest > ema9 > ema21)
    above_sma50 = bool(sma50 is not None and latest > sma50)
    if latest_rsi is None:
        rsi_bucket = "unknown"
    elif latest_rsi < 45:
        rsi_bucket = "weak"
    elif latest_rsi <= 72:
        rsi_bucket = "constructive"
    elif latest_rsi <= 82:
        rsi_bucket = "hot"
    else:
        rsi_bucket = "overextended"
    return trend_stack, above_sma50, rsi_bucket


def _historical_stop(candles: list[Candle]) -> float:
    latest = candles[-1].close
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    closes = [candle.close for candle in candles]
    latest_atr = _last_number(atr(highs, lows, closes, 14)) or latest * 0.04
    return latest - max(latest_atr * 1.5, latest * 0.04)


def _last_number(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return float(value)
    return None


def _median_days(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2)


def _sample_quality(sample_size: int) -> str:
    if sample_size < 30:
        return "low"
    if sample_size < 100:
        return "medium"
    return "high"


def _max_drawdown(values: list[float]) -> float:
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        max_drawdown = min(max_drawdown, running - peak)
    return round(max_drawdown, 4)
