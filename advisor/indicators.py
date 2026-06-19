from __future__ import annotations


def _require_period(period: int) -> None:
    if period <= 0:
        raise ValueError("period must be positive")


def sma(values: list[float], period: int) -> list[float | None]:
    _require_period(period)
    output: list[float | None] = []
    running_sum = 0.0
    for index, value in enumerate(values):
        running_sum += value
        if index >= period:
            running_sum -= values[index - period]
        if index + 1 < period:
            output.append(None)
        else:
            output.append(running_sum / period)
    return output


def ema(values: list[float], period: int) -> list[float | None]:
    _require_period(period)
    if not values:
        return []
    alpha = 2 / (period + 1)
    output: list[float | None] = [float(values[0])]
    previous = float(values[0])
    for value in values[1:]:
        previous = (float(value) * alpha) + (previous * (1 - alpha))
        output.append(previous)
    return output


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    _require_period(period)
    if len(values) < period + 1:
        return [None for _ in values]

    output: list[float | None] = [None for _ in values]
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    output[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        delta = values[index] - values[index - 1]
        gain = max(delta, 0.0)
        loss = abs(min(delta, 0.0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        output[index] = _rsi_from_averages(avg_gain, avg_loss)
    return output


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    _require_period(period)
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, and closes must have the same length")
    if not closes:
        return []

    true_ranges: list[float] = []
    for index, close in enumerate(closes):
        if index == 0:
            true_ranges.append(highs[index] - lows[index])
            continue
        previous_close = closes[index - 1]
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - previous_close),
                abs(lows[index] - previous_close),
            )
        )

    output: list[float | None] = [None for _ in closes]
    if len(true_ranges) < period:
        return output

    average = sum(true_ranges[:period]) / period
    output[period - 1] = average
    for index in range(period, len(true_ranges)):
        average = ((average * (period - 1)) + true_ranges[index]) / period
        output[index] = average
    return output


def percent_returns(values: list[float]) -> list[float | None]:
    if not values:
        return []
    output: list[float | None] = [None]
    for index in range(1, len(values)):
        previous = values[index - 1]
        if previous == 0:
            output.append(None)
        else:
            output.append((values[index] - previous) / previous)
    return output
