from __future__ import annotations

from datetime import datetime, timezone

from advisor.indicators import atr, ema, rsi, sma
from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, ScoredAsset
from advisor.regime import HIGH_FUNDING_RATE_8H, recent_gap_percent
from advisor.risk import calculate_trade_plan, rate_sample_quality


def score_asset(
    snapshot: AssetSnapshot,
    *,
    stock_regime_label: str,
    crypto_regime_label: str,
    account_capital: float = 50_000,
    risk_fraction: float = 0.005,
    relative_strength_percent: float | None = None,
    minimum_market_cap: float | None = None,
) -> ScoredAsset:
    closes = [candle.close for candle in snapshot.candles]
    highs = [candle.high for candle in snapshot.candles]
    lows = [candle.low for candle in snapshot.candles]
    latest_close = closes[-1]
    latest_atr = _last_number(atr(highs, lows, closes, 14)) or (latest_close * 0.04)
    stop = latest_close - max(latest_atr * 1.5, latest_close * 0.04)
    risk_plan = calculate_trade_plan(
        entry=latest_close,
        stop=stop,
        account_capital=account_capital,
        risk_fraction=risk_fraction,
        atr_value=latest_atr,
        average_volume=snapshot.fundamentals.average_volume,
        allow_fractional=snapshot.asset_type == "crypto",
    )

    alerts = list(risk_plan.alerts)
    limitations = list(snapshot.missing_data)
    _apply_provider_context(snapshot, alerts, limitations)
    recent_gap = recent_gap_percent(snapshot.candles)
    if recent_gap > 0.06:
        alerts.append("recent_gap_risk")
    investment_score = _investment_quality_score(snapshot, alerts, limitations)
    if (
        minimum_market_cap is not None
        and snapshot.fundamentals.market_cap is not None
        and snapshot.fundamentals.market_cap < minimum_market_cap
    ):
        alerts.append("below_minimum_market_cap")
        investment_score = max(0, investment_score - 15)
    swing_score = _swing_trade_score(
        snapshot,
        stock_regime_label,
        crypto_regime_label,
        alerts,
        limitations,
        relative_strength_percent=relative_strength_percent,
    )

    if snapshot.event:
        if snapshot.asset_type == "stock" and snapshot.event.days_to_earnings is None:
            limitations.append("earnings_data_missing")
        if snapshot.event.days_to_earnings is not None and snapshot.event.days_to_earnings <= 10:
            alerts.append("earnings_near")
            swing_score = max(0, swing_score - 18)
        if snapshot.event.days_to_earnings is not None and snapshot.event.days_to_earnings <= 5:
            alerts.append("earnings_imminent")
            swing_score = max(0, swing_score - 10)
        if (
            snapshot.event.post_earnings_gap_percent is not None
            and abs(snapshot.event.post_earnings_gap_percent) >= 0.08
        ):
            alerts.append("post_earnings_gap")
            swing_score = max(0, swing_score - 8)
        if snapshot.event.guidance_recent is True:
            alerts.append("recent_guidance")

    _apply_news_context(snapshot, alerts, limitations)

    if snapshot.asset_type == "crypto":
        if snapshot.cvd_proxy is not None:
            limitations.append("cvd_proxy_uses_taker_buy_sell_volume")
        if snapshot.liquidation_imbalance is not None and abs(snapshot.liquidation_imbalance) > 0.70:
            alerts.append("liquidation_pressure")
        if snapshot.funding_rate is not None and abs(snapshot.funding_rate) > HIGH_FUNDING_RATE_8H:
            alerts.append("leverage_risk_funding")
            swing_score = max(0, swing_score - 6)
        if snapshot.open_interest_change is not None and snapshot.open_interest_change > 0.25:
            alerts.append("leverage_risk_open_interest")
            swing_score = max(0, swing_score - 6)

    thesis = _build_thesis(snapshot, investment_score, swing_score)
    alternative_entry = round(latest_close * 0.97, 2)
    return ScoredAsset(
        snapshot=snapshot,
        investment_quality_score=round(investment_score, 2),
        swing_trade_score=round(swing_score, 2),
        risk_plan=risk_plan,
        alerts=sorted(set(alerts)),
        limitations=sorted(set(limitations)),
        thesis=thesis,
        metrics_summary=_metrics_summary(
            snapshot,
            relative_strength_percent=relative_strength_percent,
            recent_gap=recent_gap,
        ),
        ideal_entry=round(latest_close, 2),
        alternative_entry=alternative_entry,
        hold_suggestion="1-8 semanas",
    )


