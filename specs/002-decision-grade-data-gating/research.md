# Research: current architecture and bounded design choices

Baseline: `10d4ea364b4860fd1e3162dc3da2ab996a0a4184`. The authoritative rule audit is [clarifications/evidence-matrix.md](clarifications/evidence-matrix.md). This file records architecture decisions for `/speckit.plan`, not new financial policy or executed validation.

## Decision: Rule branches own capability criticality

**Rationale:** `advisor/scoring.py` already applies financial thresholds and decision caps. Its `_has_confidence_limiting_data_gap`, `_missing_data_severity`, and related helpers currently assign financial meaning to reason-code spellings. A compact rule-scoped effect recorded where the existing branch executes prevents a second financial engine and lets the report show why a cap occurred. `AssetDecision` is the natural output boundary. The optional 97-rule runtime trace can provide diagnostic rule IDs but cannot be required for every nightly, since intake is observational and sometimes absent.

**Alternatives rejected:** A universal feed registry would imply every field is globally required and duplicate existing rule applicability. Reclassifying limitations in `analyst_review.py` would be too late and could contradict the source decision. Deleting all missing-data checks would weaken genuine history/freshness gates.

## Decision: Keep status separate from criticality and provider brand

**Rationale:** `AssetSnapshot` has source/fetch/status/provenance fields, but one provider's failure does not prove loss of a capability supplied by an authorized fallback. Missing price history is different from missing optional CVD or an unimplemented liquidation feed. The consuming rule must see both status and criticality, including invalid/stale evidence. Retain raw provider attempts for audit.

**Alternatives rejected:** A single `data_incomplete` flag loses scope and cause. Requiring one provider to supply all fields or adding a paid feed violates the approved free-first contract.

## Decision: Close the approved event and statistical gates locally

**Rationale:** `score_asset` already measures near/imminent earnings; classification is where approval is capped. Empty/past-only earnings responses do not prove a safe upcoming window. `advisor/backtest.py` computes expected value directly from all R-multiples; no observed loss can legitimately mean `avg_loss_r=None`. The classifier consumes sample sufficiency, win rate, and expected value. Missing consumed values must leave the applicable comparison unresolved; missing diagnostic averages cannot cap valid EV.

**Alternatives rejected:** Making earnings a report-grade prerequisite would contaminate unrelated assets. Reconstructing EV from win/loss averages would contradict the current backtest calculation. Treating a missing win rate/EV as zero would fabricate a measured bad statistic.

## Decision: Validate actual provider compatibility

**Rationale:** `data_source` currently triggers `mixed_provider_data`/`source_mismatch_possible` merely for stock fallback brands, while snapshots include source timestamps and per-capability provenance. Compatibility is about identity, venue/instrument meaning, currency and units, metric definition, timeframe, freshness, same-capability conflict, and traceability. A concrete incompatibility should have a specific reason and only affect the consuming rule.

**Alternatives rejected:** Blanket confidence cap for Yahoo/FMP mixing; silently declaring all provider combinations compatible; adding a new provider abstraction or external service.

## Decision: Normalize market facts at report emission

**Rationale:** `advisor/cli.py` already holds benchmark candles and BTC/ETH snapshots when it renders main and analyst-review input. `advisor/report.py` presently emits regime labels and some provenance, but not benchmark observations. `_metrics_summary` does not emit the `Last price`/`Daily change` strings the Market Brief parser expects for BTC/ETH. One versioned context section built from the actual in-memory observations allows the selected raw main and nightly transport to carry the same facts. It does not add SPY/QQQ/SMH to the primary universe.

**Alternatives rejected:** More ad hoc prose scraping, a new fetch in final review, adding benchmarks as scored assets, or a PowerShell-only synthesis with no source-level provenance.

## Decision: Final review consumes authoritative structured state

**Rationale:** `_telegram_reason` currently matches the presence of `market_session_conflict` even when the value is `false`; `_objective_reading_lines` always says to await a decision-grade main on no-trade paths. The selected main context already carries primary grade, artifact validity, and blockers; extend its parsed session state to false/true/unknown, then branch on those semantic values. The existing `main_blocks_operation` remains the authority after grade evaluation. Discovery-only overall diagnostic stays visible, not operational.

**Alternatives rejected:** Recomputing grades from final-review coverage, using another substring test, or making Telegram presentation approve a trade.

## Decision: Bound runtime audit to observability

**Rationale:** `advisor/cli.py` can write `reports/runtime`; the reports workflow uploads `reports`; `scripts/fetch-latest-github-reports.ps1` downloads selected main/close runs; `advisor/runtime_scoring_intake.py` only recognizes an allowlisted bundle with an immediate `runtime` parent. A missing bundle in the selected root yields `runtime_missing/not_found`. The cited production run trees and producer logs are unavailable, so the exact loss point is not proven. Existing final-review tests establish that manifest variants do not alter decision authority.

**Alternatives rejected:** Treating runtime audit as a trade gate or editing workflow YAML before evidence identifies a path/upload mismatch.

## Resolved unknowns

The three prior financial-policy questions are settled in the spec: verified earnings timing is required for stock trade approval; consumed sample/win-rate/EV evidence remains required while win/loss averages are diagnostic; mixed-provider evidence is acceptable unless a concrete capability incompatibility is shown. No unresolved financial-policy item remains. Runtime historical attribution is a bounded implementation investigation.
