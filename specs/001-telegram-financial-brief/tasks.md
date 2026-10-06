# Tasks: Telegram Analyst Review V2 — Decision-Useful Financial Brief

**Inputs**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/telegram-message.md`  
**Execution rule**: RED → minimal GREEN → focused regression → bounded product review → one commit → stop. This file defines future work; no task was executed while generating it.

## Scope and authority gates

- Approval remains `source_decision` plus the existing final-review integrity gate. Presentation must receive the already confirmed tradeables; it must not infer approval from legacy labels, close or discovery.
- No new scoring, financial ranking, rescore, risk logic, data source/provider call, broker action, dependency, workflow edit or multi-message delivery.
- Do not modify `.github/workflows/financial-advisor-reports.yml`, `.github/workflows/financial-advisor-nightly-review.yml`, `scripts/fetch-latest-github-reports.ps1`, `advisor/report.py`, `advisor/live_loader.py`, `advisor/evidence_archive.py`, `advisor/evidence_collector.py`, `advisor/evidence_packager.py`, canonical evidence/schema, maturation, provider assignment, predictive statistics, scoring, risk engine or BNB/ZEC/Lighter scope. Stop and report a conflict if a task appears to require one of these.
- Use the current 3500-character project cap for the final wrapped payload. A normal 1500–3000-character result is a UX target, not a reason to remove critical meaning. Never slice the message or a critical field by character position.

## Phase 1 — RED contract tests

Write tests only in this phase. Reuse existing fixtures where possible. Each new assertion must fail against the current implementation for the intended reason; distinguish unrelated fixture or environment failures. No production edit until T011 completes.

- [X] T001 [US1] Add a no-trade candidate test in `tests/test_analyst_review_semantics.py` that requires the final wrapped brief to state approval status and main reason, available market context, asset thesis/confirmation/risk or invalidation/missing checks, practical call and manual execution; done when a ticker/label list alone fails the assertion.
- [X] T002 [US5] Add a partial-market test in `tests/test_analyst_review_semantics.py` with usable BTC/ETH and absent SPY/QQQ/SMH; require `partial`, BTC/ETH facts and all three missing proxy names, without changing report grade or final decision; done when the old `Market brief: missing` presentation fails.
- [X] T003 [US2] Add a crypto-status test in `tests/test_analyst_review_semantics.py` for `basic_data_status=live` and `flow_data_status=not_verified`; require separate human explanations and no tradeable promotion; done when a generic flow blocker or raw labels alone fails.
- [X] T004 [US4] Add confirmed-main tradeable tests in `tests/test_analyst_review_semantics.py` with distinct exact entry, combined stop/invalidation and sizing strings plus a missing-term variant; require exact present values, explicit or nonmisleading absence, source authority and no invented confidence; done when the old summary cannot satisfy both variants.
- [X] T005 [US4] Add a **final wrapped payload** contract test in `tests/test_telegram_notify.py` using the same construction executed before the HTTP call, with `send_json` capture where useful; require exact entry, stop/invalidation and sizing strings after safety wording and forbidden-language handling, while prohibited imperative text outside critical terms remains sanitized; done when summary-only preservation is insufficient.
- [X] T006 [US3] Add a rejected/blocked reason test in `tests/test_analyst_review_semantics.py` with an explicit operational blocker plus a descriptive reason; require blocker first, at most one optional second recorded reason, and no invented cause; done when the old generic discard summary fails.
- [X] T007 [US1] Add a no-authoritative-leader test in `tests/test_analyst_review_semantics.py`; require “Prioridades de observação” or equivalent and reject “Melhor equity”, “Melhor crypto” or “Melhor oportunidade” as financial-rank claims; done when configured order alone cannot select a winner.
- [X] T008 [US6] Add a possible-session-detection test in `tests/test_analyst_review_semantics.py`; require human wording that preserves uncertainty and the not-decision-grade consequence, while rejecting “bug confirmado”; done when only the internal code is visible.
- [X] T009 [US7] Add bounded high-volume tests in `tests/test_analyst_review_semantics.py` and `tests/test_telegram_notify.py` for one final wrapped payload at or below 3500 characters, all confirmed tradeable tickers, intact retained entry/stop/sizing values, decision, material uncertainty, call and manual-execution note; require an explicit full-report reference if complete terms cannot fit and an explicit sender failure for a still-over-limit payload; done when blind slicing is detected.
- [X] T010 [US4] Add non-promotion cases in `tests/test_analyst_review_semantics.py` for watch, research, blocked and rejected source states; require no approved-entry language for any of them while a genuinely confirmed tradeable remains approved; done when the test depends on the main source decision and existing gate, not a legacy label.
- [X] T011 Run the new focused cases with `.\.venv\Scripts\python.exe -m pytest -q tests/test_analyst_review_semantics.py tests/test_telegram_notify.py` and record the expected RED failures before changing `advisor/analyst_review.py` or `advisor/telegram_notify.py`; done when each failing contract points to current presentation/transport behavior rather than a broken fixture. If the venv is absent, use the project-equivalent interpreter.

## Phase 2 — Minimal GREEN: final-review content

Complete T011 first. Keep helpers small and local to `advisor/analyst_review.py`; do not create a new class, service, template engine or financial decision path. Preserve raw audit labels and the current confirmation predicate.

- [X] T012 [US4] Pass the already computed confirmed tradeable assets or tickers from the existing gate into `## Telegram summary` composition in `advisor/analyst_review.py`; done when only that set can receive approved/tradeable prose and all confirmed tickers remain visible.
- [X] T013 [US1] Replace raw user-facing watch/research/blocked/rejected/tradeable labels with meaning-preserving human status text in `advisor/analyst_review.py`, leaving full-review audit fields intact; done when T001/T007 wording is human without altering `source_decision` or creating a rank.
- [X] T014 [US5] Change market presentation completeness and render observed proxy/regime facts in `advisor/analyst_review.py` as `complete`, `partial` or `unavailable`; show BTC/ETH when usable and name absent SPY/QQQ/SMH, without touching report grade, readiness or integrity logic; done when T002 passes and no absent price/change is fabricated.
- [X] T015 [US1] Render bounded equity observation cards in `advisor/analyst_review.py` from existing ticker, status, metrics, thesis, confirmation, invalidation, risk, valuation, events/news and path fields; omit or humanize missing/sentinel values; done when T001 passes without listing the whole universe.
- [X] T016 [US2] Render bounded crypto observation cards in `advisor/analyst_review.py` from existing metrics, thesis, confirmation, risk and path fields, explicitly separating basic data from flow/derivatives and showing funding/OI/premium only when present; done when T003 passes without inferring a positive or negative flow conclusion.
- [X] T017 [US3] Render concise rejected/blocked reasons and a practical final call in `advisor/analyst_review.py`; prefer explicit operational blocker, else first structured recorded reason, and list priorities when no single authoritative leader exists; include uncertain session warning when present; done when T006/T007/T008 pass without invented causality or ranking.
- [X] T018 [US7] Add deterministic complete-field/section reduction in `advisor/analyst_review.py` in this order: secondary card fields, observation detail/count, rejected/blocked detail, secondary market context; reserve the final wrapper size, preserve decision, uncertainty, every confirmed ticker, intact retained trade terms, call and manual-execution note, and refer to the full report for omitted critical terms; done when the content-side T009 case passes without blind truncation.