def classify_asset(scored: ScoredAsset, backtest_stats: BacktestStats | None) -> AssetDecision:
    alerts = list(scored.alerts)
    limitations = list(scored.limitations)
    sample_quality = (
        backtest_stats.setup_quality
        if backtest_stats and backtest_stats.setup_quality
        else rate_sample_quality(backtest_stats.sample_size)
        if backtest_stats
        else None
    )
    has_low_sample = backtest_stats is None or backtest_stats.sample_size < 30
    freshness = _freshness_context(scored.snapshot, limitations)
    if freshness["is_stale"]:
        limitations.append("stale_price_data")
        alerts.append("stale_price_data")
    _apply_uncollected_context_limits(scored, backtest_stats, limitations)
    if _has_blocking_data_gap(limitations):
        decision = "blocked"
    else:
        decision = _base_decision(scored)

    hard_gates = {
        "low_liquidity",
        "event_risk",
        "earnings_imminent",
        "earnings_near",
        "market_risk_off",
        "market_not_risk_on",
        "position_too_small_for_risk",
        "recent_gap_risk",
        "small_market_cap",
    }

    if _has_blocking_data_gap(limitations):
        max_decision = "blocked"
    elif _has_confidence_limiting_data_gap(limitations):
        max_decision = "watch_buy"
        limitations.append("data_incomplete_confidence_limited")
    elif any(alert in hard_gates for alert in alerts):
        max_decision = "wait" if "earnings_imminent" in alerts else "watch_buy"
    elif has_low_sample:
        max_decision = "watch_buy"
        limitations.append("backtest_sample_low")
    else:
        max_decision = "tradeable"

    if _missing_data_severity(limitations) == "high":
        alerts.append("high_severity_data_not_watchlist")
        max_decision = _weaker_cap(max_decision, "technical_unvalidated")

    if "stale_price_data" in limitations:
        max_decision = _weaker_cap(max_decision, "wait")

    if not has_low_sample and backtest_stats and backtest_stats.win_rate_2r is not None:
        win_rate = backtest_stats.win_rate_2r
        ev = backtest_stats.expected_value_r
        if win_rate < 0.35:
            alerts.append("weak_setup_win_rate")
            max_decision = _weaker_cap(max_decision, "avoid" if scored.investment_quality_score < 35 else "technical_unvalidated")
        elif win_rate < 0.40:
            alerts.append("weak_setup_win_rate")
            max_decision = _weaker_cap(max_decision, "wait")
        elif win_rate < 0.45 and ev is not None and ev <= 0:
            alerts.append("weak_or_negative_expected_value")
            max_decision = _weaker_cap(max_decision, "wait")
        elif ev is not None and ev <= 0:
            alerts.append("weak_or_negative_expected_value")
            max_decision = _weaker_cap(max_decision, "wait")
        if ev is not None and ev < 0 and _missing_data_severity(limitations) in {"high", "critical"}:
            alerts.append("negative_ev_with_high_data_severity")
            max_decision = _weaker_cap(max_decision, "technical_unvalidated")

    if _is_intc_like_case(scored, alerts, backtest_stats):
        max_decision = _weaker_cap(max_decision, "technical_unvalidated")

    data_quality = _data_quality(limitations)
    missing_severity = _missing_data_severity(limitations)
    data_quality_score = _data_quality_score(data_quality, missing_severity, limitations)
    decision_confidence_score = _decision_confidence_score(
        scored,
        backtest_stats,
        data_quality_score=data_quality_score,
        limitations=limitations,
        alerts=alerts,
    )
    if decision_confidence_score < 65:
        max_decision = _weaker_cap(max_decision, "watch_buy")
    if _is_technical_unvalidated(scored, limitations, backtest_stats, data_quality, missing_severity, sample_quality):
        max_decision = _weaker_cap(max_decision, "technical_unvalidated")

    if "below_minimum_market_cap" in alerts:
        decision = "avoid"
    else:
        decision = _apply_cap(decision, max_decision)
    if decision == "watch_buy" and "earnings_imminent" in alerts:
        decision = "wait"

    thesis = scored.thesis
    if _has_fundamental_validation_gap(limitations):
        thesis = "Setup tecnico detectado, mas dados fundamentais insuficientes impedem validacao."
    elif "earnings_data_missing" in limitations:
        thesis = "Setup tecnico detectado, mas earnings/eventos nao verificados limitam validacao."
    return AssetDecision(
        symbol=scored.snapshot.symbol,
        asset_type=scored.snapshot.asset_type,
        decision=decision,
        investment_quality_score=scored.investment_quality_score,
        swing_trade_score=scored.swing_trade_score,
        risk_plan=scored.risk_plan,
        alerts=sorted(set(alerts)),
        limitations=sorted(set(limitations)),
        thesis=thesis,
        metrics_summary=scored.metrics_summary,
        ideal_entry=scored.ideal_entry,
        alternative_entry=scored.alternative_entry,
        hold_suggestion=_hold_suggestion(scored, backtest_stats),
        backtest_stats=backtest_stats,
        sample_quality=sample_quality,
        reason_codes=sorted(set([*alerts, *limitations])),
        data_quality=data_quality,
        missing_data_severity=missing_severity,
        news_summary=_news_summary(scored.snapshot.news_events),
        data_source=scored.snapshot.data_source,
        data_timestamp=scored.snapshot.data_timestamp,
        cache_age_seconds=scored.snapshot.cache_age_seconds,
        bucket=_bucket_for_decision(decision),
        market_session=str(freshness["market_session"]),
        last_price_timestamp=str(freshness["last_price_timestamp"]) if freshness["last_price_timestamp"] else None,
        provider=scored.snapshot.data_source,
        is_stale=bool(freshness["is_stale"]),
        stale_reason=str(freshness["stale_reason"]) if freshness["stale_reason"] else None,
        event_check_status=_event_check_status(scored.snapshot, limitations),
        news_status="collected" if scored.snapshot.news_events else "not_collected",
        macro_regime="neutral",
        macro_status="not_collected",
        thesis_status=_thesis_status(scored, backtest_stats, limitations),
        data_quality_score=data_quality_score,
        decision_confidence_score=decision_confidence_score,
        relative_strength_vs_spy=None,
        relative_strength_vs_qqq=None,
        relative_strength_vs_sector=None,
        sector_benchmark=_sector_benchmark(scored.snapshot.theme),
        short_setup_score=_short_setup_score(scored),
        squeeze_risk="unknown",
        gap_risk="high" if "recent_gap_risk" in alerts else "unknown",
        borrow_data_available=False,
        short_status="watch_only" if _short_setup_score(scored) >= 70 else "not_evaluated",
    )


