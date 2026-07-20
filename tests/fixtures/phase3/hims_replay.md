## HIMS
### decision
- base_decision: `watch_buy`
- missing_data_severity: `high`
- intermediate_decision: `technical_unvalidated`
### override
- rule_id: `below_minimum_market_cap_override`
- effect_type: `explicit_override`
- decision_before: `technical_unvalidated`
- decision_after: `avoid`
- final_decision: `avoid`
- replay_matches_observed_final: `true`
