# Feature Specification: Decision-Grade Data Completeness & Truthful Nightly Gating

**Feature directory**: `specs/002-decision-grade-data-gating`  
**Status**: Ready for clarification/planning  
**Created**: 2026-10-10  
**Baseline**: `10d4ea364b4860fd1e3162dc3da2ab996a0a4184`

## Problem statement and user value

Two consecutive scheduled production nightlies completed and sent reviews, yet the reader could not reliably tell whether a conservative decision reflected the market thesis or incomplete data. The main report stated `data_mode=live`, `report_grade=decision_grade`, `primary_report_grade=decision_grade`, `market_session=regular`, `market_session_conflict=false`, `blocking_reasons=nenhum`, `artifact_valid=true`, `operational_decision_allowed=true`, and `nightly_input_incomplete=false`. The final review also stated `report_data_grade=decision_grade` and `main_report_grade_blocked_operation=false`, while its Telegram explanation warned that session classification might be wrong and the main was not used as a decision basis. Another instruction told the reader to wait for a decision-grade main that already existed.

The reader needs a trustworthy account of which evidence is present, which is absent, why, and whether each gap actually limits the report, an asset decision, or only discovery. A genuine `no_trade`, `technical_unvalidated`, `wait`, `avoid`, or `blocked` outcome remains valid. Its stated reason must agree with the evidence and the authoritative decision state.

## User Scenarios & Testing

### Scenario 1 — Decision-grade main, no approved trade

**Given** a live, regular, non-conflicting, decision-grade primary report with a valid artifact, no primary blocking reasons, and complete nightly input, **when** no asset is approved, **then** the review and message retain `no_trade` and explain the actual asset or strategy reasons. They do not claim that the main was unusable, that the session may be wrong, or that a decision-grade main must be awaited.

### Scenario 2 — True session conflict

**Given** authoritative session evidence identifies a conflict, **when** the nightly is reviewed, **then** the affected decision basis is gated and the reader sees a session-specific explanation tied to that conflict. A diagnostic field whose value is `false` does not produce this explanation.

### Scenario 3 — Critical primary evidence missing

**Given** evidence required by the applicable primary-report gate or asset decision rule is missing, stale, invalid, or unverifiable, **when** the report and review are produced, **then** the affected report or asset cannot claim unsupported eligibility. An asset-level gap does not automatically downgrade a primary report that still satisfies its own grade contract. The reader sees the affected scope, capability, evidence status, and resulting limitation.

### Scenario 4 — Optional or unsupported evidence absent

**Given** a feed or measurement is optional for the applicable decision, unavailable from its provider, or not implemented, **when** it is absent, **then** the absence is accurately labeled and is not silently converted into a critical-data failure or a market conclusion. Any asset-specific limitation it legitimately causes remains visible.

### Scenario 5 — Discovery degrades

**Given** primary-universe evidence is sufficient but a discovery asset cannot be collected or evaluated, **when** the nightly completes, **then** the primary report's decision-grade and operational eligibility remain based on primary evidence. The discovery gap is visible in discovery coverage and cannot turn into a generic primary-data blocker or an approved trade.

### Scenario 6 — Transport or interpretation gap

**Given** source evidence exists but a later report, parser, or presentation boundary loses or misreads it, **when** the review is produced, **then** the contradiction is identified as a boundary/data-integrity problem. The system does not assert that the market source was absent, invent a value, or present a stronger decision than the authoritative evidence supports.

### Scenario 7 — Multiple limitations

**Given** an asset has a market or thesis reason and one or more data limitations, **when** it is classified as `technical_unvalidated`, `wait`, `avoid`, or `blocked`, **then** its explanation distinguishes the decision reason from missing evidence and identifies which limitation actually changed eligibility. Generic safety language cannot replace a known specific cause.

### Scenario 8 — Provider restriction with valid alternative evidence

**Given** one provider is restricted but another authorized free provider supplies a valid required capability for a crypto asset, **when** the existing decision rule evaluates that capability, **then** it is treated as available with its actual provenance. The restricted provider is reported accurately but its brand-specific failure does not make the asset's basic data unavailable. If no valid source supplies a required capability, the affected rule remains limited.