def _base_decision(scored: ScoredAsset) -> str:
    if scored.swing_trade_score < 45 or scored.investment_quality_score < 25:
        return "avoid"
    if scored.swing_trade_score < 60:
        return "wait"
    if scored.swing_trade_score >= 75 and scored.investment_quality_score >= 70:
        return "tradeable"
    if scored.swing_trade_score >= 70 and scored.investment_quality_score < 50:
        return "technical_unvalidated"
    return "watch_buy"


def _apply_cap(decision: str, cap: str) -> str:
    return decision if _decision_rank(decision) >= _decision_rank(cap) else cap


def _weaker_cap(current: str, new_cap: str) -> str:
    return current if _decision_rank(current) >= _decision_rank(new_cap) else new_cap


def _decision_rank(decision: str) -> int:
    order = {
        "tradeable": 0,
        "watch_buy": 1,
        "watch_only": 2,
        "technical_unvalidated": 3,
        "speculative_watch": 3,
        "wait": 4,
        "avoid": 5,
        "blocked": 6,
        "no_trade_day": 7,
    }
    return order.get(decision, 5)


def _is_intc_like_case(scored: ScoredAsset, alerts: list[str], backtest_stats: BacktestStats | None) -> bool:
    if scored.investment_quality_score >= 45:
        return False
    if "recent_gap_risk" not in alerts:
        return False
    if not {"negative_or_invalid_pe", "negative_or_invalid_peg"} & set(alerts):
        return False
    return bool(backtest_stats and backtest_stats.win_rate_2r is not None and backtest_stats.win_rate_2r < 0.45)


