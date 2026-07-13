# Post-Phase 2 Baseline

## Resumo executivo

- Código e artifacts: `dc0a2546e91127e9105983ea17579a06180dd103`; main run `29199329100`, close run `29199352253` (Financial Advisor Reports).
- A baseline foi executada no domingo, 2026-07-12: main às 13:01 BRT e close às 13:02 BRT. Portanto, `report_grade=diagnostic_not_decision_grade` e `no_trade_day` eram esperados e não são evidência contra os gates; o close não representa um fechamento real.
- O audit live isolado confirmou `eod_candle`, quote/candle separados, cache fetch time/age e provider capabilities no lineage. Os contratos de dados aparecem no audit, não integralmente no texto do report.
- Integração report: `quote_status` visível no Markdown = `False`; `macro_status=not_collected` legado visível = `True`. Ambos são integration gaps registrados, sem correção nesta baseline.
- Quotes/FMP: os artifacts Phase 2 distinguem `unsupported_by_plan` de rate limit/schema; o report textual ainda mostra preços `n/a` na tabela de coverage, portanto não serve como prova única de quote atual.
- Eventos: guidance e macro são `not_implemented` no audit; Alpha Vantage estava sem chave, portanto news é `not_configured`/não verificada enquanto SEC permanece separado no lineage.
- Cripto: HYPE preserva funding/OI quando recebidos; liquidations são `not_implemented`; CoinGecko fallback é daily/eod_candle e premium não é inventado com tempos incompatíveis.
- Esta baseline preserva seu valor diagnóstico para cache, timestamps, provenance, capabilities e gate tracing; não avalia a decisão operacional nem altera qualquer decisão.

## Antes vs depois

| Campo | Fase 1 | Pós-Fase 2 | Resultado |
|---|---|---|---|
| Candle de ações | EOD podia parecer live | `eod_candle` com `is_intraday=false` no lineage | corrigido |
| Fetch/cache time | snapshot recebia agora/0 | `cache_fetched_at`, cache age e source timestamp separados | corrigido |
| Quote/candle | somente candle histórico | quote separado no audit; texto do report ainda não o mostra | corrigido no dado; gap de integração no report |
| FMP 402 | erro pouco distinguível | `unsupported_by_plan`/capability auditável | corrigido |
| Guidance/macro | `not_collected` | `not_implemented` no audit | corrigido no dado; macro legado no report |
| News / SEC | misturados | statuses separados no lineage | corrigido |
| Setor | sem coleta | SPY/QQQ/SMH/IGV/XLV com provenance quando disponível | corrigido parcialmente; RS pode continuar ausente |
| Liquidações | endpoint inválido | `not_implemented`, sem valor fabricado | corrigido |
| HYPE flow | ausência global | funding/OI preservados por métrica | corrigido |

## Gates recorrentes