### Scenario 9 — Observed market context crosses report boundaries

**Given** SPY, QQQ, SMH, BTC, ETH, stock-regime, or crypto-regime observations exist upstream, **when** the nightly review is assembled, **then** those observed facts survive each applicable report and review boundary with traceable provenance. A downstream `missing` label for upstream BTC or ETH analysis is identified as a possible transport or interpretation gap, not automatically a provider failure. Truly absent upstream facts remain unavailable.

### Scenario 10 — Audit metadata disagrees with used artifacts

**Given** runtime audit metadata reports `runtime_missing` while the artifacts actually used by the nightly are valid and traceable, **when** the result is reviewed, **then** the discrepancy appears as an audit inconsistency. The audit metadata alone does not become a new operational blocker; existing artifact and provenance integrity gates still apply.

### Scenario 11 — Natural production acceptance

**Given** a future implementation is ready for production acceptance, **when** at least two scheduled natural cycles complete, **then** their artifacts and messages demonstrate correct session semantics, truthful no-trade reasoning, primary/discovery isolation, survival of observed context, fail-closed handling of real critical gaps, non-blocking treatment of optional or unsupported gaps, traceable asset reasons, and no automatic execution.

## Functional Requirements

- **FR-001**: Each nightly MUST expose a coherent, reader-visible distinction among primary report grade, discovery coverage, final-review data grade, operational eligibility, and asset decisions. A lower grade or blocked operation MUST have an identifiable supporting reason.
- **FR-002**: The review and message MUST use the semantic value of session-conflict evidence. `market_session_conflict=false` MUST mean no conflict; `true` MUST mean a conflict. The presence of that field alone MUST NOT trigger a warning.
- **FR-003**: For a live, regular, non-conflicting, decision-grade primary report with valid artifact, no blocking reasons, and complete nightly input, the review MUST NOT claim that the main was excluded as a decision basis or instruct the reader to await a decision-grade main solely because no trade was approved.
- **FR-004**: The system MUST preserve `no_trade` when no candidate passes the applicable decision rules, even if the report is decision-grade. The explanation MUST distinguish this outcome from a data or infrastructure failure.
- **FR-005**: The completeness contract MUST classify evidence by scope and by the existing decision rule that consumes it. A datum MUST be required only when an established report gate or an existing applicable asset rule depends on that capability. Primary report grade MUST depend only on its established requirements. Each applicable capability MUST be classified as `REQUIRED_FOR_DECISION`, `REQUIRED_FOR_SPECIFIC_RULE`, `OPTIONAL_CONTEXT`, `DIAGNOSTIC_ONLY`, `NOT_APPLICABLE`, or `UNSUPPORTED`. Missing optional evidence MUST NOT silently become a universal hard blocker.
- **FR-006**: A missing, stale, invalid, or unverifiable capability required by the applicable report gate or asset rule MUST prevent an unsupported eligibility or approval claim at that scope. The stated limitation MUST identify the capability, affected rule or report, and evidence status without automatically downgrading unrelated assets or an otherwise valid primary grade.
- **FR-007**: Absence of optional, unsupported, or unimplemented evidence MUST be identified as such. It MUST NOT be presented as a failed collection attempt or an automatically critical blocker; a real decision dependency on it MUST still be stated.
- **FR-008**: Provider limits or outages, absence from source, incomplete collection, stale evidence, invalid payload, transport loss, and interpretation/presentation errors MUST remain distinguishable in the user-visible or auditable explanation when the available evidence identifies them. Unknown cause MUST remain unknown.
- **FR-009**: Primary-universe completeness and discovery coverage MUST be graded separately. Discovery is best-effort research coverage; this feature sets no minimum percentage, asset count, required ticker set, or coverage SLA. Discovery coverage MUST be visible and auditable, including states equivalent to not applicable, complete, and degraded. Degradation alone MUST NOT downgrade a sufficient primary decision basis, block an otherwise valid primary report, change a primary asset's operational eligibility, or promote a discovery asset.
- **FR-010**: Market context MUST distinguish observed proxies from absent proxies and state partial context when useful observations exist alongside gaps. Missing SPY, QQQ, SMH, BTC, or ETH context MUST be named where expected, without implying that all context is missing or that a proxy gap necessarily blocks the primary report.
- **FR-011**: Every reported asset limitation MUST distinguish a measured negative signal from unavailable evidence. Unverified flow/derivatives, missing earnings data, unavailable premium, or unavailable flow MUST NOT be described as evidence against the thesis or as a failed required provider unless the applicable decision contract makes it required.
- **FR-012**: Asset-level `technical_unvalidated`, `wait`, `avoid`, and `blocked` explanations MUST identify the operative decision or data condition rather than relying only on generic phrases such as incomplete confidence or pending safety checks.
- **FR-013**: A review or message MUST NOT contradict its own grade, gate, session, completeness, or operational-eligibility fields. When source artifacts conflict, it MUST identify the conflict and avoid a stronger approval claim until resolved.
- **FR-014**: The nightly explanation MUST preserve the authoritative main decision and existing integrity restrictions. Presentation MUST NOT approve an asset, recalculate terms, create a trade, or suppress a genuine blocker.
- **FR-015**: A reader MUST be able to trace each material gate or downgraded result to the affected scope, evidence status, and reason, while distinguishing a thesis-based `no_trade` from a report-integrity failure.
- **FR-016**: The primary report's existing decision-grade contract MUST preserve these applicable conditions: live data mode; primary data not stale; regular, non-conflicting primary market session; valid required benchmark session when benchmark validation applies; and execution within the required regular-market window when that gate applies. Discovery coverage MUST NOT enter this minimum. Earnings, guidance, news, macro, sector relative strength, flow/derivatives, and completeness of every fundamental field MUST NOT become universal primary-grade requirements merely because they appear in a report.
- **FR-017**: Primary report grade and final-review operational integrity MUST remain distinct. A decision-grade main MUST still pass existing artifact validity, authoritative identity and provenance, real main blocking-reason, and required source-consistency checks before operational use. This feature MUST NOT remove or relax a genuine integrity gate.
- **FR-018**: For an equity, a datum MUST be required only by an existing applicable rule. Price/history, freshness, technical measurements, and liquidity/volume may be required for a technical eligibility rule that consumes them. Verified earnings timing is required at the **stock trade-approval scope** to establish that the existing earnings-proximity gate is clear, as specified in FR-027; it is not a primary-report requirement. Guidance, news, macro, sector relative strength, post-earnings narrative, analyst commentary, historical PE, PEG, margin trend, free cash flow, revenue growth, and EPS growth MUST NOT universally block equities merely by being absent. Their absence MAY limit context or an existing rule that actually consumes them; no new financial rule is created.
- **FR-019**: For crypto, the review MUST distinguish basic market, technical, liquidity, flow/derivatives, and event/news evidence. Applicable price/history, freshness, technical, and liquidity capabilities MAY be required by existing rules. Funding, open-interest change, CVD, liquidations, Coinbase premium, and news MUST NOT be universal requirements; each is required for a specific rule only when that existing rule actually consumes it.
- **FR-020**: Required capability availability MUST be assessed from valid authorized evidence, with the actual provider provenance preserved. A provider restriction, including Binance HTTP 451 in hosted runners, MUST NOT make basic crypto evidence unavailable when another authorized free source supplies the required capability. If no valid source supplies a genuinely required capability in that run, the affected rule or asset MUST remain limited.
- **FR-021**: Evidence status MUST distinguish available, temporary provider failure, provider restriction, not collected this run, unsupported by the current stack, not implemented, not applicable, and invalid or stale. A never-implemented capability MUST NOT be presented night after night as a transient failure expected to resolve on the next run.
- **FR-022**: Existing observed SPY, QQQ, SMH, BTC, ETH, stock-regime, and crypto-regime facts MUST survive every applicable boundary from main report through analyst-review input, nightly input, and final review. Their source and presence or absence MUST remain traceable. This requirement does not mandate new collection. If BTC or ETH analysis exists in main but the market brief reports it missing, that discrepancy MUST be identified as a possible transport, parsing, or data-contract gap rather than presumed provider absence.
- **FR-023**: When both primary and final-review grades are decision-grade, the main artifact is valid, the operational gate is not blocked, and no primary asset is tradeable, the result MAY remain `no_trade`. Its explanation MUST say that the main is usable but no primary asset satisfies applicable trade requirements. It MUST NOT claim an unusable main, possible session error, or need to wait for decision-grade data without supporting evidence.
- **FR-024**: For every material primary asset decision, the full audit artifact MUST connect source decision, applicable decision rule, capabilities required by that rule, observed and missing or invalid capabilities, and each resulting blocker or non-blocker. The concise message need not reproduce the full trace. The full review MUST distinguish thesis-based, data-limited, and report-integrity-limited `no_trade`.
- **FR-025**: Runtime audit metadata MUST remain observational unless an existing architecture contract explicitly makes it an operational gate. A `runtime_missing` audit result alongside valid, traceable artifacts actually used by the nightly MUST be reported as an inconsistency, not automatically as a new trade blocker.
- **FR-026**: The feature MUST prefer existing free tools and authorized free providers and MUST NOT require a new paid dependency. A desirable capability that the current free stack cannot reliably supply MUST be honestly classified as unsupported, not implemented, or optional context as applicable; it MUST NOT become a permanent impossible blocker unless an existing rule truly requires it.
- **FR-027**: A stock MUST NOT be approved as `tradeable` unless its earnings timing is verified sufficiently to apply the existing near/imminent earnings-risk rule. When timing cannot be verified, the asset-level event gate remains unresolved and the explanation MUST say so; the asset MAY retain a supported watch, wait, or technical-unvalidated state. This MUST NOT downgrade `primary_report_grade` or unrelated assets. Earnings evidence status MUST distinguish verified, source unavailable, not collected, not implemented, and not applicable. Absence of a known date MUST NOT be represented as proof that no earnings event is near.
- **FR-028**: The existing minimum backtest sample gate MUST remain effective: a sample below 30 or an absent backtest MUST NOT support full trade eligibility. When an applicable decision rule consumes `win_rate_2r` or `expected_value_r`, those statistics MUST be valid and traceable to the backtest sample before that rule can be treated as passed. `avg_win_r` and `avg_loss_r` are diagnostic decomposition fields for the current decision contract; if the consumed sample, win rate, and expected value are valid, absence of either decomposition field alone MUST NOT reduce confidence or eligibility. Invalid or internally contradictory required statistics remain integrity failures at the affected rule or asset scope.
- **FR-029**: Using multiple providers MUST NOT by itself reduce an asset's confidence or eligibility. Relevant capabilities from free providers MAY be combined only when asset and instrument identity, units, semantics, timeframes, timestamps/freshness, and per-capability provenance satisfy the consuming rules, and no unresolved conflicting observation is used. A limitation MUST identify a demonstrated incompatibility or integrity gap; a generic `mixed_provider_data` label or provider brand alone is diagnostic only. A genuine conflict in identity, venue, currency normalization, units, directly compared windows, intraday/EOD timing, freshness, same-capability values, provenance, or metric semantics MUST continue to limit the affected decision until resolved.

