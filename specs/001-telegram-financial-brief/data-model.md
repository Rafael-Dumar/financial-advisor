# Data Model: Telegram Analyst Review V2

No persistent model or schema change is planned. These are transient concepts already represented by the nightly input and final review.

| Concept | Existing information | Constraint |
| --- | --- | --- |
| Operational decision | Main `source_decision`, final-review integrity gate, confirmed tradeable set | Only this can authorize tradeable presentation; close/discovery and legacy labels cannot. |
| Market observation | SPY/QQQ/SMH/BTC/ETH if present; existing price/change text; stock/crypto regimes | Show only usable observed facts. Missing or unverified fields remain explicit gaps. |
| Asset review | Ticker, type, descriptive label, metrics, thesis, confirms, contradicts, risks, valuation, events/news, path to operation, crypto basic/flow statuses | Presentation may select and shorten fields, never alter their financial meaning. |
| Trade terms | Optional entry, combined stop/invalidation, position sizing preserved from main | Display exactly when present; never calculate or cut mid-field. |
| Brief | Ordered decision, market, selected assets, rejection summary, practical call and manual-execution statement | One plain-text message within the delivery cap. |

## Relationships and state rules

- One nightly input identifies one selected main and close package. The main is the authority; close is comparison only.
- A confirmed tradeable is an existing primary main asset with `source_decision=tradeable` that passes the current final-review integrity gate. Presentation does not define a new transition.
- Observation and research labels describe review attention only. Rejected/blocked remain non-tradeable.
- Market completeness is a presentation classification of available context; it does not change report grade or trade readiness.
- The final brief is derived from these facts and contains no independently persisted decision.
