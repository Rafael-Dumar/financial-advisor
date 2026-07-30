from __future__ import annotations

from datetime import datetime, timezone

from advisor.indicators import atr, ema, rsi, sma
from advisor.models import AssetDecision, AssetSnapshot, BacktestStats, ScoredAsset
from advisor.regime import HIGH_FUNDING_RATE_8H, recent_gap_percent
from advisor.risk import calculate_trade_plan, rate_sample_quality
from advisor.runtime_scoring_observability import (
    ObservationContext,
    observe_condition,
    observe_value,
    observed_helper,
    observed_invocation,
    RuntimeTrace,
    _ObservationToken,
)


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


def _resolve_effective_now(effective_now_utc: datetime | None) -> datetime:
    if effective_now_utc is None:
        effective_now_utc = datetime.now(timezone.utc)
    if not isinstance(effective_now_utc, datetime):
        raise ValueError("effective_now_utc must be a datetime")
    if effective_now_utc.tzinfo is None or effective_now_utc.utcoffset() is None:
        raise ValueError("effective_now_utc must be timezone-aware")
    return effective_now_utc.astimezone(timezone.utc)


def _classifier_inputs(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    effective_now_utc: datetime,
    alerts_before: list[str],
    limitations_before: list[str],
) -> dict[str, object]:
    snapshot = scored.snapshot
    return {
        "effective_now_utc": effective_now_utc,
        "scored_asset": {
            "investment_quality_score": scored.investment_quality_score,
            "swing_trade_score": scored.swing_trade_score,
            "risk_plan": {
                "entry": scored.risk_plan.entry,
                "stop": scored.risk_plan.stop,
                "target_2r": scored.risk_plan.target_2r,
                "target_3r": scored.risk_plan.target_3r,
                "per_unit_risk": scored.risk_plan.per_unit_risk,
                "risk_amount": scored.risk_plan.risk_amount,
                "risk_fraction": scored.risk_plan.risk_fraction,
                "max_position_units": scored.risk_plan.max_position_units,
                "max_position_value": scored.risk_plan.max_position_value,
                "risk_reward_2r": scored.risk_plan.risk_reward_2r,
                "alerts": list(scored.risk_plan.alerts),
                "position_size_display": scored.risk_plan.position_size_display,
            },
            "alerts": list(alerts_before),
            "limitations": list(limitations_before),
            "thesis": scored.thesis,
            "metrics_summary": list(scored.metrics_summary),
            "ideal_entry": scored.ideal_entry,
            "alternative_entry": scored.alternative_entry,
            "hold_suggestion": scored.hold_suggestion,
            "snapshot": {
                "symbol": snapshot.symbol,
                "asset_type": snapshot.asset_type,
                "theme": snapshot.theme,
                "last_candle_date": snapshot.candles[-1].date if snapshot.candles else snapshot.data_timestamp,
                "data_timestamp": snapshot.data_timestamp,
                "data_source": snapshot.data_source,
                "cache_age_seconds": snapshot.cache_age_seconds,
                "event": {"days_to_earnings": snapshot.event.days_to_earnings} if snapshot.event else None,
                "news_events": [
                    {
                        "news_event_type": event.get("news_event_type"),
                        "confirmed_status": event.get("confirmed_status"),
                        "already_priced": event.get("already_priced"),
                        "market_effect": event.get("market_effect"),
                        "news_confidence": event.get("news_confidence"),
                    }
                    for event in snapshot.news_events
                ],
            },
        },
        "backtest_stats": {
            "setup_quality": backtest_stats.setup_quality,
            "sample_size": backtest_stats.sample_size,
            "win_rate_2r": backtest_stats.win_rate_2r,
            "expected_value_r": backtest_stats.expected_value_r,
            "median_days_to_2r": backtest_stats.median_days_to_2r,
            "avg_win_r": backtest_stats.avg_win_r,
            "avg_loss_r": backtest_stats.avg_loss_r,
        }
        if backtest_stats
        else None,
    }


def _classification_state(
    *,
    decision: str | None,
    max_decision: str | None,
    data_quality: str | None,
    missing_data_severity: str | None,
    data_quality_score: int | None,
    decision_confidence_score: int | None,
    alerts: list[str],
    limitations: list[str],
    reason_codes: list[str],
) -> dict[str, object]:
    return {
        "decision": decision,
        "max_decision": max_decision,
        "data_quality": data_quality,
        "missing_data_severity": missing_data_severity,
        "data_quality_score": data_quality_score,
        "decision_confidence_score": decision_confidence_score,
        "alerts": list(alerts),
        "limitations": list(limitations),
        "reason_codes": list(reason_codes),
    }


def _observe(
    context: ObservationContext | None,
    rule_id: str,
    *,
    matched: object,
    condition_inputs=None,
    branch_label: str | None = None,
    terminate_if_matched: bool = False,
):
    if context is None:
        return None
    invocation = context.invocation_stack[-1] if context.enabled and context.invocation_stack else None
    return observe_condition(
        context,
        invocation,
        rule_id,
        None,
        evaluated_value=matched,
        condition_inputs=condition_inputs,
        branch_label=branch_label,
        terminate_if_matched=terminate_if_matched,
    )


def _observe_value(
    context: ObservationContext | None,
    rule_id: str,
    value: object,
    *,
    matcher=bool,
    condition_inputs=None,
    branch_label: str | None = None,
):
    if context is None:
        return None
    invocation = context.invocation_stack[-1] if context.enabled and context.invocation_stack else None
    return observe_value(
        context,
        invocation,
        rule_id,
        None,
        evaluated_value=value,
        matcher=matcher,
        condition_inputs=condition_inputs,
        branch_label=branch_label,
    )


def _observed_if(*_args: object, **_kwargs: object):
    raise RuntimeError("_observed_if is not part of the classification flow")


def _finish_token(
    token: _ObservationToken | None,
    context: ObservationContext | None,
    **kwargs: object,
) -> None:
    if context is not None and token is not None:
        token.finish(**kwargs)


def _finish_pending_observation(
    context: ObservationContext | None,
    **kwargs: object,
) -> None:
    if context is None:
        return
    token = context._pending_branch_token
    context._pending_branch_token = None
    if token is not None:
        token.finish(**kwargs)


def _observe_elif(*_args: object, **_kwargs: object):
    raise RuntimeError("_observe_elif is not part of the classification flow")


def _state_change(field: str, before: object, candidate: object, after: object) -> dict[str, object]:
    return {
        field: {
            "before": before,
            "candidate": candidate,
            "after": after,
            "changed": before != after,
        }
    }


def _decision_change(before: object, candidate: object, after: object) -> dict[str, object]:
    return _state_change("decision", before, candidate, after)


def _finish_state_change(
    token: _ObservationToken | None,
    context: ObservationContext | None,
    field: str,
    before: object,
    candidate: object,
    after: object,
) -> None:
    if context is not None and token is not None:
        token.finish(state_changes=_state_change(field, before, candidate, after))


def _finish_decision_change(
    token: _ObservationToken | None,
    context: ObservationContext | None,
    before: object,
    candidate: object,
    after: object,
) -> None:
    if context is not None and token is not None:
        token.finish(state_changes=_decision_change(before, candidate, after))


def _ensure_observation_context(observation_context: ObservationContext | None) -> ObservationContext | None:
    return observation_context