## Edge cases

- A missing diagnostic field is unknown; it is neither an observed conflict nor proof of a clean session.
- Contradictory grade or gate fields are disclosed as an integrity conflict; no unsupported approval is inferred.
- One failed discovery candidate does not redefine completeness of the primary universe.
- A provider's known limitation is labeled separately from a transient collection failure and from a feature that has never been implemented.
- A proxy can be present with only some measurements; available values remain visible and absent values are not fabricated.
- Several independent reasons may apply to an asset; the explanation identifies their separate effects without collapsing them into one generic blocker.
- One provider's failure with valid equivalent capability from another authorized source does not erase the capability; provenance identifies the source actually used.
- A required capability with no valid source limits only the applicable rule or asset, except where an established report-wide gate itself requires it.
- An upstream observation absent only downstream is a transport or interpretation concern, while a genuinely absent upstream observation remains unavailable.
- An earnings source returning no known upcoming date does not by itself verify that the existing stock event-risk window is clear; its status and covered horizon must support that conclusion.
- A backtest with all nonnegative outcomes may legitimately lack an average loss; one with all nonpositive outcomes may lack an average win. Valid expected value is not downgraded solely for that absent decomposition.
- Yahoo price history and FMP fundamentals may be combined when each capability meets its own identity, semantic, freshness, and provenance contract; a concrete mismatch still limits the affected rule.

