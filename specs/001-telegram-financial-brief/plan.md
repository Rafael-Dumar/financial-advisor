# Implementation Plan: Telegram Analyst Review V2 — Decision-Useful Financial Brief

**Specification**: `specs/001-telegram-financial-brief/spec.md`  
**Planning date**: 2026-10-06  
**Branch**: current checkout; no branch change planned in this phase  
**Scope**: technical design only; no production or test code changed

## Summary

Replace the terse `## Telegram summary` with a deterministic, phone-readable financial brief assembled from the existing final-review package. Keep approval exclusively in the current `source_decision` plus integrity gate. Render facts and uncertainty from already parsed main assets and report context. Keep Telegram transport separate and remove blind truncation at the send boundary.

## Technical Context

- **Runtime**: Python 3.12 in the reports/nightly GitHub Actions workflows; PowerShell assembles the nightly input. Existing project tests use pytest/unittest.
- **Current path**: main/close report artifacts → `scripts/fetch-latest-github-reports.ps1` → `reports/nightly-review-input.md` → `advisor.analyst_review` → `reports/analyst-final-review.md` → `advisor.telegram_notify` sends only `## Telegram summary`.
- **Current content boundary**: `advisor/analyst_review.py` parses main asset blocks into `AssetReview`, confirms tradeables with the existing main integrity gate, and generates both the full review and Telegram summary. Its legacy watch lists are ordered by labels and configured ticker order, not by an authoritative financial score.
- **Current transport boundary**: `advisor/telegram_notify.py` extracts the summary, applies a safety wrapper and forbidden-language replacement, then slices the final string at `TELEGRAM_MAX_CHARS = 3500`. The Bot API `sendMessage` supports 1–4096 text characters after entity parsing; the current project cap is a conservative 3500. No `parse_mode` is supplied.
- **Market data boundary**: BTC/ETH asset observations and stock/crypto regime fields may already reach the final review. SPY/QQQ/SMH are loaded as benchmarks upstream but are not primary asset observations in the inspected nightly input. The current `_market_brief_status` returns `missing` whenever any equity proxy is absent, masking useful context.
- **Source contract**: main emits entry, combined stop/invalidation, sizing, thesis and confidence, but final-review asset fields may contain `not_present_in_input`; confidence is not carried as an authoritative presentation field. No term may be reconstructed.
- **Dependencies**: no new library, service, provider call, schema or external integration.

## Constitution Check

No `.specify/memory/constitution.md` or active Spec Kit template/setup script exists in this checkout. Apply the approved specification and repository `AGENTS.md` instructions as the gates: narrow scope, minimal changes, inspect contracts, demonstrate focused RED before GREEN, preserve existing authority and manual execution, and avoid unrelated code. The design passes these gates. Recheck after implementation before any publication.

## Architecture and change boundaries

Keep content and semantics in `advisor/analyst_review.py`: select only existing confirmed tradeables and observation candidates, translate descriptive labels, render market and asset sections, and compose a bounded summary. Pass the already computed confirmed asset set or tickers into summary generation; do not rediscover approval from legacy labels inside presentation. Do not change the existing confirmation predicate, scoring, risk engine or report-grade semantics.

Keep `advisor/telegram_notify.py` responsible for extracting `## Telegram summary`, retaining transport safety, and enforcing the final delivery limit after wrapper/safety text is applied. Replace blind slicing with an explicit over-limit failure or a bounded-content contract that prevents sending incomplete critical values. Prefer composition within the content layer plus a final transport guard; no second summarizer or new service. The wrapper should place the human decision first and retain a short manual-execution line rather than leading with `decisao_final_conservadora: true`.

No presentation logic belongs in workflow YAML or PowerShell. The existing markdown section is the compatibility interface between generation and sender.

## Data flow and market-context strategy

