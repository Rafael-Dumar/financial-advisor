# Quickstart: future focused validation

This guide is for the later `/speckit.implement` phase and production acceptance. **No command below was executed during planning.** Use the reviewed implementation checkout and preserve its exact SHA in validation artifacts. The contract under test is [contracts/decision-evidence.md](contracts/decision-evidence.md).

## Local prerequisites

- Python environment used by this repository (project `.venv` when available), with its existing test dependencies.
- No live provider credentials, Telegram token, broker connection, or workflow run is needed for RED/GREEN tests.
- Main/close fixtures must preserve selected artifact identity, primary/discovery origin, data mode, session state, and source decisions; add only the evidence fields relevant to each case.

## RED → GREEN sequence

For each bounded slice in [plan.md](plan.md), first add the smallest failing test in the closest existing test module. Record the observed failure, then make the minimal production change and rerun that focused test. After GREEN, run its immediate dependent tests. Prefer the module targets below and avoid full-suite default:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_hardening.py tests/test_scoring_regime_report.py tests/test_risk_backtest.py tests/test_data_pipeline.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_phase25_report_integrity.py tests/test_phase25_final_review.py tests/test_analyst_review_semantics.py tests/test_report_budget_and_analyst_input.py
```

These are **future proposed commands**, not a request to run tests in `/speckit.plan`. Run narrower node/module selections for each RED case first. If scoring observation branches or rule catalog source locations change, include `tests/test_scoring_observability.py` and the existing runtime catalog validation. If a runtime path repair is proven, include `tests/test_runtime_scoring_intake.py` and `tests/test_runtime_scoring_integration.py`. Run the full suite only when focused evidence shows cross-cutting impact.

## Required scenario pairs

| Group | Must pass together |
| --- | --- |
| A report grade | Live fresh regular non-conflicting primary remains decision-grade despite degraded discovery; real primary stale/session/benchmark/window failure remains diagnostic. |
| B generic heuristics | Optional `*_unavailable` no longer caps solely from spelling; missing required price/history still blocks. |
| C stock earnings | Unknown timing prevents that stock's `tradeable`; verified clear timing itself does not cap; near/imminent measured timing keeps current effects. |
| D backtest | <30 or absent sample cannot support full eligibility; adequate sample missing win rate/EV cannot silently pass; valid direct EV with absent average win/loss is not capped; invalid EV stays limited. |
| E mixed providers | Compatible Yahoo/FMP capabilities have no generic cap; demonstrated identity/unit/time/provenance conflict still limits affected rule. |
| F crypto flow | Missing optional CVD/OI change/liquidations/premium/Binance flow is reported without global cap; observed high funding/OI/liquidation risk keeps current measured effect. |
| G crypto regime | Missing funding/OI never yields `funding_balanced`; observed zero remains an observed value and can take the existing balanced branch. |
| H context | Existing SPY/QQQ/SMH/BTC/ETH facts survive main → analyst input → nightly input → final; truly absent facts remain unavailable; disagreements show transport conflict. |
| I final review | Conflict `false` has no warning; `true` warns/gates; missing remains unknown. Usable decision-grade no-trade cites asset/rule reasons; real invalid main still blocks. |
| J discovery | Degraded discovery and overall diagnostic alone cannot downgrade primary, block operation, change a primary source decision, or promote discovery. |
| K runtime audit | Missing manifest/trace is observable, never changes decision/Telegram authority; any proven path repair retains intake safety. |

Inspect the generated full review and Telegram section as plain text, including `source_decision`, trade terms, evidence statuses, and the chosen operative reason. Do not send Telegram while testing. Existing tests that assert blanket fallback or `ev_components_missing` caps should change only after their new RED counterparts prove the approved contract.

## Later production acceptance

After implementation and separately authorized deployment, observe at least two **natural scheduled** production cycles at the reviewed SHA. Save selected main/close reports, analyst-review input, nightly input, final review, Telegram summary/delivery status, artifact IDs, and run logs. For each cycle check: semantic session state; no false decision-grade wait; primary/discovery isolation; observed market facts survive transport; genuine required gaps fail closed; optional/unsupported gaps do not become global blockers; every material primary decision has a rule/capability trace; runtime inconsistency remains observational; no automatic execution. Compare candidate counts with baseline only as a diagnostic, not a success target. Do not manually trigger or rerun workflows to satisfy the two-cycle requirement.