## Constraints, assumptions and dependencies

- This is a new upstream data-contract and decision-integrity feature. The prior Telegram Analyst Review V2 presentation design is not being reopened.
- Main decisions and existing integrity restrictions remain authoritative. The feature seeks truthful grading and explanations, not a less conservative trading policy.
- Primary and discovery are distinct universes in current reports. Discovery is best-effort research coverage and has no fixed target in this feature.
- The named production fields and two nightlies are accepted as observed evidence. This specification does not claim that all possible data failures occurred in those runs.
- Evidence criticality is derived from established report gates and existing applicable asset rules; this feature does not invent new financial requirements. A field's presence in a report does not make it universally required.
- The existing free provider stack is preferred. No paid feed, provider brand, or new trading universe is assumed.
- Final-review artifact, identity, provenance, blocking-reason, and source-consistency gates remain dependencies for operational use of a decision-grade main.
- All execution remains manual; no orders or Telegram sends are part of this specification phase.

## Clarification record — current rule and evidence contract (2026-10-10)

The repository audit for baseline `10d4ea364b4860fd1e3162dc3da2ab996a0a4184` is recorded in [the evidence matrix](clarifications/evidence-matrix.md). It documents current effects separately from provisional, rule-specific criticality. The historical post-Phase 2 audit is comparative evidence, not authority for today's eligibility rules. No new financial rule or provider requirement is established by this clarification.

