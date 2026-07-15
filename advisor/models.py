from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candle:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Fundamentals:
    pe: float | None
    peg: float | None
    historical_pe: float | None
    revenue_growth: float | None
    eps_growth: float | None
    margin_trend: float | None
    free_cash_flow_positive: bool | None
    market_cap: float | None
    average_volume: float | None
    market_cap_rank: int | None = None


@dataclass(frozen=True)
class EventInfo:
    days_to_earnings: int | None
    guidance_recent: bool | None
    post_earnings_gap_percent: float | None
    last_earnings_date: str | None = None
    next_earnings_date: str | None = None


@dataclass(frozen=True)
class DataFetchMetadata:
    provider: str
    endpoint: str
    fetched_at: str | None = None
    cache_fetched_at: str | None = None
    source_timestamp: str | None = None
    cache_age_seconds: int | None = None
    source_age_seconds: int | None = None
    is_fresh: bool | None = None
    cache_hit: bool = False
    fallback_used: bool = False
    fallback_from: str | None = None
    fallback_to: str | None = None
    granularity: str | None = None
    market_data_kind: str | None = None


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    capability: str
    configured: bool
    supported_by_plan: bool
    implemented: bool
    last_status: str
    fallback_available: bool


@dataclass(frozen=True)
class AssetSnapshot:
    symbol: str
    asset_type: str
    theme: str
    candles: list[Candle]
    fundamentals: Fundamentals
    event: EventInfo | None = None
    funding_rate: float | None = None
    open_interest_change: float | None = None
    cvd_proxy: float | None = None
    coinbase_premium: float | None = None
    liquidation_imbalance: float | None = None
    missing_data: list[str] = field(default_factory=list)
    news_events: list[dict[str, object]] = field(default_factory=list)
    provider_capabilities: list[ProviderCapability] = field(default_factory=list)
    earnings_status: str = "not_implemented"
    guidance_status: str = "not_implemented"
    macro_status: str = "not_implemented"
    news_status: str = "not_configured"
    sec_filings_status: str = "not_implemented"
    data_source: str = "unknown"
    data_timestamp: str | None = None
    cache_age_seconds: int | None = None
    data_fetch_metadata: DataFetchMetadata | None = None
    quote_status: str = "not_requested"
    quote_price: float | None = None
    quote_timestamp: str | None = None
    quote_source: str | None = None
    quote_age_seconds: int | None = None
    quote_is_intraday: bool = False
    previous_close: float | None = None
    daily_change: float | None = None
    daily_change_pct: float | None = None
    benchmark_provenance: dict[str, object] = field(default_factory=dict)
    crypto_metric_provenance: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketRegime:
    label: str
    reasons: list[str]


@dataclass(frozen=True)
class RiskPlan:
    entry: float
    stop: float
    target_2r: float
    target_3r: float
    per_unit_risk: float
    risk_amount: float
    risk_fraction: float
    max_position_units: float
    max_position_value: float
    risk_reward_2r: str
    alerts: list[str]
    position_size_display: str = ""


@dataclass(frozen=True)
class LeveragePolicy:
    allowed: bool
    reasons: list[str]


@dataclass(frozen=True)
class BacktestOutcome:
    hit_2r: bool
    hit_3r: bool
    stopped: bool
    expired: bool
    days_held: int
    exit_reason: str
    days_to_2r: int | None = None
    days_to_3r: int | None = None
    r_multiple: float = 0.0


@dataclass(frozen=True)
class BacktestStats:
    sample_size: int
    win_rate_2r: float | None
    win_rate_3r: float | None
    median_days_to_2r: int | None = None
    median_days_to_3r: int | None = None
    expected_value_r: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    setup_quality: str | None = None
    max_drawdown_r: float | None = None
    period_start: str | None = None
    period_end: str | None = None
    benchmark_comparison: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScoredAsset:
    snapshot: AssetSnapshot
    investment_quality_score: float
    swing_trade_score: float
    risk_plan: RiskPlan
    alerts: list[str]
    limitations: list[str]
    thesis: str
    metrics_summary: list[str]
    ideal_entry: float
    alternative_entry: float | None
    hold_suggestion: str


@dataclass(frozen=True)
class AssetDecision:
    symbol: str
    asset_type: str
    decision: str
    investment_quality_score: float
    swing_trade_score: float
    risk_plan: RiskPlan
    alerts: list[str]
    limitations: list[str]
    thesis: str
    metrics_summary: list[str]
    ideal_entry: float
    alternative_entry: float | None
    hold_suggestion: str
    backtest_stats: BacktestStats | None
    sample_quality: str | None
    reason_codes: list[str] = field(default_factory=list)
    data_quality: str = "unknown"
    missing_data_severity: str = "unknown"
    news_summary: str | None = None
    data_source: str = "unknown"
    data_timestamp: str | None = None
    cache_age_seconds: int | None = None
    bucket: str = "unknown"
    market_session: str = "unknown"
    last_price_timestamp: str | None = None
    provider: str = "unknown"
    is_stale: bool = False
    stale_reason: str | None = None
    event_check_status: str = "not_collected"
    news_status: str = "not_collected"
    macro_regime: str = "neutral"
    macro_status: str = "not_collected"
    thesis_status: str = "unknown"
    data_quality_score: int = 0
    decision_confidence_score: int = 0
    relative_strength_vs_spy: float | None = None
    relative_strength_vs_qqq: float | None = None
    relative_strength_vs_sector: float | None = None
    sector_benchmark: str | None = None
    short_setup_score: float = 0
    squeeze_risk: str = "unknown"
    gap_risk: str = "unknown"
    borrow_data_available: bool = False
    short_status: str = "not_evaluated"
    universe_origin: str = "unknown"