| Gate | Ativos afetados | Categoria | Frequência | Cap aplicado | Candidato para Fase 3 |
|---|---|---:|---:|---|---|
| `confidence_limiting_data_gap` | AMD, AVAX, BNB, BTC, CRDO, DELL, ETH, HIMS, HOOD, HYPE, INTC, LINK, MRVL, MSFT, MU, NVDA, SOL, USAR, XRP | `reporting_only` | 19 | `cap_to_watch_buy` | sim |
| `high_severity_data_not_watchlist` | AMD, AVAX, BNB, BTC, CRDO, DELL, ETH, HIMS, HOOD, HYPE, INTC, LINK, MRVL, MSFT, MU, NVDA, SOL, USAR, XRP | `reporting_only` | 19 | `cap_to_technical_unvalidated` | sim |
| `market_not_risk_on` | AMD, AVAX, BNB, BTC, CRDO, DELL, ETH, HIMS, HOOD, HYPE, INTC, LINK, MRVL, MSFT, MU, NVDA, SOL, USAR, XRP | `genuine_market_risk` | 19 | `cap_to_wait_or_watch_buy` | não |
| `earnings_data_missing` | AMD, CRDO, DELL, HIMS, HOOD, INTC, MRVL, MSFT, MU, NVDA, USAR | `real_data_failure` | 11 | `input_to_classification` | não |
| `guidance_recent_not_collected` | AMD, CRDO, DELL, HIMS, HOOD, INTC, MRVL, MSFT, MU, NVDA, USAR | `not_implemented` | 11 | `input_to_classification` | sim |
| `missing_average_volume` | AVAX, BNB, CRDO, DELL, HOOD, LINK, MRVL, MSFT, MU, USAR, XRP | `real_data_failure` | 11 | `input_to_classification` | não |
| `missing_market_cap` | AVAX, BNB, CRDO, DELL, HOOD, LINK, MRVL, MSFT, MU, USAR, XRP | `real_data_failure` | 11 | `input_to_classification` | não |
| `possible_priced_in` | AMD, CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, NVDA | `genuine_market_risk` | 9 | `input_to_classification` | não |
| `coinbase_premium_unavailable` | AVAX, BNB, BTC, ETH, HYPE, LINK, SOL, XRP | `optional_context_missing` | 8 | `input_to_classification` | sim |
| `fundamentals_unavailable` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `liquidations_unavailable` | AVAX, BNB, BTC, ETH, HYPE, LINK, SOL, XRP | `not_implemented` | 8 | `input_to_classification` | sim |
| `missing_eps_growth` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `missing_free_cash_flow` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `missing_margin_trend` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `missing_pe_history` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `missing_peg` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `missing_revenue_growth` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `mixed_provider_data` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `source_mismatch_possible` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `genuine_asset_risk` | 8 | `input_to_classification` | não |
| `yahoo_price_fallback` | CRDO, DELL, HIMS, HOOD, MRVL, MSFT, MU, USAR | `real_data_failure` | 8 | `input_to_classification` | não |
| `cvd_proxy_uses_taker_buy_sell_volume` | AVAX, BNB, BTC, ETH, LINK, SOL, XRP | `genuine_asset_risk` | 7 | `input_to_classification` | não |
| `high_volatility` | CRDO, INTC, MRVL, MU, USAR | `genuine_market_risk` | 5 | `input_to_classification` | não |
| `technical_unvalidated` | AMD, DELL, ETH, HYPE | `reporting_only` | 4 | `cap_to_technical_unvalidated` | sim |
| `recent_gap_risk` | AMD, MRVL, MU | `genuine_market_risk` | 3 | `cap_to_wait_or_watch_buy` | não |
| `relative_strength_weak` | AVAX, BNB, XRP | `genuine_market_risk` | 3 | `input_to_classification` | não |
| `below_minimum_market_cap` | HIMS | `genuine_asset_risk` | 1 | `input_to_classification` | não |
| `cvd_proxy_unavailable` | HYPE | `optional_context_missing` | 1 | `input_to_classification` | sim |
| `negative_or_invalid_pe` | INTC | `genuine_asset_risk` | 1 | `input_to_classification` | não |
| `negative_or_invalid_peg` | INTC | `genuine_asset_risk` | 1 | `input_to_classification` | não |
| `open_interest_change_unavailable` | HYPE | `optional_context_missing` | 1 | `input_to_classification` | sim |
| `valuation_extreme` | AMD | `genuine_asset_risk` | 1 | `input_to_classification` | não |

## Decisões hipotéticas sem alterar código

Os campos `diagnostic_counterfactuals` do JSON são rollback diagnóstico conservador: só retornam `base_decision` se todos os caps efetivos pertencerem à categoria removida. Não são recomendação, ordem ou mudança de gate.

## Critérios objetivos para Fase 3

1. Falhas reais: fundamentals/earnings/price history e providers restritos/fallbacks continuam afetando ativos específicos.
2. Não implementado: guidance, macro e liquidations aparecem repetidamente e devem ser tratados separadamente de falha transitória.
3. Opcional/não configurado: news sem Alpha Vantage e contextos de premium/CVD/OI change não devem ser confundidos com preço inválido.
4. Risco real: market_not_risk_on, gap, volatilidade, valuation e weak setup continuam preservados como riscos distintos.
5. Caps mais recorrentes e candidatos: consultar `recurring_gates` no JSON; candidates marcados são apenas agenda diagnóstica para Fase 3.

## Validação

- main/close vieram de GitHub Actions no SHA aprovado; nenhum artifact antigo foi reutilizado.
- gate tracing foi audit-only, em `.tmp/post-phase2-baseline/gate-trace.db`; não escreveu `data/advisor.db`, report normal, journal ou Telegram.
- Schema drift foi registrado no audit live e deve ser avaliado na Fase 3; não foi corrigido aqui.
