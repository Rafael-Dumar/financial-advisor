# Feature Specification: Telegram Analyst Review V2 — Decision-Useful Financial Brief

**Feature directory**: `specs/001-telegram-financial-brief`  
**Status**: Draft for review  
**Created**: 2026-10-06

## Problem statement and user value

The nightly Telegram message currently exposes mostly internal states, tickers and generic blockers. It does not give the reader enough market context or asset-specific reasons to understand the operational decision. The full final review already contains richer material; the phone message needs a compact, faithful synthesis of that material.

After opening one message, the reader should understand whether an entry is approved now, what market evidence is available, which assets merit observation and why, what could confirm or invalidate each case, what remains unverified, why relevant assets were rejected or blocked, and what would have to change before an operation becomes possible.

## Clarifications

### Session 2026-10-06

- Market context remains partial whenever at least one useful, observed proxy or verified regime is available; one missing proxy cannot make all context unavailable. The current `missing` result is explained by a proxy-universe gap and an all-equity-proxy completeness rule, not by evidence that BTC/ETH were absent.
- Main prints entry, a combined stop/invalidation value, sizing, thesis and confidence, but the current final-review boundary can lack the operational values and does not carry an authoritative confidence field into the asset review. Missing fields are identified, never reconstructed.
- All confirmed tradeable tickers remain visible. Condense observation, rejection and secondary context first; never cut a critical term mid-value. If terms cannot fit, point explicitly to the full report for omitted details in one message.
- For rejected/blocked assets, prefer an explicit operational blocker; otherwise use the first authoritative structured reason already available. A second reason may be added if useful and space permits, without inventing causality.
- Observation priority is review order only. Without an authoritative single highest priority, list observation priorities without calling one the best opportunity.
- Human-readable statuses preserve the meaning of internal states; internal labels may remain in the full review for audit.
- The 1,500–3,000 character range is a normal UX target. Critical-value integrity and faithful decision meaning take precedence; exceptional longer messages are acceptable within the delivery limit, without adding multiple-message delivery.

## User Scenarios & Testing

### Scenario 1 — No trade with candidates

**Given** the main-derived operational decision is `no_trade` and the review has observable candidates with supporting information, **when** the nightly message is prepared, **then** it starts by saying that no entry is approved and briefly why; describes the available market context; presents a bounded set of priority assets with thesis, confirmation, risk or invalidation, and missing checks where known; and ends with a practical call naming supported observation priorities, their reason, the condition for reconsideration, and the principal risk. It names one highest priority only if the existing review authoritatively identifies one. Observation is never phrased as permission to buy.

### Scenario 2 — Crypto basic data present, flow absent

**Given** a crypto asset has basic data marked available and flow or derivatives marked `not_verified`, **when** it appears in the message, **then** the reader sees that basic data is available and flow or derivatives remain unverified. Missing flow alone is not described as missing basic data or as proof for or against the asset. Its existing operational state is preserved.

### Scenario 3 — Rejected and blocked assets

**Given** the review contains relevant rejected or blocked assets with recorded reasons, **when** the message is prepared, **then** a short avoid/blocked section names the most relevant of them and gives each available reason. No rejected or blocked asset is presented as an approved entry.

### Scenario 4 — Tradeable from main

**Given** the authoritative main decision and existing integrity checks confirm a tradeable asset, **when** it is shown, **then** the message gives that asset first priority and reproduces available entry, combined stop/invalidation, and sizing values exactly as supplied by main, with its source decision and supported rationale. It neither calculates missing values nor sends an order. If an operational field is absent from the final-review input, its absence is explicit; the message does not fill it in. No separate narrative invalidation or confidence claim is required.

### Scenario 5 — Partial market context

**Given** some market proxies or verified regimes are available and others are absent, **when** the market section is prepared, **then** it uses the available SPY, QQQ, SMH, BTC and ETH context and existing price or daily change values where present, identifies the missing proxies by name, and calls the context partial. One missing proxy cannot make the whole brief unavailable. If no useful observed context exists, it says context is unavailable without implying a market direction.

### Scenario 6 — Possible session detection problem

**Given** the review reports `possible_session_detection_bug`, **when** the message is prepared, **then** it explains in ordinary language that market-session classification may be wrong and that the report was not treated as decision-grade for safety. It retains uncertainty and does not claim a confirmed bug.

### Scenario 7 — Too much information

**Given** more detail than fits a phone message, **when** the message is prepared, **then** all confirmed tradeable tickers remain visible before observation candidates; normally no more than three equities and three crypto assets receive detail; and observation, rejected/blocked and secondary market detail are shortened in that order. The decision, material uncertainty, practical call and manual-execution note survive shortening. Critical entry, stop/invalidation and sizing values are never cut mid-value. If they cannot all fit, the message explicitly directs the reader to the full report for omitted detail. One delivered message stays within the supported Telegram size limit.

## Functional Requirements