## Phase 3 — Minimal GREEN: Telegram transport

Complete the content path first. Keep `## Telegram summary`, missing-secrets behavior, token handling and HTTP payload shape unchanged. The sender must not become a second financial summarizer.

- [X] T019 [US4] Make `build_analyst_final_telegram_message` in `advisor/telegram_notify.py` human-first and preserve exact operational-value substrings through the safety wrapper/forbidden-language handling; retain a short no-order/manual-execution line and section extraction; done when T005 passes on the final wrapped payload and existing token/transport safety assertions still hold.
- [X] T020 [US7] Replace `message[:TELEGRAM_MAX_CHARS]` in `advisor/telegram_notify.py` with an explicit final-payload length guard at 3500 characters, failing before HTTP send when composition is still too long; done when T009 proves no mid-field cut, an over-limit payload is rejected and one in-limit payload retains the existing `sendMessage` shape.

## Phase 4 — Focused GREEN regression

- [X] T021 Update only Telegram-facing assertions superseded by V2 wording in `tests/test_analyst_review_semantics.py` and `tests/test_telegram_notify.py` (including old `Top`/`Melhor`, raw labels, `Market brief: missing` and prefix assertions); retain full-review audit, source-authority, no-trade, secret and extraction checks; done when no old assertion forces the rejected UX back into production.
- [X] T022 Run `.\.venv\Scripts\python.exe -m pytest -q tests/test_analyst_review_semantics.py tests/test_telegram_notify.py tests/test_report_budget_and_analyst_input.py`; done when all three modules pass, or stop and report the first concrete failure. If the venv is absent, use the project-equivalent interpreter. Do not run the full suite by default; if another test file must change, stop and explain the direct contract dependency before expanding scope.