- The primary main grade currently depends on live mode, fresh primary assets, a regular and non-conflicting primary session, valid supplied benchmark sessions, and the regular-market time window when enforced. Discovery can degrade the overall grade, but cannot by itself downgrade the primary grade. The later final-review integrity gate separately checks artifact validity, main identity/provenance, real blocking reasons, session, freshness, and provider status.
- The current asset classifier directly consumes price history, freshness, technical indicators, measured liquidity and market cap, risk-plan feasibility, regime, event proximity, and backtest statistics. Individual fundamentals change investment score when present; observed high funding and open-interest change change crypto swing score. Absence of many other fields limits current eligibility only through generic missing-data matchers. This implementation effect does not make those fields `REQUIRED_FOR_DECISION` under FR-005.
- `CURRENT_IMPLEMENTATION_MISMATCH`: Generic `missing_*`, `insufficient_*`, and `*_unavailable` matching can cap an asset even where no applicable financial rule explicitly needs the missing capability. This includes optional crypto flow, not-implemented liquidations, and some equity fundamental fields. The `mixed_provider_data` cap is applied from provider choice without checking actual semantic or timing incompatibility. Missing crypto funding is also converted to zero in regime derivation, which can be interpreted as balanced funding.
- The three asset-level policy decisions are settled: verified earnings timing is required for stock trade approval; the existing minimum backtest sample and valid consumed win-rate/EV evidence remain required at their applicable scope while unused win/loss averages are diagnostic; and provider mixing is acceptable when the actual capabilities are compatible and traceable. These policies do not add a primary-report gate, a paid provider, or a new trading rule.
- `CURRENT_IMPLEMENTATION_MISMATCH`: `ev_components_missing` currently caps confidence and marks otherwise valid EV as limited solely because an unused average-win or average-loss decomposition is absent. The backtest computes EV directly from all R-multiples, so this cap contradicts FR-028 when consumed statistics are valid.
- `CURRENT_IMPLEMENTATION_MISMATCH`: With an adequate sample, missing `win_rate_2r` or `expected_value_r` can currently skip the corresponding rule comparison without proving that rule passed. FR-028 requires valid, traceable consumed statistics for an applicable backtest rule.
- `CURRENT_IMPLEMENTATION_MISMATCH`: `mixed_provider_data` currently caps confidence and eligibility from stock price-provider choice alone. `source_mismatch_possible` is a warning of possible mismatch, not evidence that any FR-029 incompatibility occurred.
- `CURRENT_IMPLEMENTATION_MISMATCH`: The market brief expects `Last price` or `Daily change` in asset metric text, but scoring does not emit those labels; benchmark SPY/QQQ/SMH observations are used for regime and provenance without becoming market-brief assets. Existing upstream observations can therefore appear missing downstream.
- `CURRENT_IMPLEMENTATION_MISMATCH`: The Telegram session reason searches for the `market_session_conflict` field name, including `false`, and the full review has generic advice to wait for a decision-grade main on a `no_trade` path even when the main is already decision-grade.
- Runtime audit intake is separate and observational. The code proves that `runtime_missing` means the selected downloaded artifact root lacked an allowlisted runtime bundle at intake. The cause for the cited production runs cannot be reduced further without their selected artifact directory listings and runtime manifest/producer logs; valid main/close report artifacts alone do not prove that their runtime bundles were uploaded.