1. Continue reading the selected main asset observations and existing nightly/main regime fields. Market status is **complete** when expected proxy observations contain usable facts, **partial** when at least one usable observed proxy or verified regime exists but expected context is missing, and **unavailable** only when there is no useful observed context. Presence of an asset name alone without usable metrics does not imply a measured price or market direction.
2. Use BTC/ETH values already present in `AssetReview.metrics`; retain source price/change text only when present. For SPY/QQQ/SMH, use an existing report field only if the field actually contains a per-proxy observation. Otherwise name each as unavailable. Treat `unknown`, `not_verified`, `missing` and similar sentinels as absent evidence, not as a market regime.
3. Do not add SPY/QQQ/SMH to scoring or watchlist just to fill the brief. Do not copy upstream benchmark objects into nightly artifacts in the first implementation: the current reporter exposes benchmark provenance/status but does not establish a complete per-proxy close/change presentation contract. Adding a transport path would widen the feature and still would not justify invented values. If implementation inspection finds a directly usable existing main/nightly field, parse that field locally and test it; otherwise the expected normal result is a **partial** brief with BTC/ETH and explicit missing equity proxies.
4. Keep the full review's market section truthful as well as the Telegram prose if its status is reused there. Changing `missing` to `partial` is presentation completeness only; it must not alter `report_data_grade`, the final-review integrity gate, or operational approval.

## Rendering and content policy

- Start with the approved-entry answer and its main reason; follow with market context, equities in observation, crypto in observation, short avoided/blocked reasons, final practical call and manual-execution note. Sections with no supported facts use a short absence statement rather than invented narrative.
- Confirmed main tradeables come first, from the existing gate only. Show every confirmed ticker. Include available entry, combined stop/invalidation and sizing as exact source strings. Missing terms are marked unavailable or omitted without implying a usable trade plan; confidence is not introduced.
- Observation candidates use existing review order only as review priority, with no "best trade" claim. If there is no authoritative single leader, the final call lists observation priorities. Normal detailed budget is up to three equities and three crypto assets; rejected/blocked get short reasoned lines.
- Render useful existing fields from `AssetReview` (metrics, thesis, confirms, contradicts, risks, valuation, events/news, path to operation, basic/flow status). Treat empty and sentinel fields as absent. Distinguish basic crypto data from flow/derivatives; do not infer that missing flow means missing basic price or operational approval.
- Translate legacy states in user-facing prose while preserving raw labels in the full audit artifact. For rejected/blocked, prefer an explicit operational blocker; otherwise use the first structured recorded reason, optionally one more if space permits. No new reason ranking or financial ranking.
- Explain `possible_session_detection_bug` as a possible classification problem. Do not turn suspicion into a confirmed bug or into a new decision.

### Deterministic size policy

The content layer should compose complete lines/sections, evaluate the final wrapped message size, and reduce content in a fixed order: (1) optional secondary fields within asset cards, (2) observation detail/count, (3) rejected/blocked detail, (4) secondary market context. Preserve decision, material uncertainty, every confirmed tradeable ticker, complete critical terms that fit, practical call and manual-execution line. If all tradeable terms cannot fit, omit whole terms and explicitly refer to the complete review; never clip a value or sentence mid-field. A normal brief targets 1,500–3,000 characters; exceptional briefs may be longer within the verified delivery limit. Keep one message. The final transport guard rejects over-limit output rather than slicing it.

## Compatibility strategy

- Keep the `## Telegram summary` heading and `extract_telegram_summary` behavior so the nightly workflow and send command remain unchanged.
- Keep full-review structured fields such as `source_decision`, `review_status`, `legacy_label`, `tradeable_confirmed`, source terms and diagnostic sections for audit. Change only presentation status text that must become truthful (for example market completeness).
- Existing tests assert old Telegram phrases (`Top equities`, `Melhor equity`, raw `watch_pending_checks`, `crypto_research_only`, `Market brief: missing`) and sender prefix `decisao_final_conservadora: true`. Replace only those assertions that are explicitly superseded by the approved UX; retain assertions for raw full-review contracts, no-trade safety, token secrecy and section extraction.
- Keep the sender's missing-secrets and missing-report behavior, payload shape and no-`parse_mode` plain-text delivery. Do not send a live message while developing.
- Make a small update to `docs/AUTOMATION_SETUP.md` describing the richer but still single-section Telegram output and the single-message size/safety rule; leave workflow setup and secret instructions intact.