## Phase 5 — Local product sample validation

- [X] T023 Generate and inspect three **local, unsent final wrapped payloads** from tests/fixtures using `advisor/analyst_review.py` and `advisor/telegram_notify.py`: no-trade, confirmed-tradeable and high-volume; record `len(payload)` for each and check phone readability, human decision, market facts/gaps, rationale, risks, call, intact terms, manual execution and `<=3500`; done when all three samples are reviewable and no Telegram HTTP request occurs.

## Phase 6 — Small documentation adjustment

- [X] T024 Update only the relevant Telegram paragraph in `docs/AUTOMATION_SETUP.md` to state that the nightly job still sends only `## Telegram summary`, now as one bounded, decision-useful brief with manual execution and no automatic order; done when setup/secrets instructions and workflow description outside that paragraph remain unchanged.

## Phase 7 — Bounded review

- [X] T025 Review only the feature diff in `advisor/analyst_review.py`, `advisor/telegram_notify.py`, `tests/test_analyst_review_semantics.py`, `tests/test_telegram_notify.py` and `docs/AUTOMATION_SETUP.md`; classify findings `BLOCKER` or `NON_BLOCKER`, verify no scoring/risk/approval/provider/workflow/dependency change, no blind truncation, no critical-term mutation or label promotion; done when no blocker remains after at most one bounded correction-and-focused-retest pass.

## Phase 8 — Commit gate and stop

- [X] T026 After RED evidence, focused GREEN, three reviewed samples and blocker-free bounded review, verify the branch/base and exact diff in `advisor/analyst_review.py`, `advisor/telegram_notify.py`, `tests/test_analyst_review_semantics.py`, `tests/test_telegram_notify.py`, `docs/AUTOMATION_SETUP.md`, `specs/001-telegram-financial-brief/` and `.specify/feature.json`, then create one local commit `feat: make analyst telegram review decision useful`; done when the commit contains only reviewed feature files and its SHA is recorded. Stop immediately afterward: no push, workflow run or real Telegram send without a later explicit instruction.

## Dependencies and independent acceptance

- **Hard gate**: T001–T010 create contracts; T011 proves RED; no production task T012–T020 may start before T011.
- **Content sequence**: T012 establishes authority before T013–T018 render it. T014–T017 provide content before T018 reduces it. T019–T020 follow the content contract so the final wrapped payload can be tested.
- **Validation sequence**: T021 updates only intentionally obsolete wording assertions; T022 is the focused regression gate; T023 validates the product examples; T024 documents the behavior; T025 reviews the bounded diff; T026 commits and stops.
- **Story acceptance**: US1 passes with a reasoned no-trade brief and no invented leader; US2 distinguishes crypto basic data and flow; US3 explains blocked/rejected reasons; US4 preserves source authority and exact trade terms through the final wrapped payload; US5 presents partial market facts and gaps; US6 retains session-warning uncertainty; US7 delivers one complete bounded payload without mid-field truncation.
- **MVP boundary**: US1 + US5 establish the useful no-trade and partial-market experience, but the release gate requires US2–US7 and the final transport guard because financial meaning and delivery integrity are cross-cutting.
- **Parallel opportunities**: None marked `[P]`. Most RED work edits one test module, most GREEN work edits one content module, and the authority/size gates are sequential. Parallelizing these files would make the RED/GREEN evidence and diff harder to verify.