## Key entities

- **Evidence capability**: Market, technical, liquidity, flow, event, or fundamental evidence classified by scope and existing consuming rule as required for a decision, required for a specific rule, optional context, diagnostic only, not applicable, or unsupported.
- **Evidence status**: Available, temporary provider failure, provider restriction, not collected this run, unsupported, not implemented, not applicable, or invalid/stale, with source provenance when available.
- **Primary universe**: Assets whose coverage and decisions determine primary report eligibility.
- **Discovery universe**: Candidates evaluated for research or observation whose coverage is reported separately.
- **Report gate**: The primary report-grade outcome under its established requirements, distinct from final-review operational integrity checks, each with supporting reasons.
- **Asset decision**: The authoritative classification and the thesis, evidence, and limitations that explain it.
- **Nightly explanation**: The full review and reader-facing summary that state grades, decisions, gaps, and next conditions without contradiction.

## Non-goals

- Redesigning the previous Telegram brief's layout, length, ranking, or wording beyond correcting false or contradictory claims.
- Relaxing trade approval, changing execution to automatic, or treating missing data as positive market evidence.
- Mandating specific new providers, paid subscriptions, or new asset-universe members without an explicit evidence contract.
- BNB, ZEC, or Lighter expansion; a new scoring model or trading strategy; lower risk thresholds merely to generate trades.
- Paid-provider migration, broker integration, automatic execution, canonical evidence redesign, or maturation redesign.
- A fixed discovery coverage target or collection of new market facts solely to satisfy market-context presentation.

## Success Criteria

- **SC-001**: In all acceptance scenarios, 100% of session-conflict explanations agree with the semantic conflict value; zero clean regular-session examples carry a false session warning.
- **SC-002**: In decision-grade `no_trade` examples, 100% of review and message explanations acknowledge the usable main and identify the actual no-trade condition; zero instruct the reader to await an already available decision-grade main.
- **SC-003**: In acceptance examples with missing evidence, 100% of material gaps identify the affected scope and distinguish required, optional, discovery-only, or unknown applicability; no absent measurement is presented as measured.
- **SC-004**: In acceptance examples with adequate primary evidence and degraded discovery, 100% retain the primary grade and eligibility supported by primary evidence while visibly reporting discovery degradation.
- **SC-005**: For every acceptance example, a reader can determine from the review whether the outcome is thesis-based, data-limited, or report-integrity-limited, and can identify the next condition required for reconsideration without a contradictory instruction.
- **SC-006**: In applicable acceptance examples, 100% of valid upstream observations for SPY, QQQ, SMH, BTC, ETH, stock regime, and crypto regime remain available or are explicitly identified as lost at a later boundary; zero downstream absences are misattributed to a provider without evidence.
- **SC-007**: In asset acceptance examples, 100% of material primary decisions have an inspectable rule-and-capability trace; zero optional, unsupported, or discovery-only gaps become unexplained global blockers, while every genuinely missing required capability limits its applicable scope.
- **SC-008**: Final production acceptance includes at least two scheduled natural cycles whose artifacts and messages show no false session conflict or decision-grade wait instruction, separate primary and discovery outcomes, preserved available market observations, fail-closed real critical gaps, truthful optional and unsupported statuses, traceable asset reasons, and no automatic execution.
