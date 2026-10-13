# Data Model: decision evidence and market context

This is a design contract for fields carried in existing Python objects and Markdown artifacts. It does not request a database, external API, or new financial score. See [contracts/decision-evidence.md](contracts/decision-evidence.md) for transport details.

## Capability observation

| Field | Meaning and validation |
| --- | --- |
| `capability` | Stable semantic key such as `price_history`, `earnings_timing`, `backtest.expected_value_r`, or `crypto.funding_rate`; never inferred only from a reason-code suffix. |
| `scope` | `primary_report`, `stock_asset`, `crypto_asset`, `specific_rule`, `market_context`, or `discovery`; an asset observation carries its symbol and universe origin separately. |
| `status` | One of `available`, `temporary_provider_failure`, `provider_restriction`, `not_collected`, `unsupported`, `not_implemented`, `not_applicable`, `invalid`, `stale`; old/unrecognized values map to `legacy_unknown` only at the reader boundary. `available` requires rule-valid evidence. |
| `source` / `timestamp` / `unit` / `semantic_basis` | Per-capability provenance when observed. Missing provenance for a required combined observation is an integrity concern, not automatically a provider outage. |
| `reason_code` | Existing alert/limitation label if applicable. It explains status or effect but is not itself a financial dependency. |

Status transitions in a run: raw source outcome → validated capability observation → applicable rule effect → immutable report artifact. A fallback may make the capability `available` while the failed provider's restriction remains recorded separately. `not_implemented` does not transition to `temporary_provider_failure` merely because a nightly ran.

## Rule evidence effect

| Field | Meaning and validation |
| --- | --- |
| `rule_id` | Stable identifier of the existing consuming rule, preferably an existing scoring observation ID where exact correspondence exists; a local stable identifier otherwise. It must identify an actual rule path, not a fabricated rule. |
| `criticality` | One of `REQUIRED_FOR_DECISION`, `REQUIRED_FOR_SPECIFIC_RULE`, `OPTIONAL_CONTEXT`, `DIAGNOSTIC_ONLY`, `NOT_APPLICABLE`, `UNSUPPORTED`; scoped to this rule and asset/report. |
| `capabilities` | Small list of actual inputs consumed or checked. If a required one is absent/invalid, this rule cannot report `passed`. |
| `evaluation` | `passed`, `failed_observed_signal`, `unresolved_required_evidence`, or `not_applicable`. Distinguishes adverse measured data from unknown data. |
| `decision_effect` | `none`, `score_change`, `confidence_only`, `cap_to_watch`, `cap_to_technical_unvalidated`, `cap_to_wait`, `avoid`, or `blocked`, matching the existing decision vocabulary/effect. No invented cap. |
| `reason_codes` | Existing code(s) tied to this effect; may be empty for a passed rule. |

`AssetDecision` retains its existing authoritative `decision`, `alerts`, `limitations`, `reason_codes`, quality/confidence and backtest fields. The bounded effect list accompanies these fields so report and final review can explain them without rerunning scoring. At minimum all material primary approval constraints and actual downgrades must be represented. Diagnostic-only entries may be summarized rather than enumerating every optional feed.

## Report gate and final operational gate

`ReportGradeResult` remains the owner of `primary_report_grade`, `overall_report_grade`, `discovery_coverage_grade`, primary session with semantic conflict, stale count and blocking reasons. `MainReviewContext` remains the final operational gate input, adding parsed conflict as `true`, `false`, or `unknown` rather than searching diagnostic prose. A `decision_grade` primary report is not automatically operationally usable: selected artifact validity, main identity/provenance, real blocking reasons, session and required source consistency still apply. Discovery and overall grade are not substitutes for primary grade.

## Market-context observation

The report emits one normalized observation for each expected proxy (`SPY`, `QQQ`, `SMH`, `BTC`, `ETH`) only from facts already present upstream, plus existing stock/crypto regimes. Each observation has `status=observed|unavailable|legacy_unknown`, optional `price`, optional `daily_change_pct`, source, observation time, unit/currency, and context-only role. `observed` needs at least one real, usable measurement; an asset name alone is not evidence. Preserve partial fields: price may exist without a daily change. Proxies never enter ranking, primary coverage, or source decision. Final review detects a contradiction if selected raw main and carried copy disagree; it does not silently choose a value or invent one.

## Legacy and invalid records

An old main report without the new fields remains selectable under the existing artifact/identity/integrity rules, with trace and context marked `legacy_unknown`. That status does not grant a new approval, infer criticality from `missing_*`, or turn a known false cap into the new policy. A malformed declared v1 trace is an explicit source-consistency failure for that new artifact; it cannot be silently treated as valid observed capability or used to confirm a stronger decision. Existing raw reason codes and source decisions remain readable for compatibility.