## Test strategy and validation sequence

**Future implementation only:** establish focused RED cases before editing production code for (1) useful no-trade brief, (2) partial market context with BTC/ETH and absent SPY/QQQ/SMH, (3) basic crypto data vs unverified flow, (4) exact tradeable terms and missing term handling, (5) blocker reason precedence, (6) no invented single observation leader, (7) size pressure with all confirmed tickers and no mid-field cut, and (8) no status promotion or automatic execution. Use the existing fixtures where possible, adding one bounded high-volume fixture only if needed. Demonstrate the first focused failure, then implement minimal GREEN.

Run the focused regression set with the project venv if available:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_analyst_review_semantics.py tests/test_telegram_notify.py tests/test_report_budget_and_analyst_input.py
```

Add another focused test file only if an actually changed contract requires it. Review generated summaries and final wrapped payloads as plain text, including Unicode and over-limit cases. No full-suite gate by default. After focused tests, perform a bounded diff review and scope check. Any later commit/push or production validation follows explicit authorization and exact reviewed-state guards. Prefer checking the generated artifact/log first; a real Telegram send or nightly workflow run is a separate authorized production step. Production evidence should cover render quality, workflow status, decision semantics and delivered size.

## Likely files to change during implementation

- `advisor/analyst_review.py` — content selection, market completeness and deterministic brief rendering.
- `advisor/telegram_notify.py` — human-first safety wrapper and final no-truncation size guard.
- `tests/test_analyst_review_semantics.py` — focused content and authority cases; update superseded Telegram-only assertions.
- `tests/test_telegram_notify.py` — wrapper, size, extraction and safety cases.
- `docs/AUTOMATION_SETUP.md` — concise description of the new output.
- `tests/test_report_budget_and_analyst_input.py` — regression read/run initially; edit only if an existing report input contract is directly changed, which this plan does not require.

## Explicitly protected files and systems

No changes to `.github/workflows/financial-advisor-reports.yml`, `.github/workflows/financial-advisor-nightly-review.yml`, `scripts/fetch-latest-github-reports.ps1`, `advisor/report.py`, `advisor/live_loader.py`, `advisor/evidence_archive.py`, `advisor/evidence_collector.py`, `advisor/evidence_packager.py`, scoring, risk engine, canonical evidence/schema, maturation, provider assignment, predictive statistics, broker integration, or BNB/ZEC/Lighter scope. These were inspected as contracts only. No new provider calls, dependency or external execution.

## Risks and mitigations

| Risk | Design response |
| --- | --- |
| Legacy label accidentally grants approval | Pass confirmed tradeables from the existing gate; test watch/research/blocked/rejected non-promotion. |
| Market brief misstates partial context | Classify from usable observed facts; name absent proxies; keep report-grade logic separate. |
| Existing parser lacks some desired fields | Omit/mark unavailable; do not infer or expand provider/report generation just for prose. |
| Oversized message silently cuts a trade term or final call | Compose whole fields with deterministic reduction; final sender guard fails closed on overflow. |
| Wrapper safety replacement changes exact operational terms | Test the final wrapped payload for exact entry/stop/sizing preservation; keep replacement limited to prohibited imperative language. |
| Old string-based tests resist intentional UX change | Update only assertions superseded by the spec; preserve full artifact and transport contract tests. |
| Real runtime differs from local fixture | Verify artifact/log before any separately authorized production send; distinguish local proof from delivery proof. |

## Open technical questions

None blocking planning. During implementation, confirm any per-proxy main/nightly fields before using them and validate the chosen conservative payload cap against final wrapped plain text. The official Bot API limit is 4096 text characters; the current project cap is 3500.

## Post-design gate

The design does not add a decision path, data source, ranking model, order execution, schema change or workflow change. Only the planned presentation and transport guard are in scope. No source or test execution occurred during this planning phase.