def classify_asset(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    effective_now_utc: datetime | None = None,
) -> AssetDecision:
    effective_now = _resolve_effective_now(effective_now_utc)
    decision, _trace = _classify_asset_observed(
        scored,
        backtest_stats,
        effective_now_utc=effective_now,
        observation_context=None,
    )
    return decision


def classify_asset_with_trace(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    effective_now_utc: datetime | None = None,
    observation_context: ObservationContext | None = None,
) -> tuple[AssetDecision, RuntimeTrace]:
    effective_now = _resolve_effective_now(effective_now_utc)
    context = ObservationContext.create_enabled(effective_now) if observation_context is None else observation_context
    if not context.enabled:
        raise ValueError("classify_asset_with_trace requires an enabled ObservationContext")
    context_now = context.effective_now_utc
    if (
        not isinstance(context_now, datetime)
        or context_now.tzinfo is None
        or context_now.utcoffset() is None
        or context_now.astimezone(timezone.utc) != effective_now
    ):
        raise ValueError("observation_context effective_now_utc must match the classification time")
    context.effective_now_utc = effective_now
    return _classify_asset_observed(
        scored,
        backtest_stats,
        effective_now_utc=effective_now,
        observation_context=context,
    )


def _classify_asset_observed(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    effective_now_utc: datetime,
    observation_context: ObservationContext | None,
) -> tuple[AssetDecision, RuntimeTrace | None]:
    if observation_context is not None and not observation_context.enabled:
        observation_context = None
    root_invocation = observation_context.begin_invocation("classify_asset") if observation_context is not None else None
    alerts = list(scored.alerts)
    limitations = list(scored.limitations)
    alerts_before = list(alerts)
    limitations_before = list(limitations)
    if observation_context is not None:
        observation_context.start_trace(
            classification_inputs=_classifier_inputs(
                scored,
                backtest_stats,
                effective_now_utc,
                alerts_before,
                limitations_before,
            ),
            initial_state=_classification_state(
                decision=None,
                max_decision=None,
                data_quality=None,
                missing_data_severity=None,
                data_quality_score=None,
                decision_confidence_score=None,
                alerts=alerts_before,
                limitations=limitations_before,
                reason_codes=[],
            ),
        )
    sample_quality_token = None
    sample_quality_value = backtest_stats and backtest_stats.setup_quality
    sample_quality_matched = bool(sample_quality_value)
    if observation_context is not None:
        sample_quality_token = _observe_value(
            observation_context,
            "classify_asset.sample_quality_setup_quality",
            value=sample_quality_value,
            matcher=bool,
            condition_inputs=lambda: {
                "has_backtest_stats": backtest_stats is not None,
                "setup_quality": backtest_stats.setup_quality if backtest_stats else None,
            },
        )
        sample_quality_value = sample_quality_token.value
        sample_quality_matched = sample_quality_token.matched
    if sample_quality_matched:
        sample_quality = sample_quality_value
        if observation_context is not None and sample_quality_token is not None:
            _finish_token(sample_quality_token, observation_context)
    else:
        if observation_context is not None and sample_quality_token is not None:
            _finish_token(sample_quality_token, observation_context)
        sample_quality_derived_token = None
        sample_quality_derived_matched = bool(backtest_stats)
        if observation_context is not None:
            sample_quality_derived_token = _observe(
                observation_context,
                "classify_asset.sample_quality_derived",
                matched=sample_quality_derived_matched,
                condition_inputs=lambda: {"has_backtest_stats": backtest_stats is not None},
            )
            sample_quality_derived_matched = sample_quality_derived_token.matched
        if sample_quality_derived_matched:
            if observation_context is not None and sample_quality_derived_token is not None:
                _finish_token(sample_quality_derived_token, observation_context)
            sample_quality = rate_sample_quality(
                backtest_stats.sample_size,
                observation_context=observation_context,
            )
        else:
            if observation_context is not None and sample_quality_derived_token is not None:
                _finish_token(sample_quality_derived_token, observation_context)
            sample_quality = None
    if observation_context is not None:
        observation_context.update_classification_inputs(sample_quality=sample_quality)
    has_low_sample = backtest_stats is None or backtest_stats.sample_size < 30
    freshness = _freshness_context(
        scored.snapshot,
        limitations,
        effective_now_utc=effective_now_utc,
        observation_context=observation_context,
    )
    stale_annotation_token = None
    stale_annotation_matched = bool(freshness["is_stale"])
    if observation_context is not None:
        stale_annotation_token = _observe(
            observation_context,
            "classify_asset.stale_annotation",
            matched=stale_annotation_matched,
            condition_inputs=lambda: {"is_stale": freshness["is_stale"]},
        )
        stale_annotation_matched = stale_annotation_token.matched
    if stale_annotation_matched:
        limitations.append("stale_price_data")
        alerts.append("stale_price_data")
        if observation_context is not None and stale_annotation_token is not None:
            stale_annotation_token.finish(
                alerts_added=["stale_price_data"],
                limitations_added=["stale_price_data"],
            )
    else:
        if observation_context is not None and stale_annotation_token is not None:
            _finish_token(stale_annotation_token, observation_context)
    _apply_uncollected_context_limits(
        scored,
        backtest_stats,
        limitations,
        observation_context=observation_context,
    )
    initial_blocking_token = None
    initial_blocking_matched = bool(_has_blocking_data_gap(limitations, observation_context=observation_context))
    if observation_context is not None:
        initial_blocking_token = _observe(
            observation_context,
            "classify_asset.initial_blocking_base",
            matched=initial_blocking_matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        initial_blocking_matched = initial_blocking_token.matched
    if initial_blocking_matched:
        decision = "blocked"
        if observation_context is not None and initial_blocking_token is not None:
            _finish_token(initial_blocking_token, observation_context)
    else:
        if observation_context is not None and initial_blocking_token is not None:
            _finish_token(initial_blocking_token, observation_context)
        decision = _base_decision(scored, observation_context=observation_context)

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

    max_blocking_token = None
    max_blocking_matched = bool(_has_blocking_data_gap(limitations, observation_context=observation_context))
    if observation_context is not None:
        max_blocking_token = _observe(
            observation_context,
            "classify_asset.max_blocking_cap",
            matched=max_blocking_matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        max_blocking_matched = max_blocking_token.matched
    if not max_blocking_matched:
        if observation_context is not None and max_blocking_token is not None:
            _finish_token(max_blocking_token, observation_context)
    initial_confidence_cap_matched = None
    initial_hard_gate_cap_matched = None
    initial_low_sample_cap_matched = None
    earnings_choice_token = None
    earnings_choice_matched = None
    if max_blocking_matched:
        max_decision = "blocked"
        if observation_context is not None and max_blocking_token is not None:
            _finish_token(max_blocking_token, observation_context)
    elif (initial_confidence_cap_matched := bool(_has_confidence_limiting_data_gap(limitations, observation_context=observation_context))):
        max_decision = "watch_buy"
        limitations.append("data_incomplete_confidence_limited")
    elif (initial_hard_gate_cap_matched := bool(any(alert in hard_gates for alert in alerts))):
        earnings_choice_matched = bool("earnings_imminent" in alerts)
        if observation_context is not None:
            earnings_choice_token = _observe(
                observation_context,
                "classify_asset.hard_gate_earnings_choice",
                matched=earnings_choice_matched,
                condition_inputs=lambda: {"alerts": list(alerts)},
            )
            earnings_choice_matched = earnings_choice_token.matched
        max_decision = "wait" if earnings_choice_matched else "watch_buy"
        if observation_context is not None and earnings_choice_token is not None:
            _finish_token(earnings_choice_token, observation_context)
    elif (initial_low_sample_cap_matched := bool(has_low_sample)):
        max_decision = "watch_buy"
        limitations.append("backtest_sample_low")
    else:
        max_decision = "tradeable"

    if observation_context is not None:
        if initial_confidence_cap_matched is not None:
            initial_confidence_token = _observe(
                observation_context,
                "classify_asset.initial_confidence_cap",
                matched=initial_confidence_cap_matched,
                condition_inputs=lambda: {"limitations": list(limitations)},
            )
            if initial_confidence_token is not None:
                _finish_token(
                    initial_confidence_token,
                    observation_context,
                    limitations_added=["data_incomplete_confidence_limited"] if initial_confidence_cap_matched else (),
                )
        if initial_hard_gate_cap_matched is not None:
            initial_hard_gate_token = _observe(
                observation_context,
                "classify_asset.initial_hard_gate_cap",
                matched=initial_hard_gate_cap_matched,
                condition_inputs=lambda: {"alerts": list(alerts), "hard_gates": sorted(hard_gates)},
            )
            if initial_hard_gate_token is not None:
                _finish_token(initial_hard_gate_token, observation_context)
        if initial_low_sample_cap_matched is not None:
            initial_low_sample_token = _observe(
                observation_context,
                "classify_asset.initial_low_sample_cap",
                matched=initial_low_sample_cap_matched,
                condition_inputs=lambda: {
                    "sample_size": backtest_stats.sample_size if backtest_stats else None,
                    "has_low_sample": has_low_sample,
                },
            )
            if initial_low_sample_token is not None:
                _finish_token(
                    initial_low_sample_token,
                    observation_context,
                    limitations_added=["backtest_sample_low"] if initial_low_sample_cap_matched else (),
                )

    high_severity_token = None
    high_severity_matched = bool(_missing_data_severity(limitations, observation_context=observation_context) == "high")
    if observation_context is not None:
        high_severity_token = _observe(
            observation_context,
            "classify_asset.high_severity_cap",
            matched=high_severity_matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        high_severity_matched = high_severity_token.matched
    if high_severity_matched:
        alerts.append("high_severity_data_not_watchlist")
        if observation_context is not None and high_severity_token is not None:
            high_severity_token.finish(alerts_added=["high_severity_data_not_watchlist"])
        max_decision = _weaker_cap(max_decision, "technical_unvalidated", observation_context=observation_context)
    else:
        if observation_context is not None and high_severity_token is not None:
            _finish_token(high_severity_token, observation_context)

    stale_cap_token = None
    stale_cap_matched = bool("stale_price_data" in limitations)
    if observation_context is not None:
        stale_cap_token = _observe(
            observation_context,
            "classify_asset.stale_cap",
            matched=stale_cap_matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        stale_cap_matched = stale_cap_token.matched
    if stale_cap_matched:
        if observation_context is not None and stale_cap_token is not None:
            _finish_token(stale_cap_token, observation_context)
        max_decision = _weaker_cap(max_decision, "wait", observation_context=observation_context)
    else:
        if observation_context is not None and stale_cap_token is not None:
            _finish_token(stale_cap_token, observation_context)

    backtest_branch_token = None
    backtest_branch_matched = bool(not has_low_sample and backtest_stats and backtest_stats.win_rate_2r is not None)
    if observation_context is not None:
        backtest_branch_token = _observe(
            observation_context,
            "classify_asset.backtest_branch_entry",
            matched=backtest_branch_matched,
            condition_inputs=lambda: {
                "has_low_sample": has_low_sample,
                "has_backtest_stats": backtest_stats is not None,
                "win_rate_2r": backtest_stats.win_rate_2r if backtest_stats else None,
            },
        )
        backtest_branch_matched = backtest_branch_token.matched
    if backtest_branch_matched:
        if observation_context is not None and backtest_branch_token is not None:
            _finish_token(backtest_branch_token, observation_context)
        win_rate = backtest_stats.win_rate_2r
        ev = backtest_stats.expected_value_r
        win_rate_low_token = None
        win_rate_low_matched = bool(win_rate < 0.35)
        if observation_context is not None:
            win_rate_low_token = _observe(
                observation_context,
                "classify_asset.win_rate_below_35",
                matched=win_rate_low_matched,
                condition_inputs=lambda: {"win_rate_2r": win_rate},
            )
            win_rate_low_matched = win_rate_low_token.matched
        if not win_rate_low_matched:
            if observation_context is not None and win_rate_low_token is not None:
                _finish_token(win_rate_low_token, observation_context)
        if win_rate_low_matched:
            alerts.append("weak_setup_win_rate")
            if observation_context is not None and win_rate_low_token is not None:
                win_rate_low_token.finish(alerts_added=["weak_setup_win_rate"])
            low_win_rate_choice_token = None
            low_win_rate_choice_matched = bool(scored.investment_quality_score < 35)
            if observation_context is not None:
                low_win_rate_choice_token = _observe(
                    observation_context,
                    "classify_asset.low_win_rate_choice",
                    matched=low_win_rate_choice_matched,
                    condition_inputs=lambda: {
                        "investment_quality_score": scored.investment_quality_score,
                        "win_rate_2r": win_rate,
                    },
                )
                low_win_rate_choice_matched = low_win_rate_choice_token.matched
            new_cap = "avoid" if low_win_rate_choice_matched else "technical_unvalidated"
            if observation_context is not None and low_win_rate_choice_token is not None:
                _finish_token(low_win_rate_choice_token, observation_context)
            max_decision = _weaker_cap(
                max_decision,
                new_cap,
                observation_context=observation_context,
            )
        elif (win_rate_below_40_matched := bool(win_rate < 0.40)):
            alerts.append("weak_setup_win_rate")
            max_decision = _weaker_cap(max_decision, "wait", observation_context=observation_context)
        elif (win_rate_below_45_nonpositive_ev_matched := bool(win_rate < 0.45 and ev is not None and ev <= 0)):
            alerts.append("weak_or_negative_expected_value")
            max_decision = _weaker_cap(max_decision, "wait", observation_context=observation_context)
        elif (nonpositive_ev_matched := bool(ev is not None and ev <= 0)):
            alerts.append("weak_or_negative_expected_value")
            max_decision = _weaker_cap(max_decision, "wait", observation_context=observation_context)
        else:
            pass
        if observation_context is not None and win_rate_below_40_matched is not None:
            win_rate_below_40_token = _observe(
                observation_context,
                "classify_asset.win_rate_below_40",
                matched=win_rate_below_40_matched,
                condition_inputs=lambda: {"win_rate_2r": win_rate},
            )
            if win_rate_below_40_token is not None:
                if win_rate_below_40_matched:
                    _finish_token(
                        win_rate_below_40_token,
                        observation_context,
                        alerts_added=["weak_setup_win_rate"],
                    )
                else:
                    _finish_token(win_rate_below_40_token, observation_context)
        if observation_context is not None and win_rate_below_45_nonpositive_ev_matched is not None:
            win_rate_below_45_token = _observe(
                observation_context,
                "classify_asset.win_rate_below_45_nonpositive_ev",
                matched=win_rate_below_45_nonpositive_ev_matched,
                condition_inputs=lambda: {"win_rate_2r": win_rate, "expected_value_r": ev},
            )
            if win_rate_below_45_token is not None:
                if win_rate_below_45_nonpositive_ev_matched:
                    _finish_token(
                        win_rate_below_45_token,
                        observation_context,
                        alerts_added=["weak_or_negative_expected_value"],
                    )
                else:
                    _finish_token(win_rate_below_45_token, observation_context)
        if observation_context is not None and nonpositive_ev_matched is not None:
            nonpositive_ev_token = _observe(
                observation_context,
                "classify_asset.nonpositive_ev",
                matched=nonpositive_ev_matched,
                condition_inputs=lambda: {"expected_value_r": ev},
            )
            if nonpositive_ev_token is not None:
                if nonpositive_ev_matched:
                    _finish_token(
                        nonpositive_ev_token,
                        observation_context,
                        alerts_added=["weak_or_negative_expected_value"],
                    )
                else:
                    _finish_token(nonpositive_ev_token, observation_context)
        negative_ev_token = None
        negative_ev_matched = bool(ev is not None and ev < 0 and _missing_data_severity(limitations, observation_context=observation_context) in {"high", "critical"})
        if observation_context is not None:
            negative_ev_token = _observe(
                observation_context,
                "classify_asset.negative_ev_high_severity",
                matched=negative_ev_matched,
                condition_inputs=lambda: {"expected_value_r": ev, "limitations": list(limitations)},
            )
            negative_ev_matched = negative_ev_token.matched
        if negative_ev_matched:
            alerts.append("negative_ev_with_high_data_severity")
            if observation_context is not None and negative_ev_token is not None:
                negative_ev_token.finish(alerts_added=["negative_ev_with_high_data_severity"])
            max_decision = _weaker_cap(max_decision, "technical_unvalidated", observation_context=observation_context)
        else:
            if observation_context is not None and negative_ev_token is not None:
                _finish_token(negative_ev_token, observation_context)
    else:
        if observation_context is not None and backtest_branch_token is not None:
            _finish_token(backtest_branch_token, observation_context)

    intc_like_token = None
    intc_like_matched = bool(_is_intc_like_case(scored, alerts, backtest_stats, observation_context=observation_context))
    if observation_context is not None:
        intc_like_token = _observe(
            observation_context,
            "classify_asset.intc_like_cap",
            matched=intc_like_matched,
            condition_inputs=lambda: {
                "investment_quality_score": scored.investment_quality_score,
                "alerts": list(alerts),
                "win_rate_2r": backtest_stats.win_rate_2r if backtest_stats else None,
            },
        )
        intc_like_matched = intc_like_token.matched
    if intc_like_matched:
        if observation_context is not None and intc_like_token is not None:
            _finish_token(intc_like_token, observation_context)
        max_decision = _weaker_cap(max_decision, "technical_unvalidated", observation_context=observation_context)
    else:
        if observation_context is not None and intc_like_token is not None:
            _finish_token(intc_like_token, observation_context)

    data_quality = _data_quality(limitations, observation_context=observation_context)
    missing_severity = _missing_data_severity(limitations, observation_context=observation_context)
    data_quality_score = _data_quality_score(
        data_quality,
        missing_severity,
        limitations,
        observation_context=observation_context,
    )
    decision_confidence_score = _decision_confidence_score(
        scored,
        backtest_stats,
        data_quality_score=data_quality_score,
        limitations=limitations,
        alerts=alerts,
        observation_context=observation_context,
    )
    confidence_cap_token = None
    confidence_cap_matched = bool(decision_confidence_score < 65)
    if observation_context is not None:
        confidence_cap_token = _observe(
            observation_context,
            "classify_asset.confidence_below_65",
            matched=confidence_cap_matched,
            condition_inputs=lambda: {"decision_confidence_score": decision_confidence_score},
        )
        confidence_cap_matched = confidence_cap_token.matched
    if confidence_cap_matched:
        if observation_context is not None and confidence_cap_token is not None:
            _finish_token(confidence_cap_token, observation_context)
        max_decision = _weaker_cap(max_decision, "watch_buy", observation_context=observation_context)
    else:
        if observation_context is not None and confidence_cap_token is not None:
            _finish_token(confidence_cap_token, observation_context)
    technical_cap_token = None
    technical_cap_matched = bool(
        _is_technical_unvalidated(
            scored,
            limitations,
            backtest_stats,
            data_quality,
            missing_severity,
            sample_quality,
            observation_context=observation_context,
        )
    )
    if observation_context is not None:
        technical_cap_token = _observe(
            observation_context,
            "classify_asset.technical_unvalidated_cap",
            matched=technical_cap_matched,
            condition_inputs=lambda: {
                "swing_trade_score": scored.swing_trade_score,
                "data_quality": data_quality,
                "missing_data_severity": missing_severity,
                "sample_quality": sample_quality,
            },
        )
        technical_cap_matched = technical_cap_token.matched
    if technical_cap_matched:
        if observation_context is not None and technical_cap_token is not None:
            _finish_token(technical_cap_token, observation_context)
        max_decision = _weaker_cap(max_decision, "technical_unvalidated", observation_context=observation_context)
    else:
        if observation_context is not None and technical_cap_token is not None:
            _finish_token(technical_cap_token, observation_context)

    minimum_cap_token = None
    minimum_cap_matched = bool("below_minimum_market_cap" in alerts)
    if observation_context is not None:
        minimum_cap_token = _observe(
            observation_context,
            "classify_asset.minimum_market_cap_override",
            matched=minimum_cap_matched,
            condition_inputs=lambda: {"alerts": list(alerts)},
        )
        minimum_cap_matched = minimum_cap_token.matched
    if minimum_cap_matched:
        before_decision = decision
        decision = "avoid"
        if observation_context is not None:
            _finish_decision_change(minimum_cap_token, observation_context, before_decision, "avoid", decision)
    else:
        if observation_context is not None and minimum_cap_token is not None:
            _finish_token(minimum_cap_token, observation_context)
        decision = _apply_cap(decision, max_decision, observation_context=observation_context)
    earnings_wait_token = None
    earnings_wait_matched = bool(decision == "watch_buy" and "earnings_imminent" in alerts)
    if observation_context is not None:
        earnings_wait_token = _observe(
            observation_context,
            "classify_asset.earnings_imminent_wait",
            matched=earnings_wait_matched,
            condition_inputs=lambda: {"decision": decision, "alerts": list(alerts)},
        )
        earnings_wait_matched = earnings_wait_token.matched
    if earnings_wait_matched:
        before_decision = decision
        decision = "wait"
        if observation_context is not None:
            _finish_decision_change(earnings_wait_token, observation_context, before_decision, "wait", decision)
    else:
        if observation_context is not None and earnings_wait_token is not None:
            _finish_token(earnings_wait_token, observation_context)

    thesis = scored.thesis
    fundamental_thesis_token = None
    fundamental_thesis_matched = bool(_has_fundamental_validation_gap(limitations, observation_context=observation_context))
    if observation_context is not None:
        fundamental_thesis_token = _observe(
            observation_context,
            "classify_asset.fundamental_gap_thesis",
            matched=fundamental_thesis_matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        fundamental_thesis_matched = fundamental_thesis_token.matched
    if not fundamental_thesis_matched:
        if observation_context is not None and fundamental_thesis_token is not None:
            _finish_token(fundamental_thesis_token, observation_context)
    earnings_missing_thesis_matched = None
    if fundamental_thesis_matched:
        before_thesis = thesis
        thesis = "Setup tecnico detectado, mas dados fundamentais insuficientes impedem validacao."
        if observation_context is not None:
            _finish_state_change(fundamental_thesis_token, observation_context, "thesis", before_thesis, thesis, thesis)
    elif (earnings_missing_thesis_matched := bool("earnings_data_missing" in limitations)):
        before_thesis = thesis
        thesis = "Setup tecnico detectado, mas earnings/eventos nao verificados limitam validacao."
    else:
        pass
    if observation_context is not None and earnings_missing_thesis_matched is not None:
        earnings_missing_thesis_token = _observe(
            observation_context,
            "classify_asset.earnings_missing_thesis",
            matched=earnings_missing_thesis_matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        if earnings_missing_thesis_token is not None:
            if earnings_missing_thesis_matched:
                _finish_token(
                    earnings_missing_thesis_token,
                    observation_context,
                    state_changes=_state_change("thesis", before_thesis, thesis, thesis),
                )
            else:
                _finish_token(earnings_missing_thesis_token, observation_context)
    hold_suggestion = _hold_suggestion(scored, backtest_stats, observation_context=observation_context)
    news_summary = _news_summary(scored.snapshot.news_events, observation_context=observation_context)
    bucket = _bucket_for_decision(decision, observation_context=observation_context)
    market_session = str(freshness["market_session"])

    last_price_timestamp_matched = bool(freshness["last_price_timestamp"])
    last_price_timestamp = str(freshness["last_price_timestamp"]) if last_price_timestamp_matched else None
    last_price_timestamp_token = None
    if observation_context is not None:
        last_price_timestamp_token = _observe(
            observation_context,
            "classify_asset.last_price_timestamp_field",
            matched=last_price_timestamp_matched,
            condition_inputs=lambda: {"last_price_timestamp": freshness["last_price_timestamp"]},
        )
    if observation_context is not None and last_price_timestamp_token is not None:
        _finish_token(last_price_timestamp_token, observation_context)

    provider = scored.snapshot.data_source
    is_stale = bool(freshness["is_stale"])
    stale_reason_matched = bool(freshness["stale_reason"])
    stale_reason = str(freshness["stale_reason"]) if stale_reason_matched else None
    stale_reason_token = None
    if observation_context is not None:
        stale_reason_token = _observe(
            observation_context,
            "classify_asset.stale_reason_field",
            matched=stale_reason_matched,
            condition_inputs=lambda: {"stale_reason": freshness["stale_reason"]},
        )
    if observation_context is not None and stale_reason_token is not None:
        _finish_token(stale_reason_token, observation_context)

    event_check_status = _event_check_status(scored.snapshot, limitations, observation_context=observation_context)
    news_status_matched = bool(scored.snapshot.news_events)
    news_status = "collected" if news_status_matched else "not_collected"
    news_status_token = None
    if observation_context is not None:
        news_status_token = _observe(
            observation_context,
            "classify_asset.news_status_field",
            matched=news_status_matched,
            condition_inputs=lambda: {"news_event_count": len(scored.snapshot.news_events)},
        )
    if observation_context is not None and news_status_token is not None:
        _finish_token(news_status_token, observation_context)

    thesis_status = _thesis_status(scored, backtest_stats, limitations, observation_context=observation_context)
    sector_benchmark = _sector_benchmark(scored.snapshot.theme, observation_context=observation_context)
    short_setup_score = _short_setup_score(scored, observation_context=observation_context)
    gap_risk_matched = bool("recent_gap_risk" in alerts)
    gap_risk = "high" if gap_risk_matched else "unknown"
    gap_risk_token = None
    if observation_context is not None:
        gap_risk_token = _observe(
            observation_context,
            "classify_asset.gap_risk_field",
            matched=gap_risk_matched,
            condition_inputs=lambda: {"alerts": list(alerts)},
        )
    if observation_context is not None and gap_risk_token is not None:
        _finish_token(gap_risk_token, observation_context)

    short_status_matched = bool(_short_setup_score(scored, observation_context=observation_context) >= 70)
    short_status = "watch_only" if short_status_matched else "not_evaluated"
    short_status_token = None
    if observation_context is not None:
        short_status_token = _observe(
            observation_context,
            "classify_asset.short_status_field",
            matched=short_status_matched,
            condition_inputs=lambda: {"swing_trade_score": scored.swing_trade_score},
        )
    if observation_context is not None and short_status_token is not None:
        _finish_token(short_status_token, observation_context)

    decision_result = AssetDecision(
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
        hold_suggestion=hold_suggestion,
        backtest_stats=backtest_stats,
        sample_quality=sample_quality,
        reason_codes=sorted(set([*alerts, *limitations])),
        data_quality=data_quality,
        missing_data_severity=missing_severity,
        news_summary=news_summary,
        data_source=provider,
        data_timestamp=scored.snapshot.data_timestamp,
        cache_age_seconds=scored.snapshot.cache_age_seconds,
        bucket=bucket,
        market_session=market_session,
        last_price_timestamp=last_price_timestamp,
        provider=scored.snapshot.data_source,
        is_stale=is_stale,
        stale_reason=stale_reason,
        event_check_status=event_check_status,
        news_status=news_status,
        macro_regime="neutral",
        macro_status="not_collected",
        thesis_status=thesis_status,
        data_quality_score=data_quality_score,
        decision_confidence_score=decision_confidence_score,
        relative_strength_vs_spy=None,
        relative_strength_vs_qqq=None,
        relative_strength_vs_sector=None,
        sector_benchmark=sector_benchmark,
        short_setup_score=short_setup_score,
        squeeze_risk="unknown",
        gap_risk=gap_risk,
        borrow_data_available=False,
        short_status=short_status,
    )
    trace = None
    if observation_context is not None:
        observation_context.end_invocation(root_invocation)
        trace = observation_context.build_trace(
            final_state=_classification_state(
                decision=decision_result.decision,
                max_decision=max_decision,
                data_quality=decision_result.data_quality,
                missing_data_severity=decision_result.missing_data_severity,
                data_quality_score=decision_result.data_quality_score,
                decision_confidence_score=decision_result.decision_confidence_score,
                alerts=list(decision_result.alerts),
                limitations=list(decision_result.limitations),
                reason_codes=list(decision_result.reason_codes),
            )
        )
    return decision_result, trace


@observed_helper("_base_decision")
def _base_decision(
    scored: ScoredAsset,
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(scored.swing_trade_score < 45 or scored.investment_quality_score < 25)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.base_avoid",
            matched=matched,
            condition_inputs=lambda: {
                "swing_trade_score": scored.swing_trade_score,
                "investment_quality_score": scored.investment_quality_score,
            },
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            if context is not None and token is not None:
                _finish_token(token, context, terminated=True, termination_kind="return")
        return "avoid"
    if context is not None and token is not None:
        if context is not None and token is not None:
            if context is not None and token is not None:
                _finish_token(token, context)
    token = None
    matched = bool(scored.swing_trade_score < 60)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.base_wait",
            matched=matched,
            condition_inputs=lambda: {"swing_trade_score": scored.swing_trade_score},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            if context is not None and token is not None:
                _finish_token(token, context, terminated=True, termination_kind="return")
        return "wait"
    if context is not None and token is not None:
        if context is not None and token is not None:
            if context is not None and token is not None:
                _finish_token(token, context)
    token = None
    matched = bool(scored.swing_trade_score >= 75 and scored.investment_quality_score >= 70)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.base_tradeable",
            matched=matched,
            condition_inputs=lambda: {
                "swing_trade_score": scored.swing_trade_score,
                "investment_quality_score": scored.investment_quality_score,
            },
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            if context is not None and token is not None:
                _finish_token(token, context, terminated=True, termination_kind="return")
        return "tradeable"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(scored.swing_trade_score >= 70 and scored.investment_quality_score < 50)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.base_technical_unvalidated",
            matched=matched,
            condition_inputs=lambda: {
                "swing_trade_score": scored.swing_trade_score,
                "investment_quality_score": scored.investment_quality_score,
            },
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            if context is not None and token is not None:
                _finish_token(token, context, terminated=True, termination_kind="return")
        return "technical_unvalidated"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return "watch_buy"


@observed_helper("_apply_cap")
def _apply_cap(
    decision: str,
    cap: str,
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(_decision_rank(decision, observation_context=context) >= _decision_rank(cap, observation_context=context))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.apply_cap_rank_choice",
            matched=matched,
            condition_inputs=lambda: {"decision": decision, "cap": cap},
        )
        matched = token.matched
    result = decision if matched else cap
    if context is not None:
        _finish_decision_change(token, context, decision, cap, result)
    return result


@observed_helper("_weaker_cap")
def _weaker_cap(
    current: str,
    new_cap: str,
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(_decision_rank(current, observation_context=context) >= _decision_rank(new_cap, observation_context=context))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.weaker_cap_rank_choice",
            matched=matched,
            condition_inputs=lambda: {"current": current, "new_cap": new_cap},
        )
        matched = token.matched
    result = current if matched else new_cap
    if context is not None:
        _finish_decision_change(token, context, current, new_cap, result)
    return result


@observed_helper("_decision_rank")
def _decision_rank(
    decision: str,
    *,
    observation_context: ObservationContext | None = None,
) -> int:
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


@observed_helper("_is_intc_like_case")
def _is_intc_like_case(
    scored: ScoredAsset,
    alerts: list[str],
    backtest_stats: BacktestStats | None,
    *,
    observation_context: ObservationContext | None = None,
) -> bool:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(scored.investment_quality_score >= 45)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.intc_investment_threshold",
            matched=matched,
            condition_inputs=lambda: {"investment_quality_score": scored.investment_quality_score},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return False
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("recent_gap_risk" not in alerts)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.intc_gap_requirement",
            matched=matched,
            condition_inputs=lambda: {"alerts": list(alerts)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return False
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(not {"negative_or_invalid_pe", "negative_or_invalid_peg"} & set(alerts))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.intc_valuation_requirement",
            matched=matched,
            condition_inputs=lambda: {"alerts": list(alerts)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return False
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return bool(backtest_stats and backtest_stats.win_rate_2r is not None and backtest_stats.win_rate_2r < 0.45)


@observed_helper("_hold_suggestion")
def _hold_suggestion(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(
        backtest_stats is not None
        and backtest_stats.sample_size >= 30
        and backtest_stats.median_days_to_2r is not None
    )
    if context is not None:
        token = _observe(
            context,
            "classify_asset.hold_median_days",
            matched=matched,
            condition_inputs=lambda: {
                "sample_size": backtest_stats.sample_size if backtest_stats else None,
                "median_days_to_2r": backtest_stats.median_days_to_2r if backtest_stats else None,
            },
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return f"{backtest_stats.median_days_to_2r} dias medianos ate +2R; max {scored.hold_suggestion}"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return scored.hold_suggestion


@observed_helper("_has_confidence_limiting_data_gap")
def _has_confidence_limiting_data_gap(
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> bool:
    context = _ensure_observation_context(observation_context)
    non_blocking = {"cvd_proxy_uses_taker_buy_sell_volume", "fmp_price_light_fallback"}
    explicitly_limiting = {"earnings_data_missing", "mixed_provider_data", "news_rumor_not_confirmed", "news_confidence_low"}
    for limitation in limitations:
        token = None
        matched = bool(limitation in non_blocking)
        if context is not None:
            token = _observe(
                context,
                "classify_asset.confidence_nonblocking_skip",
                matched=matched,
                condition_inputs=lambda limitation=limitation: {"limitation": limitation},
            )
            matched = token.matched
        if matched:
            if context is not None and token is not None:
                _finish_token(token, context)
            continue
        if context is not None and token is not None:
            _finish_token(token, context)
        token = None
        matched = bool(limitation in explicitly_limiting)
        if context is not None:
            token = _observe(
                context,
                "classify_asset.confidence_explicit_limitation",
                matched=matched,
                condition_inputs=lambda limitation=limitation: {"limitation": limitation},
                terminate_if_matched=True,
            )
            matched = token.matched
        if matched:
            if context is not None and token is not None:
                _finish_token(token, context, terminated=True, termination_kind="return")
            return True
        if context is not None and token is not None:
            _finish_token(token, context)
        token = None
        matched = bool(
            limitation.startswith("missing_")
            or limitation.startswith("insufficient_")
            or limitation.endswith("_unavailable")
            or limitation.endswith("_not_live")
            or limitation.endswith("_demo")
        )
        if context is not None:
            token = _observe(
                context,
                "classify_asset.confidence_pattern_limitation",
                matched=matched,
                condition_inputs=lambda limitation=limitation: {"limitation": limitation},
                terminate_if_matched=True,
            )
            matched = token.matched
        if matched:
            if context is not None and token is not None:
                _finish_token(token, context, terminated=True, termination_kind="return")
            return True
        if context is not None and token is not None:
            _finish_token(token, context)
    return False


@observed_helper("_has_blocking_data_gap")
def _has_blocking_data_gap(
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> bool:
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


@observed_helper("_data_quality")
def _data_quality(
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(_has_blocking_data_gap(limitations, observation_context=context))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_quality_blocked",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "blocked"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(_has_confidence_limiting_data_gap(limitations, observation_context=context))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_quality_limited",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "limited"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return "ok"


@observed_helper("_missing_data_severity")
def _missing_data_severity(
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(_has_blocking_data_gap(limitations, observation_context=context))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.severity_blocking",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "critical"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(any(limitation == "earnings_data_missing" or limitation.endswith("_unavailable") for limitation in limitations))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.severity_high",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "high"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(_has_confidence_limiting_data_gap(limitations, observation_context=context))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.severity_medium",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "medium"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return "low"


def _apply_provider_context(snapshot: AssetSnapshot, alerts: list[str], limitations: list[str]) -> None:
    if snapshot.symbol in {"TSM", "ASML"}:
        alerts.append("adr_or_foreign_listing")
        alerts.append("provider_market_cap_mismatch_possible")
    if snapshot.asset_type == "stock" and snapshot.data_source in {"yahoo", "stooq", "alphavantage"}:
        alerts.append("source_mismatch_possible")
        limitations.append("mixed_provider_data")
    if snapshot.data_source in {"yahoo", "stooq", "alphavantage"}:
        alerts.append("source_mismatch_possible")


@observed_helper("_apply_uncollected_context_limits")
def _apply_uncollected_context_limits(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> None:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(not scored.snapshot.news_events)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.uncollected_news_limit",
            matched=matched,
            condition_inputs=lambda: {"news_event_count": len(scored.snapshot.news_events)},
        )
        matched = token.matched
    if matched:
        limitations.append("news_not_collected_confidence_limited")
        if context is not None and token is not None:
            token.finish(limitations_added=["news_not_collected_confidence_limited"])
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    limitations.append("macro_not_collected_confidence_limited")
    token = None
    matched = bool(
        _sector_benchmark(scored.snapshot.theme, observation_context=context)
        and scored.snapshot.asset_type == "stock"
    )
    if context is not None:
        token = _observe(
            context,
            "classify_asset.uncollected_sector_limit",
            matched=matched,
            condition_inputs=lambda: {
                "theme": scored.snapshot.theme,
                "asset_type": scored.snapshot.asset_type,
            },
        )
        matched = token.matched
    if matched:
        limitations.append("sector_relative_strength_not_collected")
        if context is not None and token is not None:
            token.finish(limitations_added=["sector_relative_strength_not_collected"])
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(
        backtest_stats is not None
        and backtest_stats.expected_value_r is not None
        and (backtest_stats.avg_win_r is None or backtest_stats.avg_loss_r is None)
    )
    if context is not None:
        token = _observe(
            context,
            "classify_asset.missing_ev_components",
            matched=matched,
            condition_inputs=lambda: {
                "expected_value_r": backtest_stats.expected_value_r if backtest_stats else None,
                "avg_win_r": backtest_stats.avg_win_r if backtest_stats else None,
                "avg_loss_r": backtest_stats.avg_loss_r if backtest_stats else None,
            },
        )
        matched = token.matched
    if matched:
        limitations.append("ev_components_missing")
        if context is not None and token is not None:
            token.finish(limitations_added=["ev_components_missing"])
    else:
        if context is not None and token is not None:
            _finish_token(token, context)


@observed_helper("_freshness_context")
def _freshness_context(
    snapshot: AssetSnapshot,
    limitations: list[str],
    *,
    effective_now_utc: datetime | None = None,
    observation_context: ObservationContext | None = None,
) -> dict[str, object]:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(snapshot.candles)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.last_price_timestamp_fallback",
            matched=matched,
            condition_inputs=lambda: {
                "candle_count": len(snapshot.candles),
                "data_timestamp": snapshot.data_timestamp,
            },
        )
        matched = token.matched
    last_price_timestamp = snapshot.candles[-1].date if matched else snapshot.data_timestamp
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    is_stale = "stale_price_data" in limitations
    token = None
    matched = bool(is_stale)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.freshness_stale_reason_choice",
            matched=matched,
            condition_inputs=lambda: {"is_stale": is_stale},
        )
        matched = token.matched
    stale_reason = "price_cache_or_last_candle_stale" if matched else None
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(snapshot.cache_age_seconds is not None and snapshot.cache_age_seconds > 60 * 60 * 24)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.cache_stale",
            matched=matched,
            condition_inputs=lambda: {"cache_age_seconds": snapshot.cache_age_seconds},
        )
        matched = token.matched
    if matched:
        is_stale = True
        stale_reason = "cache_age_exceeds_24h"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return {
        "market_session": _market_session(effective_now_utc, observation_context=context),
        "last_price_timestamp": last_price_timestamp,
        "is_stale": is_stale,
        "stale_reason": stale_reason,
    }


@observed_helper("_market_session")
def _market_session(
    now: datetime | None = None,
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    now = now or datetime.now(timezone.utc)
    # Fixed UTC windows keep this dependency-free; report still labels unknown if clocks drift.
    weekday = now.weekday()
    minutes = (now.hour * 60) + now.minute
    token = None
    matched = bool(weekday >= 5)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.market_session_weekend",
            matched=matched,
            condition_inputs=lambda: {"weekday": weekday, "effective_now_utc": now},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "closed"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(13 * 60 + 30 <= minutes < 20 * 60)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.market_session_regular",
            matched=matched,
            condition_inputs=lambda: {"minutes": minutes, "effective_now_utc": now},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "regular"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(8 * 60 <= minutes < 13 * 60 + 30)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.market_session_pre",
            matched=matched,
            condition_inputs=lambda: {"minutes": minutes, "effective_now_utc": now},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "pre_market"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(20 * 60 <= minutes < 24 * 60)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.market_session_after",
            matched=matched,
            condition_inputs=lambda: {"minutes": minutes, "effective_now_utc": now},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "after_hours"
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return "closed"


@observed_helper("_has_fundamental_validation_gap")
def _has_fundamental_validation_gap(
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> bool:
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


@observed_helper("_is_technical_unvalidated")
def _is_technical_unvalidated(
    scored: ScoredAsset,
    limitations: list[str],
    backtest_stats: BacktestStats | None,
    data_quality: str,
    missing_severity: str,
    sample_quality: str | None,
    *,
    observation_context: ObservationContext | None = None,
) -> bool:
    context = _ensure_observation_context(observation_context)
    token = None
    ev = backtest_stats.expected_value_r if backtest_stats else None
    if context is not None:
        token = _observe_value(
            context,
            "classify_asset.technical_ev_presence",
            value=ev,
            matcher=lambda value: value is not None,
            condition_inputs=lambda: {
                "has_backtest_stats": backtest_stats is not None,
                "expected_value_r": backtest_stats.expected_value_r if backtest_stats else None,
            },
        )
        ev = token.value
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    return (
        scored.swing_trade_score >= 70
        and (
            missing_severity in {"high", "critical"}
            or data_quality == "limited"
            or _has_fundamental_validation_gap(limitations, observation_context=context)
            or (ev is not None and ev <= 0)
            or sample_quality == "low"
        )
    )


@observed_helper("_data_quality_score")
def _data_quality_score(
    data_quality: str,
    missing_severity: str,
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> int:
    context = _ensure_observation_context(observation_context)
    score = 95
    token = None
    matched = bool(data_quality == "blocked")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_score_blocked",
            matched=matched,
            condition_inputs=lambda: {"data_quality": data_quality},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            token.finish(
                state_changes=_state_change("data_quality_score", score, 0, 0),
                terminated=True,
                termination_kind="return",
            )
        return 0
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(data_quality == "limited")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_score_limited",
            matched=matched,
            condition_inputs=lambda: {"data_quality": data_quality},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 65)
        if context is not None:
            _finish_state_change(token, context, "data_quality_score", before_score, 65, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(missing_severity == "high")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_score_high_severity",
            matched=matched,
            condition_inputs=lambda: {"missing_data_severity": missing_severity},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 55)
        if context is not None:
            _finish_state_change(token, context, "data_quality_score", before_score, 55, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(missing_severity == "critical")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_score_critical_severity",
            matched=matched,
            condition_inputs=lambda: {"missing_data_severity": missing_severity},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 20)
        if context is not None:
            _finish_state_change(token, context, "data_quality_score", before_score, 20, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("earnings_data_missing" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_score_earnings_missing",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 60)
        if context is not None:
            _finish_state_change(token, context, "data_quality_score", before_score, 60, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("stale_price_data" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.data_score_stale",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 50)
        if context is not None:
            _finish_state_change(token, context, "data_quality_score", before_score, 50, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    return max(0, score)


@observed_helper("_decision_confidence_score")
def _decision_confidence_score(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    data_quality_score: int,
    limitations: list[str],
    alerts: list[str],
    observation_context: ObservationContext | None = None,
) -> int:
    context = _ensure_observation_context(observation_context)
    score = min(data_quality_score, int(round((scored.investment_quality_score + scored.swing_trade_score) / 2)))
    token = None
    sample_size = backtest_stats.sample_size if backtest_stats else 0
    if context is not None:
        token = _observe_value(
            context,
            "classify_asset.confidence_sample_size_presence",
            value=sample_size,
            matcher=lambda _value: backtest_stats is not None,
            condition_inputs=lambda: {"has_backtest_stats": backtest_stats is not None},
        )
        sample_size = token.value
    if context is not None and token is not None:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool(sample_size < 30)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_sample_low",
            matched=matched,
            condition_inputs=lambda: {"sample_size": sample_size},
        )
        matched = token.matched
    if not matched:
        if context is not None and token is not None:
            _finish_token(token, context)
    confidence_sample_medium_matched = None
    if matched:
        before_score = score
        score = min(score, 45)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 45, score)
    elif (confidence_sample_medium_matched := bool(sample_size < 100)):
        before_score = score
        score = min(score, 70)
    else:
        pass
    if context is not None and confidence_sample_medium_matched is not None:
        medium_token = _observe(
            context,
            "classify_asset.confidence_sample_medium",
            matched=confidence_sample_medium_matched,
            condition_inputs=lambda: {"sample_size": sample_size},
        )
        if medium_token is not None:
            if confidence_sample_medium_matched:
                _finish_token(
                    medium_token,
                    context,
                    state_changes=_state_change("decision_confidence_score", before_score, 70, score),
                )
            else:
                _finish_token(medium_token, context)
    token = None
    matched = bool(backtest_stats and backtest_stats.expected_value_r is not None and backtest_stats.expected_value_r <= 0)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_nonpositive_ev",
            matched=matched,
            condition_inputs=lambda: {
                "expected_value_r": backtest_stats.expected_value_r if backtest_stats else None,
            },
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 50)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 50, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("earnings_data_missing" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_earnings_missing",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 55)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 55, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("mixed_provider_data" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_mixed_provider",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 55)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 55, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("market_not_risk_on" in alerts)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_neutral_market",
            matched=matched,
            condition_inputs=lambda: {"alerts": list(alerts)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 75)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 75, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("market_risk_off" in alerts)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_risk_off",
            matched=matched,
            condition_inputs=lambda: {"alerts": list(alerts)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 45)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 45, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("stale_price_data" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_stale",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 45)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 45, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("news_rumor_not_confirmed" in limitations or "news_confidence_low" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_news_low",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 55)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 55, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("news_not_collected_confidence_limited" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_news_not_collected",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 80)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 80, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("macro_not_collected_confidence_limited" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_macro_not_collected",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 75)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 75, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("sector_relative_strength_not_collected" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_sector_not_collected",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 70)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 70, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    token = None
    matched = bool("ev_components_missing" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.confidence_ev_components",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
        )
        matched = token.matched
    if matched:
        before_score = score
        score = min(score, 60)
        if context is not None:
            _finish_state_change(token, context, "decision_confidence_score", before_score, 60, score)
    else:
        if context is not None and token is not None:
            _finish_token(token, context)
    return max(0, score)


@observed_helper("_event_check_status")
def _event_check_status(
    snapshot: AssetSnapshot,
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(snapshot.asset_type == "crypto")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.event_crypto",
            matched=matched,
            condition_inputs=lambda: {"asset_type": snapshot.asset_type},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "not_applicable"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool("earnings_unavailable" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.event_source_unavailable",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "source_unavailable"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool("earnings_data_missing" in limitations)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.event_not_collected",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "not_collected"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(snapshot.event is not None and snapshot.event.days_to_earnings is not None)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.event_verified",
            matched=matched,
            condition_inputs=lambda: {
                "has_event": snapshot.event is not None,
                "days_to_earnings": snapshot.event.days_to_earnings if snapshot.event else None,
            },
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "verified"
    if context is not None and token is not None:
        _finish_token(token, context)
    return "not_collected"


@observed_helper("_bucket_for_decision")
def _bucket_for_decision(
    decision: str,
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(decision in {"tradeable", "watch_buy", "watch_only", "technical_unvalidated", "wait", "blocked", "avoid"})
    if context is not None:
        token = _observe(
            context,
            "classify_asset.bucket_known",
            matched=matched,
            condition_inputs=lambda: {"decision": decision},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return decision
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(decision == "speculative_watch")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.bucket_speculative",
            matched=matched,
            condition_inputs=lambda: {"decision": decision},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "technical_unvalidated"
    if context is not None and token is not None:
        _finish_token(token, context)
    return "avoid"


@observed_helper("_thesis_status")
def _thesis_status(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    limitations: list[str],
    *,
    observation_context: ObservationContext | None = None,
) -> str:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(_has_fundamental_validation_gap(limitations, observation_context=context))
    if context is not None:
        token = _observe(
            context,
            "classify_asset.thesis_fundamental_gap",
            matched=matched,
            condition_inputs=lambda: {"limitations": list(limitations)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "unknown"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(scored.investment_quality_score >= 70 and scored.swing_trade_score >= 70)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.thesis_strengthening",
            matched=matched,
            condition_inputs=lambda: {
                "investment_quality_score": scored.investment_quality_score,
                "swing_trade_score": scored.swing_trade_score,
            },
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "strengthening"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(backtest_stats and backtest_stats.expected_value_r is not None and backtest_stats.expected_value_r < 0)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.thesis_weakening",
            matched=matched,
            condition_inputs=lambda: {
                "expected_value_r": backtest_stats.expected_value_r if backtest_stats else None,
            },
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "weakening"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(scored.investment_quality_score >= 55)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.thesis_stable",
            matched=matched,
            condition_inputs=lambda: {"investment_quality_score": scored.investment_quality_score},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "stable"
    if context is not None and token is not None:
        _finish_token(token, context)
    return "unknown"


@observed_helper("_sector_benchmark")
def _sector_benchmark(
    theme: str,
    *,
    observation_context: ObservationContext | None = None,
) -> str | None:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(theme == "semiconductors")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.sector_semiconductors",
            matched=matched,
            condition_inputs=lambda: {"theme": theme},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "SMH"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(theme in {"software", "software_ai"})
    if context is not None:
        token = _observe(
            context,
            "classify_asset.sector_software",
            matched=matched,
            condition_inputs=lambda: {"theme": theme},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "IGV"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(theme == "cloud_ecommerce")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.sector_cloud",
            matched=matched,
            condition_inputs=lambda: {"theme": theme},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "QQQ"
    if context is not None and token is not None:
        _finish_token(token, context)
    token = None
    matched = bool(theme == "healthcare")
    if context is not None:
        token = _observe(
            context,
            "classify_asset.sector_healthcare",
            matched=matched,
            condition_inputs=lambda: {"theme": theme},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return "XLV"
    if context is not None and token is not None:
        _finish_token(token, context)
    return None


@observed_helper("_short_setup_score")
def _short_setup_score(
    scored: ScoredAsset,
    *,
    observation_context: ObservationContext | None = None,
) -> float:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(scored.swing_trade_score <= 35)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.short_setup_threshold",
            matched=matched,
            condition_inputs=lambda: {"swing_trade_score": scored.swing_trade_score},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return round(100 - scored.swing_trade_score, 2)
    if context is not None and token is not None:
        _finish_token(token, context)
    return 0.0


@observed_helper("_news_summary")
def _news_summary(
    news_events: list[dict[str, object]],
    *,
    observation_context: ObservationContext | None = None,
) -> str | None:
    context = _ensure_observation_context(observation_context)
    token = None
    matched = bool(not news_events)
    if context is not None:
        token = _observe(
            context,
            "classify_asset.news_summary_empty",
            matched=matched,
            condition_inputs=lambda: {"news_event_count": len(news_events)},
            terminate_if_matched=True,
        )
        matched = token.matched
    if matched:
        if context is not None and token is not None:
            _finish_token(token, context, terminated=True, termination_kind="return")
        return None
    if context is not None and token is not None:
        _finish_token(token, context)
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