- **FR-001**: The message MUST open with a human-readable answer to whether any entry is approved now, followed by the principal available reason. Internal flags and diagnostic codes MUST NOT be its lead content.
- **FR-002**: The message MUST derive operational approval exclusively from the existing `source_decision` produced by main and the final review's existing integrity gate. Presentation MUST NOT promote watch, research, rejected or blocked states to tradeable, or reinterpret a close/discovery observation as approval.
- **FR-003**: The market section MUST distinguish complete, partial and unavailable context. It MUST show available SPY, QQQ, SMH, BTC, ETH, stock regime and crypto regime evidence, including last price and daily change only when present, and identify missing proxies precisely. If at least one useful observed proxy or verified regime exists but expected context is absent, status MUST be partial, not unavailable. Unavailable is reserved for no useful observed context. The message MUST NOT infer unavailable market direction or fabricate numbers.
- **FR-004**: The message MUST present equities and crypto separately, using language such as "em observação" or "prioridades de revisão" when no authoritative financial ranking exists. It MUST NOT portray configured ticker order or label priority as a quantitative rank or as the best trades.
- **FR-005**: For each detailed equity, the message MUST identify the ticker and human-readable status and, when present and useful, summarize price, daily change, relative strength, thesis, confirming evidence, contradiction or invalidation, material risks, valuation, earnings/guidance/news, and the path to operation. Empty, redundant or unverified fields MUST be omitted or explained plainly.
- **FR-006**: For each detailed crypto asset, the message MUST distinguish the status of basic data from flow/derivatives and, when present and useful, summarize price, daily change, relative strength, funding, open interest, premium, flow, thesis, confirmation, material risks, and path to operation. An absent measurement MUST NOT become a positive or negative conclusion.
- **FR-007**: In normal cases, no more than three equities and three crypto assets MUST receive detailed treatment. All confirmed tradeable tickers MUST remain visible and take first priority. The message MUST give relevant rejected/blocked assets a short reasoned summary without detailing the full universe. For multiple recorded reasons, it MUST prefer an explicit blocker directly tied to operational impossibility; otherwise it MUST use the first authoritative structured reason already available. It MAY add one other supported reason if space permits, without inventing causality.
- **FR-008**: Every message MUST end with a practical call. For `no_trade`, it MUST state that no entry is approved and, when supported by the review, identify existing observation priorities, why they matter, what needs to happen before an operation, and the principal risk or invalidation. It MUST identify one highest priority only when the existing review provides an authoritative basis for that distinction; otherwise it MUST list priorities without a "best opportunity" claim. If the review supports no priority, the call MUST say so.
- **FR-009**: For a confirmed tradeable, the message MAY display available entry, combined stop/invalidation, sizing, source decision and supported existing rationale. Any displayed numeric or operational value MUST match the main-provided value exactly. Absent terms MUST be identified rather than inferred. The message MUST NOT require a separate narrative invalidation or confidence value, or rescore, recalculate, resize, or execute.
- **FR-010**: User-facing prose MUST translate internal labels and diagnostics into ordinary language while preserving their meaning and uncertainty. At minimum, watch with pending checks means "Em observação"; research-only equity or crypto means "Research"; crypto context watch means "Contexto/observação"; blocked means "Bloqueado"; rejected means "Rejeitado"; and a confirmed tradeable means "Trade candidate confirmado pelo main" or equivalent wording. These are presentation states, not changes in decision. Internal labels MAY remain in the full review for audit. `possible_session_detection_bug` MUST remain a possibility, not a confirmed diagnosis.
- **FR-011**: The normal message SHOULD target approximately 1,500–3,000 characters as a UX goal, not an absolute bound, and MUST remain within Telegram's supported limit. When shortening is needed, it MUST remove observation detail first, then rejected/blocked detail, then secondary market context, while preserving the operational decision, all confirmed tradeable tickers, material uncertainty, practical call and safety note. It MUST NOT truncate entry, stop/invalidation or sizing mid-value or omit context in a way that changes the decision's meaning. If critical detail still cannot fit, it MUST explicitly point to the full report for omitted terms. Exceptional messages MAY exceed 3,000 characters within the delivery limit; this feature does not require multiple-message delivery.
- **FR-012**: The message MUST state that execution is manual and the bot sends no orders. It MUST NOT trigger broker execution, automatic orders, buying or selling.
- **FR-013**: Every factual market or asset statement MUST be supported by information already available to the final review; missing, stale or unverified information MUST be identified as such when material to the conclusion.

## Edge cases

- No candidate or tradeable: state no approved entry, explain the available reason, and avoid inventing a leading asset.
- No usable market proxies: state that market context is unavailable and name material missing context without inferring a regime.
- One proxy with only price or only daily change: show the available value and identify the other as absent only if needed for interpretation.
- More confirmed tradeables than the normal asset-detail budget: retain every approved ticker; shorten observation, rejected/blocked and secondary market detail in that order. Preserve complete critical main-provided terms when they fit; otherwise point explicitly to the full report for omitted details. The single-message delivery limit still applies.
- Contradictory or missing operational fields: preserve the authoritative decision, identify the missing or conflicting presentation evidence, and do not reconstruct values.
- A candidate with no meaningful thesis, confirmation or invalidation: present only supported facts and missing checks; do not manufacture a narrative.
- Unicode, line breaks and long source text: preserve readability on a phone and retain complete critical values and safety wording within the delivery limit.

