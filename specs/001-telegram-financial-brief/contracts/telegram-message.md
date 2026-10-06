# Contract: Nightly Telegram brief

## Producer and consumer

- Producer: final review includes one `## Telegram summary` section in `reports/analyst-final-review.md`.
- Consumer: the existing `analyst-final` Telegram sender extracts only that section, wraps it with minimal safety text and sends one plain-text `sendMessage` payload. No other full-review section is sent.

## Observable payload

1. Begin with the operational approval answer and principal supported reason.
2. Describe market context as complete, partial or unavailable, showing existing facts and named missing proxies.
3. Show confirmed tradeables first, then a bounded equities/crypto observation brief, then concise rejected/blocked reasons.
4. End with a practical call and manual-execution/no-order statement.
5. Use human-readable states. Raw decision labels can stay in the full report, but must not dominate the phone message.
6. Preserve every confirmed tradeable ticker and each displayed main entry, combined stop/invalidation and sizing value exactly. Missing terms are never filled in.
7. Keep the final wrapped plain-text payload within the project delivery cap and the Bot API limit. Optional content is removed as complete fields/sections in a fixed order; no silent substring cut. If complete trade terms cannot all fit, point to the full report for omitted detail.

## Non-promotion invariant

`source_decision` plus the existing final-review integrity gate remains the only approval authority. The Telegram message cannot turn observation, research, rejected or blocked states into an approved entry and cannot place an order.