def _hold_suggestion(scored: ScoredAsset, backtest_stats: BacktestStats | None) -> str:
    if (
        backtest_stats is not None
        and backtest_stats.sample_size >= 30
        and backtest_stats.median_days_to_2r is not None
    ):
        return f"{backtest_stats.median_days_to_2r} dias medianos ate +2R; max {scored.hold_suggestion}"
    return scored.hold_suggestion


def _has_confidence_limiting_data_gap(limitations: list[str]) -> bool:
    non_blocking = {"cvd_proxy_uses_taker_buy_sell_volume", "fmp_price_light_fallback"}
    explicitly_limiting = {"earnings_data_missing", "news_rumor_not_confirmed", "news_confidence_low"}
    for limitation in limitations:
        if limitation in non_blocking:
            continue
        if limitation in explicitly_limiting:
            return True
        if (
            limitation.startswith("missing_")
            or limitation.startswith("insufficient_")
            or limitation.endswith("_unavailable")
            or limitation.endswith("_not_live")
            or limitation.endswith("_demo")
        ):
            return True
    return False


def _has_blocking_data_gap(limitations: list[str]) -> bool:
    blocking = {
        "insufficient_price_history",
        "price_history_unavailable",
        "fmp_price_unavailable",
    }
    return any(limitation in blocking for limitation in limitations)


def _apply_news_context(snapshot: AssetSnapshot, alerts: list[str], limitations: list[str]) -> None:
    for event in snapshot.news_events:
        status = str(event.get("confirmed_status", "")).lower()
        already_priced = str(event.get("already_priced", "")).lower()
        market_effect = str(event.get("market_effect", "")).lower()
        confidence = str(event.get("news_confidence", "")).lower()
        if status == "rumor":
            alerts.append("news_rumor_confidence_limited")
            limitations.append("news_rumor_not_confirmed")
        if already_priced in {"yes", "unclear"}:
            alerts.append("possible_priced_in")
        if market_effect == "risk_off":
            alerts.append("news_risk_off")
        if confidence == "low":
            limitations.append("news_confidence_low")


def _data_quality(limitations: list[str]) -> str:
    if _has_blocking_data_gap(limitations):
        return "blocked"
    if _has_confidence_limiting_data_gap(limitations):
        return "limited"
    return "ok"


def _missing_data_severity(limitations: list[str]) -> str:
    if _has_blocking_data_gap(limitations):
        return "critical"
    if any(limitation == "earnings_data_missing" or limitation.endswith("_unavailable") for limitation in limitations):
        return "high"
    if _has_confidence_limiting_data_gap(limitations):
        return "medium"
    return "low"


