# Internal artifact contract: decision evidence and market context v1

This is a proposed internal Markdown contract between the main report, analyst-review input, nightly input, and final review. It does not define a new network API. Existing `decision_label`, `reason_codes`, `alerts`, `limitations`, grades, provenance fields, report headings, artifact identities, and `## Telegram summary` remain compatible. The selected **raw main report** remains authoritative for source decisions and primary grade.

## Per-asset decision evidence

The main report's primary asset block carries `- evidence_trace_version: 1` and `- evidence_trace_v1: <single-line compact JSON object>`. The serializer emits UTF-8 JSON with deterministic key and effect ordering; the parser reads only that field in the selected asset block and rejects malformed/duplicate keys and oversized values. No Markdown prose extraction is necessary to classify the payload. Existing report field parsing may trim surrounding Markdown backticks if present, but the new writer does not require them.

The logical payload is:

```json
{
  "source_decision": "technical_unvalidated",
  "asset_symbol": "EXAMPLE",
  "universe_origin": "primary",
  "effects": [
    {
      "rule_id": "existing-rule-id",
      "capability": "earnings_timing",
      "scope": "stock_asset",
      "criticality": "REQUIRED_FOR_DECISION",
      "status": "not_collected",
      "evaluation": "unresolved_required_evidence",
      "decision_effect": "cap_to_technical_unvalidated",
      "reason_code": "earnings_data_missing",
      "source": null,
      "observed_at": null
    }
  ]
}
```

This example illustrates shape, not a mandate to cap every unknown earnings event specifically to `technical_unvalidated`: the existing decision ladder chooses its supported nontradeable state. Each effect must say which specific rule consumed the capability and whether the effect was caused by a measured signal or by unresolved required evidence. Existing reason codes remain identifiers, not criticality inputs. A trace mismatch with source decision is reported as an integrity inconsistency; the review cannot use it to promote an asset. Full review shows enough of the trace to audit material primary decisions; Telegram uses only a concise operative reason.

**Validation:** allowed enum values only; sane size and count bounds; matching asset symbol and origin; no duplicate semantic effect key; finite numeric observations where present; source and observation time retained per capability when needed; no `passed` for an unavailable required input; no `observed` adverse signal synthesized from missing data. `legacy_unknown` is assigned by a reader to an old artifact without v1, never serialized as a fresh source observation. Malformed or source-decision-conflicting v1 is marked `data_contract_invalid` as a source-consistency failure for that new artifact; it cannot support a stronger approval and is not treated as an empty successful trace.

## Market-context observations

One `## Market context observations v1` section is emitted by the report renderer from in-memory facts already used in the run and is copied into main/analyst-review input through their existing artifact paths. It contains `- market_context_version: 1` and `- market_context_v1: <single-line compact JSON object>` with deterministic ordering and strict parsing. Logical entries are `SPY`, `QQQ`, `SMH`, `BTC`, `ETH`, `stock_regime`, `crypto_regime`. Each has `status`, `source`, `observed_at`, and available `price` and/or `daily_change_pct` (regimes carry a label instead). Per-value units/currency and session/time basis must be explicit when material to interpretation. `status=unavailable` never includes fabricated price/change. A partially observed proxy keeps its observed fields and labels the absent field. These proxies are `context`, never an asset `source_decision` or primary coverage member. The nightly packager's copied analyst input carries this section; its short `Main summary` need not duplicate it.

Selected raw main is the authoritative copy. If the analyst/nightly copy is absent, retain the raw main observation. If a second copy disagrees on a material value/source/time, render `transport_conflict` and do not present the conflict as a provider outage. No new collection occurs in final review. Old artifacts without this section yield `legacy_unknown`; existing regime labels and asset prose may still be displayed as legacy context but cannot be silently certified as new normalized observations.

## Gate/session and no-trade contract

The main retains `primary_report_grade`, `overall_report_grade`, `discovery_coverage_grade`, `market_session_conflict`, `blocking_reasons` and established artifact identity fields. The review parses conflict strictly as `true`, `false`, or `unknown`; field presence or text containing `market_session_conflict` is not a boolean. `false` cannot cause a session warning. `true` follows the existing session/integrity gate. `unknown` is disclosed and handled according to the existing missing-gate policy, never treated as proof of either conflict or safety.

If primary and final-review data grades are decision-grade, the main artifact is valid and operationally usable, and no primary asset is approved, the final decision can be `no_trade`. Its explanation identifies asset/rule evidence or measured thesis/risk conditions and must not claim that the main is unusable or awaiting decision-grade data. Overall grade degradation caused only by discovery remains diagnostic. Runtime scoring manifest is observational and cannot supply an approval or blocker.

## Compatibility and authority

The v1 fields are additive. Old readers continue to consume existing fields. New readers accept legacy artifacts as `legacy_unknown` for trace/context while preserving source decision and existing main integrity gates. They never infer `REQUIRED_FOR_DECISION` from a `missing_*` or `*_unavailable` string in a legacy artifact. New main artifacts must not omit v1 material effects silently. A missing/invalid declared v1 trace is a source-consistency problem for that new artifact and cannot support operational approval until reconciled. Actual approval still depends on authoritative scoring plus the existing integrity gate; the final review does not independently rescore or approve.
