# Validation Guide: Telegram Analyst Review V2

This guide is for a later implementation phase. It was not executed during planning.

## Prerequisites

- Use the project checkout and its existing Python virtual environment.
- Use local fixtures or saved report artifacts only; do not set Telegram secrets for these checks.
- Review the approved [specification](spec.md), [message contract](contracts/telegram-message.md) and [plan](plan.md).

## Focused RED → GREEN

Before production edits, add the focused regression cases described in the plan and demonstrate a failing test for the old terse/no-context behavior. After the smallest implementation change, run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_analyst_review_semantics.py tests/test_telegram_notify.py tests/test_report_budget_and_analyst_input.py
```

Expected: no-trade briefs explain context and asset-specific reasons; partial BTC/ETH context does not read as wholly missing; basic crypto data and flow have distinct wording; confirmed tradeable terms match main; no watch/research/rejected/blocked promotion; oversized fixtures preserve critical content without mid-field truncation.

## Bounded review

- Inspect the generated `## Telegram summary` and final wrapped payload for representative no-trade, tradeable, missing-field, partial-context and oversized fixtures.
- Confirm the final payload length after wrapper/safety wording, and confirm no token or unrelated report section appears in it.
- Review only the implementation diff and the explicitly allowed files. A real send, workflow run, commit or push needs a separate authorized step.