def _apply_provider_context(snapshot: AssetSnapshot, alerts: list[str], limitations: list[str]) -> None:
    if snapshot.symbol in {"TSM", "ASML"}:
        alerts.append("adr_or_foreign_listing")
        alerts.append("provider_market_cap_mismatch_possible")
    if snapshot.asset_type == "stock" and snapshot.data_source not in {"fmp", "fmp_light", "unknown"}:
        alerts.append("source_mismatch_possible")
        limitations.append("mixed_provider_data")
    if snapshot.data_source in {"yahoo", "stooq", "alphavantage"}:
        alerts.append("source_mismatch_possible")


def _apply_uncollected_context_limits(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    limitations: list[str],
) -> None:
    if not scored.snapshot.news_events:
        limitations.append("news_not_collected_confidence_limited")
    limitations.append("macro_not_collected_confidence_limited")
    if _sector_benchmark(scored.snapshot.theme) and scored.snapshot.asset_type == "stock":
        limitations.append("sector_relative_strength_not_collected")
    if (
        backtest_stats is not None
        and backtest_stats.expected_value_r is not None
        and (backtest_stats.avg_win_r is None or backtest_stats.avg_loss_r is None)
    ):
        limitations.append("ev_components_missing")


def _freshness_context(snapshot: AssetSnapshot, limitations: list[str]) -> dict[str, object]:
    last_price_timestamp = snapshot.candles[-1].date if snapshot.candles else snapshot.data_timestamp
    is_stale = "stale_price_data" in limitations
    stale_reason = "price_cache_or_last_candle_stale" if is_stale else None
    if snapshot.cache_age_seconds is not None and snapshot.cache_age_seconds > 60 * 60 * 24:
        is_stale = True
        stale_reason = "cache_age_exceeds_24h"
    return {
        "market_session": _market_session(),
        "last_price_timestamp": last_price_timestamp,
        "is_stale": is_stale,
        "stale_reason": stale_reason,
    }


