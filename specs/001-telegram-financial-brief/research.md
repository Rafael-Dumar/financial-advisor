# Research: Telegram Analyst Review V2

This is bounded repository and interface-contract research for the implementation plan. No workflow or live send was run.

## Decision: Keep rendering in final review

**Rationale**: `advisor/analyst_review.py` already owns `AssetReview`, market brief, candidate order, full review sections and `## Telegram summary`; it computes confirmed tradeables from `source_decision` plus the existing integrity gate. `advisor/telegram_notify.py` extracts that section and sends it. Passing confirmed assets into the renderer avoids a second approval calculation.

**Alternatives considered**: Reparse the full markdown in the sender would duplicate domain semantics; workflow/script formatting would spread presentation across unrelated boundaries.

## Decision: Express partial market context from current facts

**Rationale**: The reports workflow's primary watchlist omits SPY/QQQ/SMH, though `live_loader.py` loads them as benchmarks. BTC/ETH and stock/crypto regimes are present in inspected nightly input. The current market status treats any missing equity proxy as `missing`, while the full review still shows BTC/ETH. Correcting presentation completeness and listing explicit gaps uses existing facts without altering scoring universe or collection.

**Alternatives considered**: Transport benchmark objects into nightly input would touch report/package contracts without a proven per-proxy price/change output requirement. Adding ETFs to the scoring/watchlist universe would change financial behavior and is excluded. A local parse of a genuinely existing per-proxy field is acceptable if verified; no such field is assumed.

## Decision: Preserve main trade terms verbatim where present

**Rationale**: Main report emits `Entrada ideal`, `Stop/invalidation` and `Tamanho maximo da posicao`. The final-review parser preserves them as text with an absent sentinel. It does not carry confidence as an authoritative presentation field. The renderer can show complete existing values without calculations.

**Alternatives considered**: Recompute numeric terms or use confidence to rank candidates would create a new decision path and violate the spec.

## Decision: Bound one plain-text Telegram message without slicing

**Rationale**: The sender currently slices at 3500 characters after adding its prefix. This can remove a critical value or call. The Bot API [sendMessage documentation](https://core.telegram.org/bots/api) specifies 1–4096 text characters after entity parsing; the existing project cap of 3500 offers margin. Compose and reduce optional sections before sending, then reject any final payload still over the cap.

**Alternatives considered**: Blind truncation is unsafe; multi-message delivery is unnecessary for the specified bounded brief; rich markup would add escaping and length complexity without clear UX benefit.

## Decision: Preserve the existing section transport contract

**Rationale**: Nightly workflow calls `advisor.telegram_notify analyst-final`; that command reads only `## Telegram summary`. Changing prose inside the section and the wrapper does not require workflow edits. Raw full-review labels remain available for audit even when Telegram prose changes.

**Alternatives considered**: Sending the full review would exceed mobile readability and size constraints; changing report artifact selection is unrelated.