## Constraints, assumptions and dependencies

- `source_decision` and the current final-review integrity gate remain authoritative. The Telegram message is presentation and synthesis only.
- Manual execution only; no silent inference, fabricated data or status promotion.
- The existing final review and its nightly input are the information boundary for this feature. Existing report fields may be surfaced; acquisition of new data is outside scope.
- "Priority" means attention for review under existing states, not a newly computed return, quality or trade score.
- The normal length target is a presentation goal; preserving exact approved terms and truthful uncertainty takes precedence over filling the target range.
- The standard main report prints entry, combined stop/invalidation and sizing, but the final review can contain an explicit missing-field sentinel and its confirmation gate does not depend on those presentation fields. Main also prints thesis and a confidence score; an exact main rationale may be absent from final-review presentation, and confidence is not part of the current final-review asset contract.

## Key entities

- **Operational decision**: The main-derived approval state after existing final-review integrity checks.
- **Market context**: Available proxy observations, regimes, their values, and explicit gaps.
- **Asset review**: An asset's authoritative source decision, descriptive review state, existing measurements, thesis, confirmation, invalidation, risks and missing checks.
- **Practical call**: The reader-facing conclusion and conditions for reconsideration, without creating a new decision.

## Non-goals

No new scoring or final-review rescore; no risk-engine change; no canonical evidence archive, maturation or canonical-schema change; no provider-assignment change or new providers; no BNB, ZEC or Lighter expansion; no broker integration, automatic execution or new paid data sources. This feature does not repair market-data collection or diagnose session detection.

## Success Criteria

- **SC-001**: In all seven acceptance scenarios, a reader can identify the current approval state and the practical next condition from a single message without opening the full report.
- **SC-002**: In the no-trade candidate scenario, the message includes at least one supported asset-specific reason, confirmation, risk/invalidation and missing check, plus a practical call; when any item is absent upstream, the message identifies that gap without invention.
- **SC-003**: In every partial-context scenario, all available proxy observations used by the message are accurate and every expected missing proxy is named; no absent value is displayed as measured.
- **SC-004**: Across acceptance scenarios, zero watch/research/rejected/blocked assets are described as approved trades, and every displayed tradeable entry, stop/invalidation and sizing value matches main exactly.
- **SC-005**: Normal messages detail at most three equities and three crypto assets and target about 1,500–3,000 characters; 100% of delivered messages fit the supported Telegram limit while retaining every confirmed tradeable ticker, the decision, material uncertainty, practical call and manual-execution note. No critical field is truncated mid-value; exceptional messages may exceed the target range within the delivery limit.
- **SC-006**: A reader reviewing the seven scenarios can distinguish attention priority from financial ranking and basic crypto data from unverified flow without relying on internal codes; no single "best opportunity" is asserted solely from configured order or legacy label priority.

## Clarified data interpretation and remaining verification

- **Market brief diagnosis**: In the inspected workflow, SPY, QQQ and SMH are collected as benchmarks for regime/scoring context, but they are not in the configured primary asset universe and do not appear as asset observations in the nightly input inspected. BTC and ETH do appear. The current brief completeness rule reports `missing` whenever any of SPY/QQQ/SMH is absent, so it masks useful crypto context. This is an input/representation gap plus an overly strict presentation completeness rule; no evidence in this bounded inspection establishes a runtime provider failure. This feature changes only how available facts and gaps are presented.
- **Confirmed-tradeable field contract**: `source_decision` is required and available for a confirmed tradeable. The standard main report prints entry, combined stop/invalidation and position sizing, but each is possibly absent at the final-review presentation boundary, where missing values already have an explicit sentinel. Exact main rationale/thesis is possibly absent at that boundary; the final review has a descriptive thesis that is not itself a new decision. A confidence score exists in the main report but is not part of the current final-review asset presentation contract, so Telegram does not need to show it.

  | Field at the final-review boundary | Classification | Interpretation |
  | --- | --- | --- |
  | `source_decision` | `REQUIRED_AND_AVAILABLE` | `tradeable` confirmation depends on this main-derived state. |
  | Entry | `POSSIBLY_ABSENT` | Standard main output includes it; the final review permits a missing sentinel. |
  | Combined stop/invalidation | `POSSIBLY_ABSENT` | Standard main output includes one combined value; no separate narrative invalidation is guaranteed. |
  | Position sizing | `POSSIBLY_ABSENT` | Standard main output includes it; the final review permits a missing sentinel. |
  | Exact main rationale/thesis | `POSSIBLY_ABSENT` | A descriptive final-review thesis is available but is not necessarily the main's original wording. |
  | Authoritative confidence for Telegram | `NOT_PART_OF_CURRENT_CONTRACT` | Main reports a score, but the current final-review asset presentation does not carry it. |

- **Remaining verification**: During planning, confirm the exact delivery-size limit and which complete tradeable details can fit in one message in representative high-volume cases. These do not change the behavior specified above.