def _market_session(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    # Fixed UTC windows keep this dependency-free; report still labels unknown if clocks drift.
    weekday = now.weekday()
    minutes = (now.hour * 60) + now.minute
    if weekday >= 5:
        return "closed"
    if 13 * 60 + 30 <= minutes < 20 * 60:
        return "regular"
    if 8 * 60 <= minutes < 13 * 60 + 30:
        return "pre_market"
    if 20 * 60 <= minutes < 24 * 60:
        return "after_hours"
    return "closed"


def _has_fundamental_validation_gap(limitations: list[str]) -> bool:
    fundamental_gaps = {
        "fundamentals_unavailable",
        "missing_revenue_growth",
        "missing_eps_growth",
        "missing_peg",
        "missing_pe_history",
        "missing_free_cash_flow",
        "missing_margin_trend",
    }
    return bool(fundamental_gaps & set(limitations))


def _is_technical_unvalidated(
    scored: ScoredAsset,
    limitations: list[str],
    backtest_stats: BacktestStats | None,
    data_quality: str,
    missing_severity: str,
    sample_quality: str | None,
) -> bool:
    ev = backtest_stats.expected_value_r if backtest_stats else None
    return (
        scored.swing_trade_score >= 70
        and (
            missing_severity in {"high", "critical"}
            or data_quality == "limited"
            or _has_fundamental_validation_gap(limitations)
            or (ev is not None and ev <= 0)
            or sample_quality == "low"
        )
    )


def _data_quality_score(data_quality: str, missing_severity: str, limitations: list[str]) -> int:
    score = 95
    if data_quality == "blocked":
        return 0
    if data_quality == "limited":
        score = min(score, 65)
    if missing_severity == "high":
        score = min(score, 55)
    if missing_severity == "critical":
        score = min(score, 20)
    if "earnings_data_missing" in limitations:
        score = min(score, 60)
    if "stale_price_data" in limitations:
        score = min(score, 50)
    return max(0, score)


def _decision_confidence_score(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    data_quality_score: int,
    limitations: list[str],
    alerts: list[str],
) -> int:
    score = min(data_quality_score, int(round((scored.investment_quality_score + scored.swing_trade_score) / 2)))
    sample_size = backtest_stats.sample_size if backtest_stats else 0
    if sample_size < 30:
        score = min(score, 45)
    elif sample_size < 100:
        score = min(score, 70)
    if backtest_stats and backtest_stats.expected_value_r is not None and backtest_stats.expected_value_r <= 0:
        score = min(score, 50)
    if "earnings_data_missing" in limitations:
        score = min(score, 55)
    if "market_not_risk_on" in alerts:
        score = min(score, 75)
    if "market_risk_off" in alerts:
        score = min(score, 45)
    if "stale_price_data" in limitations:
        score = min(score, 45)
    if "news_rumor_not_confirmed" in limitations or "news_confidence_low" in limitations:
        score = min(score, 55)
    if "news_not_collected_confidence_limited" in limitations:
        score = min(score, 80)
    if "macro_not_collected_confidence_limited" in limitations:
        score = min(score, 75)
    if "sector_relative_strength_not_collected" in limitations:
        score = min(score, 70)
    if "ev_components_missing" in limitations:
        score = min(score, 60)
    return max(0, score)


def _event_check_status(snapshot: AssetSnapshot, limitations: list[str]) -> str:
    if snapshot.asset_type == "crypto":
        return "not_applicable"
    if "earnings_unavailable" in limitations:
        return "source_unavailable"
    if "earnings_data_missing" in limitations:
        return "not_collected"
    if snapshot.event is not None and snapshot.event.days_to_earnings is not None:
        return "verified"
    return "not_collected"


def _bucket_for_decision(decision: str) -> str:
    if decision in {"tradeable", "watch_buy", "watch_only", "technical_unvalidated", "wait", "blocked", "avoid"}:
        return decision
    if decision == "speculative_watch":
        return "technical_unvalidated"
    return "avoid"


def _thesis_status(scored: ScoredAsset, backtest_stats: BacktestStats | None, limitations: list[str]) -> str:
    if _has_fundamental_validation_gap(limitations):
        return "unknown"
    if scored.investment_quality_score >= 70 and scored.swing_trade_score >= 70:
        return "strengthening"
    if backtest_stats and backtest_stats.expected_value_r is not None and backtest_stats.expected_value_r < 0:
        return "weakening"
    if scored.investment_quality_score >= 55:
        return "stable"
    return "unknown"


def _sector_benchmark(theme: str) -> str | None:
    if theme == "semiconductors":
        return "SMH"
    if theme in {"software", "software_ai"}:
        return "IGV"
    if theme == "cloud_ecommerce":
        return "QQQ"
    if theme == "healthcare":
        return "XLV"
    return None


def _short_setup_score(scored: ScoredAsset) -> float:
    if scored.swing_trade_score <= 35:
        return round(100 - scored.swing_trade_score, 2)
    return 0.0


def _news_summary(news_events: list[dict[str, object]]) -> str | None:
    if not news_events:
        return None
    parts = []
    for event in news_events:
        parts.append(
            (
                f"{event.get('news_event_type', 'unknown')} "
                f"status={event.get('confirmed_status', 'unknown')} "
                f"effect={event.get('market_effect', 'neutral')} "
                f"priced={event.get('already_priced', 'unclear')} "
                f"confidence={event.get('news_confidence', 'unknown')}"
            )
        )
    return "; ".join(parts)


def _investment_quality_score(snapshot: AssetSnapshot, alerts: list[str], limitations: list[str]) -> float:
    fundamentals = snapshot.fundamentals
    score = 45.0
    score_cap = 100.0

    if fundamentals.market_cap is None:
        limitations.append("missing_market_cap")
        score -= 8
    elif fundamentals.market_cap >= 10_000_000_000:
        score += 12
    elif fundamentals.market_cap < 1_000_000_000:
        score -= 12
        alerts.append("small_market_cap")

    if fundamentals.average_volume is None:
        limitations.append("missing_average_volume")
        score -= 8
    elif fundamentals.average_volume >= 1_000_000:
        score += 8
    elif fundamentals.average_volume < 100_000:
        score -= 20
        alerts.append("low_liquidity")

    if snapshot.asset_type == "crypto":
        return _clamp(score + 10)

    if fundamentals.revenue_growth is None:
        limitations.append("missing_revenue_growth")
    elif fundamentals.revenue_growth >= 0.15:
        score += 12
    elif fundamentals.revenue_growth <= 0:
        score -= 12
    elif fundamentals.revenue_growth < 0.05:
        score -= 8

    if fundamentals.eps_growth is None:
        limitations.append("missing_eps_growth")
    elif fundamentals.eps_growth >= 0.10:
        score += 8
    elif fundamentals.eps_growth < 0:
        score -= 12

    if fundamentals.margin_trend is None:
        limitations.append("missing_margin_trend")
    elif fundamentals.margin_trend > 0:
        score += 6
    else:
        score -= 6

    if fundamentals.free_cash_flow_positive is True:
        score += 6
    elif fundamentals.free_cash_flow_positive is False:
        score -= 10
    else:
        limitations.append("missing_free_cash_flow")

    if fundamentals.pe is not None and fundamentals.pe <= 0:
        score -= 15
        alerts.append("negative_or_invalid_pe")
        score_cap = min(score_cap, 50)
    elif (
        fundamentals.pe is not None
        and fundamentals.historical_pe is not None
        and fundamentals.historical_pe > 0
    ):
        if fundamentals.pe <= fundamentals.historical_pe * 1.10:
            score += 6
        elif fundamentals.pe >= fundamentals.historical_pe * 2.0:
            score -= 20
            alerts.append("valuation_extreme")
            score_cap = min(score_cap, 70)
        elif fundamentals.pe >= fundamentals.historical_pe * 1.5:
            score -= 12
            alerts.append("valuation_stretched")
            score_cap = min(score_cap, 82)
        else:
            score -= 4
    else:
        limitations.append("missing_pe_history")

    if fundamentals.peg is not None:
        if fundamentals.peg <= 0:
            score -= 8
            alerts.append("negative_or_invalid_peg")
            score_cap = min(score_cap, 55)
        elif fundamentals.peg <= 2:
            score += 5
        elif fundamentals.peg > 5:
            score -= 14
            alerts.append("valuation_extreme")
            score_cap = min(score_cap, 70)
        elif fundamentals.peg > 3:
            score -= 8
            alerts.append("peg_stretched")
            score_cap = min(score_cap, 82)
    else:
        limitations.append("missing_peg")

    return min(_clamp(score), score_cap)


def _swing_trade_score(
    snapshot: AssetSnapshot,
    stock_regime_label: str,
    crypto_regime_label: str,
    alerts: list[str],
    limitations: list[str],
    *,
    relative_strength_percent: float | None,
) -> float:
    closes = [candle.close for candle in snapshot.candles]
    score = 45.0
    ema9 = _last_number(ema(closes, 9))
    ema21 = _last_number(ema(closes, 21))
    sma50 = _last_number(sma(closes, 50))
    sma200 = _last_number(sma(closes, 200))
    if sma200 is None:
        limitations.append("insufficient_sma200_history")
    latest_rsi = _last_number(rsi(closes, 14))
    latest = closes[-1]

    if ema9 is not None and ema21 is not None and latest > ema9 > ema21:
        score += 16
    elif ema9 is not None and ema21 is not None and latest < ema21:
        score -= 12

    if sma50 is not None and sma200 is not None and latest > sma50 >= sma200:
        score += 14
    elif sma50 is not None and latest < sma50:
        score -= 12

    if latest_rsi is None:
        limitations.append("missing_rsi")
    elif 45 <= latest_rsi <= 72:
        score += 8
    elif latest_rsi > 80:
        score -= 8
        alerts.append("overextended_rsi")

    regime_label = crypto_regime_label if snapshot.asset_type == "crypto" else stock_regime_label
    if regime_label == "risk_on":
        score += 10
    elif regime_label == "risk_off":
        score -= 18
        alerts.append("market_risk_off")
    else:
        alerts.append("market_not_risk_on")

    if relative_strength_percent is not None:
        if relative_strength_percent >= 0.03:
            score += 6
        elif relative_strength_percent <= -0.03:
            score -= 10
            alerts.append("relative_strength_weak")

    if snapshot.fundamentals.average_volume is not None and snapshot.fundamentals.average_volume < 100_000:
        alerts.append("low_liquidity")
        score -= 20

    return _clamp(score)


def _build_thesis(snapshot: AssetSnapshot, investment_score: float, swing_score: float) -> str:
    if _has_fundamental_validation_gap(snapshot.missing_data):
        return "Setup tecnico detectado, mas dados fundamentais insuficientes impedem validacao."
    if investment_score >= 70 and swing_score >= 70:
        return "Ativo com qualidade e setup alinhados, desde que o risco planejado seja respeitado."
    if investment_score >= 70:
        return "Ativo de boa qualidade, mas a entrada atual ainda precisa de confirmacao."
    if swing_score >= 70:
        return "Setup tecnico favoravel, mas qualidade fundamental exige cuidado."
    return "Sem assimetria clara suficiente para compra agressiva agora."


def _metrics_summary(
    snapshot: AssetSnapshot,
    *,
    relative_strength_percent: float | None,
    recent_gap: float,
) -> list[str]:
    closes = [candle.close for candle in snapshot.candles]
    latest_rsi = _last_number(rsi(closes, 14))
    metrics = [
        f"RSI: {_format_metric(latest_rsi)}",
        f"EMA 9: {_format_metric(_last_number(ema(closes, 9)))}",
        f"EMA 21: {_format_metric(_last_number(ema(closes, 21)))}",
        f"SMA 50: {_format_metric(_last_number(sma(closes, 50)))}",
        f"SMA 200: {_format_metric(_last_number(sma(closes, 200)))}",
        f"Market cap: {_format_metric(snapshot.fundamentals.market_cap)}",
        f"Market cap rank: {_format_int(snapshot.fundamentals.market_cap_rank)}",
        f"Average volume: {_format_metric(snapshot.fundamentals.average_volume)}",
    ]
    metrics.append(f"Recent gap: {_format_percent(recent_gap)}")
    if relative_strength_percent is not None:
        metrics.append(f"Relative strength: {_format_percent(relative_strength_percent)}")
    if snapshot.asset_type == "stock":
        fundamentals = snapshot.fundamentals
        metrics.extend(
            [
                f"PE: {_format_metric(fundamentals.pe)}",
                f"PEG: {_format_metric(fundamentals.peg)}",
                f"Historical PE: {_format_metric(fundamentals.historical_pe)}",
                f"Revenue growth: {_format_percent(fundamentals.revenue_growth)}",
                f"EPS growth: {_format_percent(fundamentals.eps_growth)}",
            ]
        )
        if snapshot.event is not None:
            metrics.extend(
                [
                    f"Days to earnings: {_format_int(snapshot.event.days_to_earnings)}",
                    f"Guidance recent: {_format_bool(snapshot.event.guidance_recent)}",
                    f"Post earnings gap: {_format_percent(snapshot.event.post_earnings_gap_percent)}",
                ]
            )
    else:
        metrics.extend(
            [
                f"Funding rate (8h normalized): {_format_percent(snapshot.funding_rate)}",
                f"Open interest change: {_format_percent(snapshot.open_interest_change)}",
                f"CVD proxy: {_format_metric(snapshot.cvd_proxy)}",
                f"Coinbase premium: {_format_percent(snapshot.coinbase_premium)}",
                f"Liquidation imbalance: {_format_metric(snapshot.liquidation_imbalance)}",
            ]
        )
    return metrics


def _last_number(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return float(value)
    return None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _format_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _format_int(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _format_bool(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "yes" if value else "no"
