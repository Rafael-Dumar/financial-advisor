# FASE 3A.3.0.2 - Correcao Final de Consistencia da Especificacao

Status: especificacao somente. Nenhuma instrumentacao e implementada nesta fase.

## 0. Autoridade, baseline e contrato

Esta especificacao foi produzida contra o checkout congelado abaixo:

- branch: `main`
- `HEAD`: `20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa`
- `origin/main`: `20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa`
- caminho de trabalho: raiz do repositorio (path local nao serializado)
- timezone operacional dos reports: `America/Sao_Paulo`
- data de report: data BRT (`UTC-03:00`), nunca a data local implícita do processo

Contrato central para uma fase posterior:

```text
mesmos inputs
    -> mesma AssetDecision
    -> trace adicional somente observacional
```

O trace nao pode ser fonte de uma nova decisao, alterar uma decisao existente,
reaplicar uma regra, inferir duplicidade, criar um ledger ou modificar gates,
thresholds, caps, confidence, risk ou sizing.

Nesta fase somente este documento pode ser criado ou modificado. Nao devem ser
alterados `advisor/scoring.py`, outros arquivos de producao, reports, Telegram,
workflows, testes de `tests/phase3/` ou qualquer item untracked explicitamente
fora do escopo. Nao ha stage, commit ou push.

## 1. Arquivos inspecionados

### Caminho de scoring e modelos

- `advisor/scoring.py`
- `advisor/models.py`
- `advisor/risk.py`
- `advisor/backtest.py`
- `advisor/scan_engine.py`
- `advisor/data_pipeline.py`
- `advisor/live_loader.py`
- `advisor/fixtures.py`
- `advisor/cli.py`
- `advisor/audit.py`

### Persistencia, reports e consumidores

- `advisor/cache.py`
- `advisor/report.py`
- `advisor/analyst_review.py`
- `advisor/telegram_notify.py`
- `advisor/artifact_selection.py`
- `advisor/__main__.py`

### Agendamento, CI e selecao de artifacts

- `scripts/run-main-report.ps1`
- `scripts/run-close-report.ps1`
- `scripts/run-nightly-analyst-review.ps1`
- `scripts/fetch-latest-github-reports.ps1`
- `.github/workflows/financial-advisor-nightly-review.yml`

### Testes e evidencias anteriores usados como contexto

- `tests/test_hardening.py`
- `tests/test_scoring_regime_report.py`
- `tests/test_cache_config_cli.py`
- `tests/test_phase25_artifact_selection.py`
- `tests/test_telegram_notify.py`
- `reports/audit/phase3-trace-linkage.json`
- `reports/audit/phase3-real-cycle-baseline.json`

Os dois artifacts de `reports/audit/` sao contexto de auditoria. Eles nao sao
substitutos dos inputs reais de uma futura execucao instrumentada e nao permitem
replay do run antigo.

## 2. Auditoria do caminho real

### 2.1 Assinaturas e pontos de entrada

Assinatura atual, em `advisor/scoring.py:113`:

```python
def classify_asset(scored: ScoredAsset, backtest_stats: BacktestStats | None) -> AssetDecision:
```

O produtor imediato e `score_asset` em `advisor/scoring.py:11`:

```python
def score_asset(
    snapshot: AssetSnapshot,
    *,
    stock_regime_label: str,
    crypto_regime_label: str,
    account_capital: float = 50_000,
    risk_fraction: float = 0.005,
    relative_strength_percent: float | None = None,
    minimum_market_cap: float | None = None,
) -> ScoredAsset:
```

`classify_asset` possui exatamente dois callers de producao observados:

1. `advisor/cli.py:198`, no loop de `_scan`, depois de `score_asset` e de
   `_stats_for_snapshot`.
2. `advisor/audit.py:978`, no caminho de `_trace_gates`, para auditoria local.

Os testes chamam a funcao diretamente, principalmente em
`tests/test_hardening.py` e `tests/test_scoring_regime_report.py`.

`advisor/report.py`, `advisor/cache.py`, `advisor/analyst_review.py` e
`advisor/telegram_notify.py` nao chamam `classify_asset`; eles consomem ou
interpretam uma decisao ja produzida. Essa fronteira deve permanecer explicita.

### 2.2 Call graph textual real

```text
input acquisition
  -> AdvisorConfig / LiveDataLoader.load_snapshots()
     ou fixtures.load_scan_fixture()/snapshots_from_fixture()
  -> benchmarks e candles
  -> scan_engine.derive_market_regimes()
  -> scan_engine.derive_relative_strength()
  -> cli._scan()
     -> se candles < MIN_PRICE_HISTORY_FOR_SCORING:
           cli._unscorable_decision() -> AssetDecision(blocked)
           (classify_asset nao e chamado neste ramo)
     -> scoring.score_asset(snapshot, regimes, capital, risk, relative_strength, market_cap)
          -> risk.calculate_trade_plan()
          -> scoring._apply_provider_context()
          -> scoring._investment_quality_score()
          -> scoring._swing_trade_score()
          -> eventos, news e metricas crypto
          -> ScoredAsset
     -> cli._stats_for_snapshot() ou backtest.backtest_similar_setups()
          -> BacktestStats
     -> scoring.classify_asset(scored, backtest_stats)
          -> freshness/context limits
          -> _base_decision()
          -> caps, gates, overrides e ajustes de backtest
          -> _data_quality(), _missing_data_severity()
          -> _data_quality_score()
          -> _decision_confidence_score()
          -> AssetDecision
  -> cli._assign_universe_origins()
  -> report.render_markdown_report(decisions, report_context)
  -> report.render_analyst_review_input(decisions, review_context)
  -> report.render_html_report(markdown)
  -> cache.SQLiteCache.save_signal_journal(decisions, cache_context)
  -> cli._write_reports()
       -> advisor-report.md/html
       -> latest.md/html quando aplicavel
       -> history/<BRT-date>-main|close.md/html
  -> nightly artifact selection/fetch
  -> analyst_review.parse_review_package()
       -> preserva source_decision; nao executa scoring
  -> Telegram opcional, somente depois do Final Review
```

### 2.2.1 Call graph completo para a instrumentacao futura

Os dois callers reais de `classify_asset` permanecem distintos e devem ser
auditados separadamente:

| caminho | entrada | comportamento relevante | saida/consumidor |
|---|---|---|---|
| `advisor/cli.py:_scan` | universo produzido por acquisition e `score_asset` | chama `classify_asset` somente quando ha candles suficientes; ramo insuficiente chama `_unscorable_decision` e nao pode ser apresentado como invocacao do classifier | `AssetDecision` para report, cache e Final Review |
| `advisor/audit.py:_trace_gates` | snapshot/scored usado pela auditoria | chama `classify_asset` para diagnostico independente; nao deve consumir o trace para mudar a decisao auditada | evidencias de auditoria, sem decisao operacional nova |

Retornos bloqueados que precisam permanecer visiveis no call graph, mas nao
devem ser inventados como eventos do classifier:

- `cli._scan` pode retornar `_unscorable_decision` quando a amostra de candles
  esta abaixo do minimo;
- `_report` encerra com report bloqueado quando `config validate` falha ou a
  execucao live nao satisfaz os pre-requisitos;
- `_run_report_job` encerra com report bloqueado quando baseline, scan ou
  `require_live` falha;
- erros de acquisition/provider podem ser registrados pelo caminho existente e
  impedir a classificacao, sem fabricar `AssetDecision` nem runtime event;
- `audit._trace_gates` tem seu proprio tratamento de erro e nao e um terceiro
  caller operacional de producao.

O desenho futuro tem quatro fronteiras adicionais, todas fora desta fase:

```text
classify_asset(scored, stats)               -> AssetDecision
classify_asset_with_trace(scored, stats)    -> AssetDecision + RuntimeTrace
  ambos -> _classify_asset_observed(scored, stats) -> uma execucao do classifier
  classificacao -> RuntimeTraceCollector    -> serializers/hashes
  run collector -> artifact writer          -> aggregate JSON gzip/chunks
  reports/Final Review/Telegram             -> preservam decisao existente;
                                                nunca interpretam o trace
```

O adapter publico com trace e o writer futuro entram na matriz de callers, mas
nenhum caller pode chamar `_classify_asset_observed` diretamente. O artifact
writer recebe resultados ja produzidos e nao reexecuta scoring. A selecao
nightly valida SHA/data/schema do artifact; o Final Review preserva somente
referencia, status e hash, mantendo `source_decision` como autoridade.

### 2.3 Construcao de `AssetSnapshot`

`AssetSnapshot` e definido em `advisor/models.py:69-103`. A aquisicao live passa
por `LiveDataLoader` e `data_pipeline`; fixtures passam por `fixtures.py`. O
objeto contem candles, fundamentos, evento, campos crypto, limitations,
news/statuses, provenance de provider e metadados de quote/candle.

`score_asset` transforma o snapshot em `ScoredAsset` em
`advisor/scoring.py:94-110`. A construcao inclui:

- scores `investment_quality_score` e `swing_trade_score`;
- `RiskPlan` produzido por `calculate_trade_plan`;
- `alerts` e `limitations` deduplicados e ordenados;
- `thesis`, `metrics_summary`, entradas e `hold_suggestion`.

O trace de `classify_asset` deve registrar apenas os campos do snapshot que o
closure do classifier realmente le. Ele nao deve serializar o snapshot inteiro
por conveniencia.

### 2.4 Construcao de `BacktestStats`

`BacktestStats` e definido em `advisor/models.py:148-162`. No caminho principal,
`cli._stats_for_snapshot` transforma payload em `BacktestStats`; o audit tambem
pode usar `backtest_similar_setups` diretamente. `classify_asset` le somente:

- `setup_quality`;
- `sample_size`;
- `win_rate_2r`;
- `expected_value_r`;
- `median_days_to_2r`;
- `avg_win_r` e `avg_loss_r`.

Os demais campos sao mantidos fora de `classification_inputs` ate que uma
versao futura do classifier realmente os consuma.

### 2.5 Transformacoes dentro de `classify_asset`

Em `advisor/scoring.py:113-260`, a funcao:

1. captura `effective_now_utc` uma unica vez e passa a mesma hora aos helpers
   temporais;
2. copia `scored.alerts` e `scored.limitations` para listas locais;
3. resolve `sample_quality` a partir de `setup_quality`, tamanho ou `None`;
4. calcula freshness e pode acrescentar `stale_price_data`;
5. aplica limites de contexto nao coletado;
6. escolhe a decisao base ou `blocked`;
7. calcula o teto inicial por dados, hard gates, amostra e demais caps;
8. aplica caps de severidade, stale, backtest, caso especial e confidence;
9. calcula quality/severity/confidence;
10. aplica override de market cap e ajuste de earnings imminent;
11. constroi `AssetDecision` em `advisor/scoring.py:215-260`.

O classifier nao muta `ScoredAsset`; o trace futuro deve evitar alterar tambem as
listas ou snapshots recebidos.

### 2.6 Alertas, limitations, quality e confidence

`score_asset` gera alerts/limitations upstream, inclusive risk, provider,
eventos, news, liquidez, regime e metricas crypto. `classify_asset` copia essas
listas e pode acrescentar:

- `stale_price_data` em alert e limitation;
- `data_incomplete_confidence_limited`;
- `backtest_sample_low`;
- `high_severity_data_not_watchlist`;
- `weak_setup_win_rate`;
- `weak_or_negative_expected_value`;
- `negative_ev_with_high_data_severity`;
- limitacoes de news, macro, sector e componentes de EV nao coletados.

`_data_quality` e `_missing_data_severity` sao derivados de limitations.
`_data_quality_score` e `_decision_confidence_score` aplicam caps numericos
independentes, sem que o trace possa converter esses caps em politica nova.

### 2.7 Serializacao e consumidores atuais

Nao existe um serializer JSON geral de `AssetDecision`. Os consumidores atuais
sao:

- `report.render_markdown_report`, que imprime campos da decisao por ativo;
- `report.render_analyst_review_input`, que cria o input do Final Review;
- `report.render_html_report`, que transforma Markdown em HTML;
- `cache._signal_row` em `advisor/cache.py:445-478`, que grava campos selecionados
  no SQLite e serializa somente `reason_codes` com `json.dumps`;
- `analyst_review`, que parseia Markdown, preserva `source_decision` e declara
  que nao reexecuta scoring;
- Telegram, que le somente report Markdown/Final Review.

O trace deve ter serializers proprios e explicitos. Nao deve depender da forma
como qualquer um desses consumidores renderiza a decisao.

## 3. Fronteira de inputs e allowlist de classificacao

### 3.1 `ScoredAsset` realmente consumido

O serializer futuro de inputs pode incluir somente estes campos, com serializers
explícitos para os objetos aninhados:

```text
ScoredAsset.snapshot
ScoredAsset.investment_quality_score
ScoredAsset.swing_trade_score
ScoredAsset.risk_plan
ScoredAsset.alerts
ScoredAsset.limitations
ScoredAsset.thesis
ScoredAsset.metrics_summary
ScoredAsset.ideal_entry
ScoredAsset.alternative_entry
ScoredAsset.hold_suggestion
```

Isso cobre todos os campos acessados pelo classifier, sem serializar atributos
futuros ou privados que possam ser adicionados ao dataclass.

### 3.2 `AssetSnapshot` realmente consumido pelo classifier

Dentro do closure atual, os campos observados sao:

```text
symbol
asset_type
theme
candles[-1].date, ou data_timestamp quando candles estiver vazio
event.days_to_earnings
news_events.confirmed_status
news_events.already_priced
news_events.market_effect
news_events.news_confidence
news_events.news_event_type, somente para news_summary
data_source
data_timestamp
cache_age_seconds
```

`fundamentals`, `missing_data`, `provider_capabilities`, `earnings_status`,
`guidance_status`, `macro_status`, `news_status`, `sec_filings_status`, quote
provenance e metricas crypto nao sao lidos diretamente pelo classifier atual.
Eles somente entram no trace se seus efeitos ja estiverem materializados em
`ScoredAsset`, `alerts` ou `limitations`. Isso evita registrar dados nao
consumidos.

### 3.3 `BacktestStats` realmente consumido

```text
setup_quality
sample_size
win_rate_2r
expected_value_r
median_days_to_2r
avg_win_r
avg_loss_r
```

`win_rate_3r`, `median_days_to_3r`, `max_drawdown_r`, periodo,
`benchmark_comparison` e `warnings` nao devem entrar no trace atual se nao forem
consumidos pelo caminho classificado.

### 3.4 Estado derivado minimo

O trace deve registrar explicitamente, em vez de recalcular no consumidor:

- alerts e limitations antes da classificacao;
- `sample_quality` resolvido;
- `missing_data_severity` e `data_quality` antes e depois quando aplicavel;
- `data_quality_score`;
- `decision_confidence_score`;
- decisao base, teto corrente e decisao final;
- reason codes, alerts e limitations finais;
- campos de `RiskPlan` que ja vieram do `ScoredAsset`.

## 4. Inventario dos branches

### 4.1 Regra de contagem

O inventario abaixo conta 97 pontos condicionais observaveis na closure do
classifier no SHA congelado:

- 21 nos no corpo de `classify_asset`;
- 4 em `_base_decision`;
- 3 em `_is_intc_like_case`;
- 1 em `_hold_suggestion`;
- 3 em `_has_confidence_limiting_data_gap`;
- 2 em `_data_quality`;
- 3 em `_missing_data_severity`;
- 3 em `_apply_uncollected_context_limits`;
- 1 em `_freshness_context`;
- 4 em `_market_session`;
- 6 em `_data_quality_score`;
- 13 em `_decision_confidence_score`;
- 4 em `_event_check_status`;
- 2 em `_bucket_for_decision`;
- 4 em `_thesis_status`;
- 4 em `_sector_benchmark`;
- 1 em `_short_setup_score`;
- 1 em `_news_summary`;
- 2 em `risk.rate_sample_quality`;
- 15 expressoes condicionais inline em `classify_asset` e helpers, incluindo
  resolucao de sample quality, escolhas internas de cap, fallbacks de freshness,
  status de news, gap risk, short status, `_apply_cap`, `_weaker_cap` e a
  presenca de BacktestStats no predicate tecnico e a presenca de BacktestStats
  no calculo de confidence.

Cada linha das subsecoes 4.2 e 4.3 representa um `if`/`elif` do source. A
subsecao 4.4 registra cada expressao condicional inline que tambem muda um
campo observavel. Alternativas `else` e comparacoes internas do helper sao
descritas em `possible_before_values` e `possible_after_values` da mesma linha,
sem criar uma segunda regra artificial.

### 4.1.1 `rule_catalog` estatico versus runtime events

Os 97 pontos acima formam um catalogo estatico, presente uma unica vez por run.
O catalogo nao e uma lista de eventos ocorridos. Seu contrato futuro e:

```json
{
  "rule_id": "classify_asset.confidence_below_65",
  "source_locator": {
    "path": "advisor/scoring.py",
    "function": "classify_asset",
    "line_start": 198,
    "source_sha": "20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa"
  },
  "function": "classify_asset",
  "branch_signature": "if decision_confidence_score < 65",
  "axis": "decision",
  "effect_type": "cap",
  "evidence_keys": ["decision_confidence.score"]
}
```

O artifact agregado deve conter:

```text
rule_catalog_version
rule_catalog_hash
rule_catalog: [97 entradas unicas]
```

`rule_catalog_hash` e calculado sobre o catalogo canonico, com os entries
ordenados por `rule_id`, e nao e repetido em cada asset. Os 97 rule IDs continuam
unicos no catalogo.

Revisao de `axis` e `effect_type`:

- `classify_asset.confidence_below_65` pertence a `axis=decision`, pois o
  branch aplica cap a decisao; os branches de `_decision_confidence_score`
  permanecem em `axis=confidence` porque calculam o score de confidence antes
  do cap decisorio;
- `classify_asset.backtest_branch_entry` e `effect_type=control_flow`, pois
  somente abre ou pula o subcaminho de backtest;
- `base`, `cap`, `override` e `adjustment` ficam reservados a efeitos que
  mudam o eixo de decisao/teto, enquanto `annotation` registra estado derivado
  sem nova politica; quality/risk/other continuam metadata dos seus eixos.

Essa revisao foi aplicada ao catalogo inteiro: nenhum branch que apenas
calcula confidence, quality, risk, thesis, session, bucket ou metadata deve ser
classificado como cap/override de decisao.

Eventos runtime sao criados somente quando uma invocacao real alcanca o ponto.
A ausencia de um event nao tem semantica unica: ela somente pode provar
`unreached` quando a invocation e o asset trace satisfazem o contrato de
cobertura completa da secao 4.1.2. Em trace parcial ou failed, a mesma ausencia
e `unknown/unobserved`.

- evento presente com `reached=true`, `evaluated=true`, `matched=false`:
  branch alcancado, condicao avaliada e falsa;
- evento presente com `reached=true`, `evaluated=true`, `matched=true`:
  branch alcancado, condicao avaliada e verdadeira;
- input ausente nao e convertido em `evaluated=false`; o wrapper de evidencia
  deve registrar status factual e a condicao real deve ser executada;
- nunca gerar 97 events artificiais com `evaluated=false`.

`evaluated=false` somente existe quando a condicao foi alcancada mas uma
excecao impediu que ela produzisse um booleano. Nesse caso `matched=null`, o
evento termina com `terminated=true`/`termination_kind=raise` e a excecao e
propagada sem mudanca.

### 4.1.2 Contrato condicional para ausencia de evento

A inferencia `rule_id` ausente dos `events` = `unreached` somente e valida
quando todos os requisitos abaixo forem verdadeiros:

```text
observer_enabled=true
trace_status=complete
invocation_coverage_complete=true
```

Neste caso, se o rule ID estiver no catalogo aplicavel da funcao e nao estiver
em `reached_rule_ids`, ele pode entrar em `unreached_rule_ids`, respeitando a
ordem do fluxo e um early return conhecido. Em qualquer outro caso:

- `observer_enabled=false`, `trace_status=partial`, `trace_status=failed`,
  `coverage_complete=false` ou
  `invocation_coverage_complete=false` tornam a ausencia `unknown/unobserved`;
- regras depois do ultimo ponto confiavel nao entram em
  `unreached_rule_ids`;
- `matched=false` exige um event materializado que confirme que o branch foi
  alcancado e sua condicao foi falsa.

O nome `invocation_coverage_complete` e a forma logica da mesma flag
serializada como `coverage_complete`; as duas nao podem divergir.

Exemplo de invocation partial depois de uma falha do collector:

```json
{
  "invocation_id": "_sector_benchmark#1",
  "function": "_sector_benchmark",
  "catalog_rule_ids": ["r1", "r2", "r3", "r4"],
  "reached_rule_ids": ["r1", "r2"],
  "known_unreached_rule_ids": [],
  "unknown_rule_ids": ["r3", "r4"],
  "unreached_rule_ids": [],
  "coverage_status": "partial",
  "coverage_complete": false,
  "invocation_coverage_complete": false,
  "last_reliable_sequence": 12,
  "observation_failure_sequence": 13
}
```

Neste exemplo, `r3` e `r4` nao podem ser declarados `unreached`: a falha
impediu saber se o fluxo decisorio chegou a eles.

Os `rule_id` das tabelas abaixo sao os IDs canonicos que serao publicados no
catalogo; nao sao IDs a inserir no codigo nesta fase.

Abreviacoes dos rows:

- `SA`: `ScoredAsset`;
- `SN`: `AssetSnapshot`;
- `BT`: `BacktestStats`;
- `A`: alerts;
- `L`: limitations;
- `S`: estado de decisao/teto;
- `DQ`: data quality;
- `MS`: missing-data severity;
- `DC`: decision confidence.

O campo `source_path` e sempre relativo ao repositorio. Nenhum rule ID deve ser
inserido no codigo nesta fase.

### 4.2 Corpo de `classify_asset`

| rule_id | source_path | function | line_start | branch_signature | axis | effect_type | inputs_consumed | possible_before_values | possible_after_values |
|---|---|---|---:|---|---|---|---|---|---|
| `classify_asset.stale_annotation` | `advisor/scoring.py` | `classify_asset` | 125 | `if freshness["is_stale"]` | quality | annotation | `freshness.is_stale` | `fresh` | `A/L += stale_price_data` |
| `classify_asset.initial_blocking_base` | `advisor/scoring.py` | `classify_asset` | 129 | `if _has_blocking_data_gap(limitations)` | decision | base | `L` | `any` | `blocked` or `_base_decision(SA)` |
| `classify_asset.initial_confidence_cap` | `advisor/scoring.py` | `classify_asset` | 148 | `elif _has_confidence_limiting_data_gap(limitations)` | decision | cap | `L` | base candidate | cap `watch_buy`, add limitation |
| `classify_asset.initial_hard_gate_cap` | `advisor/scoring.py` | `classify_asset` | 151 | `elif any(alert in hard_gates for alert in alerts)` | decision | cap | `A`, hard-gate set | base candidate | `wait` for imminent earnings, otherwise `watch_buy` |
| `classify_asset.initial_low_sample_cap` | `advisor/scoring.py` | `classify_asset` | 153 | `elif has_low_sample` | decision | cap | `BT.sample_size` | base candidate | cap `watch_buy`, add `backtest_sample_low` |
| `classify_asset.max_blocking_cap` | `advisor/scoring.py` | `classify_asset` | 146 | `if _has_blocking_data_gap(limitations)` | decision | cap | `L` | current candidate | `blocked`, otherwise continue the max-decision chain |
| `classify_asset.high_severity_cap` | `advisor/scoring.py` | `classify_asset` | 159 | `if _missing_data_severity(limitations) == "high"` | decision | cap | `L`, `MS` | current `S` | weaker cap `technical_unvalidated`, alert annotation |
| `classify_asset.stale_cap` | `advisor/scoring.py` | `classify_asset` | 163 | `if "stale_price_data" in limitations` | decision | cap | `L` | current `S` | weaker cap `wait` |
| `classify_asset.backtest_branch_entry` | `advisor/scoring.py` | `classify_asset` | 166 | `if not has_low_sample and backtest_stats and win_rate_2r is not None` | decision | control_flow | `BT.sample_size`, `BT.win_rate_2r` | no BT branch | enter or skip win-rate/EV rows |
| `classify_asset.win_rate_below_35` | `advisor/scoring.py` | `classify_asset` | 169 | `if win_rate < 0.35` | decision | cap | `BT.win_rate_2r`, `SA.investment_quality_score` | current `S` | `avoid` when IQ < 35, otherwise `technical_unvalidated` |
| `classify_asset.win_rate_below_40` | `advisor/scoring.py` | `classify_asset` | 172 | `elif win_rate < 0.40` | decision | cap | `BT.win_rate_2r` | current `S` | weaker cap `wait` |
| `classify_asset.win_rate_below_45_nonpositive_ev` | `advisor/scoring.py` | `classify_asset` | 175 | `elif win_rate < 0.45 and ev is not None and ev <= 0` | decision | cap | `BT.win_rate_2r`, `BT.expected_value_r` | current `S` | weaker cap `wait`, alert |
| `classify_asset.nonpositive_ev` | `advisor/scoring.py` | `classify_asset` | 178 | `elif ev is not None and ev <= 0` | decision | cap | `BT.expected_value_r` | current `S` | weaker cap `wait`, alert |
| `classify_asset.negative_ev_high_severity` | `advisor/scoring.py` | `classify_asset` | 181 | `if ev is not None and ev < 0 and MS in {high,critical}` | decision | cap | `BT.expected_value_r`, `MS` | current `S` | weaker cap `technical_unvalidated`, alert |
| `classify_asset.intc_like_cap` | `advisor/scoring.py` | `classify_asset` | 185 | `if _is_intc_like_case(scored, alerts, backtest_stats)` | decision | cap | IQ, `A`, `BT.win_rate_2r` | current `S` | weaker cap `technical_unvalidated` |
| `classify_asset.confidence_below_65` | `advisor/scoring.py` | `classify_asset` | 198 | `if decision_confidence_score < 65` | decision | cap | `DC` | current `S` | weaker cap `watch_buy` |
| `classify_asset.technical_unvalidated_cap` | `advisor/scoring.py` | `classify_asset` | 200 | `if _is_technical_unvalidated(allowlisted_inputs)` | decision | cap | swing score, `L`, `BT`, `DQ`, `MS`, sample quality | current `S` | weaker cap `technical_unvalidated` |
| `classify_asset.minimum_market_cap_override` | `advisor/scoring.py` | `classify_asset` | 203 | `if "below_minimum_market_cap" in alerts` | decision | override | `A` | current `S` | exact `avoid` |
| `classify_asset.earnings_imminent_wait` | `advisor/scoring.py` | `classify_asset` | 207 | `if decision == "watch_buy" and "earnings_imminent" in alerts` | decision | adjustment | current decision, `A` | `watch_buy` | `wait` |
| `classify_asset.fundamental_gap_thesis` | `advisor/scoring.py` | `classify_asset` | 211 | `if _has_fundamental_validation_gap(limitations)` | other | annotation | `L` | scored thesis | conservative fundamental-gap thesis |
| `classify_asset.earnings_missing_thesis` | `advisor/scoring.py` | `classify_asset` | 213 | `elif "earnings_data_missing" in limitations` | other | annotation | `L` | scored thesis | earnings-not-verified thesis |

### 4.3 Helpers de decisao, predicates e metadata

| rule_id | source_path | function | line_start | branch_signature | axis | effect_type | inputs_consumed | possible_before_values | possible_after_values |
|---|---|---|---:|---|---|---|---|---|---|
| `classify_asset.base_avoid` | `advisor/scoring.py` | `_base_decision` | 264 | `swing < 45 or investment < 25` | decision | base | SA scores | no base | `avoid` |
| `classify_asset.base_wait` | `advisor/scoring.py` | `_base_decision` | 266 | `swing < 60` | decision | base | swing score | no prior base | `wait` |
| `classify_asset.base_tradeable` | `advisor/scoring.py` | `_base_decision` | 268 | `swing >= 75 and investment >= 70` | decision | base | SA scores | no prior base | `tradeable` |
| `classify_asset.base_technical_unvalidated` | `advisor/scoring.py` | `_base_decision` | 270 | `swing >= 70 and investment < 50` | decision | base | SA scores | no prior base | `technical_unvalidated` |
| `classify_asset.intc_investment_threshold` | `advisor/scoring.py` | `_is_intc_like_case` | 299 | `if investment_quality_score >= 45` | decision | annotation | IQ | candidate special case | predicate false |
| `classify_asset.intc_gap_requirement` | `advisor/scoring.py` | `_is_intc_like_case` | 301 | `if "recent_gap_risk" not in alerts` | decision | annotation | `A` | special case candidate | predicate false |
| `classify_asset.intc_valuation_requirement` | `advisor/scoring.py` | `_is_intc_like_case` | 303 | `if negative PE/PEG alerts absent` | decision | annotation | `A` | special case candidate | predicate false or continue |
| `classify_asset.hold_median_days` | `advisor/scoring.py` | `_hold_suggestion` | 309 | `sample >= 30 and median_days_to_2r is not None` | other | annotation | `BT`, SA hold suggestion | original hold | median-days hold text |
| `classify_asset.confidence_nonblocking_skip` | `advisor/scoring.py` | `_has_confidence_limiting_data_gap` | 322 | `if limitation in non_blocking` | confidence | annotation | each `L` value | limitation considered | skip limitation |
| `classify_asset.confidence_explicit_limitation` | `advisor/scoring.py` | `_has_confidence_limiting_data_gap` | 324 | `if limitation in explicitly_limiting` | confidence | annotation | each `L` value | no limiting gap | limiting gap true |
| `classify_asset.confidence_pattern_limitation` | `advisor/scoring.py` | `_has_confidence_limiting_data_gap` | 326 | `missing_/insufficient_/_unavailable/_not_live/_demo` | confidence | annotation | each `L` value | no limiting gap | limiting gap true |
| `classify_asset.data_quality_blocked` | `advisor/scoring.py` | `_data_quality` | 364 | `if blocking data gap` | quality | annotation | `L` | unknown/limited | `blocked` |
| `classify_asset.data_quality_limited` | `advisor/scoring.py` | `_data_quality` | 366 | `if confidence limiting data gap` | quality | annotation | `L` | `ok` | `limited` |
| `classify_asset.severity_blocking` | `advisor/scoring.py` | `_missing_data_severity` | 372 | `if blocking data gap` | quality | annotation | `L` | low/medium | `critical` |
| `classify_asset.severity_high` | `advisor/scoring.py` | `_missing_data_severity` | 374 | `earnings missing or unavailable limitation` | quality | annotation | `L` | low/medium | `high` |
| `classify_asset.severity_medium` | `advisor/scoring.py` | `_missing_data_severity` | 376 | `if confidence limiting data gap` | quality | annotation | `L` | low | `medium` |
| `classify_asset.uncollected_news_limit` | `advisor/scoring.py` | `_apply_uncollected_context_limits` | 397 | `if not snapshot.news_events` | quality | annotation | `SN.news_events` | collected | `news_not_collected_confidence_limited` |
| `classify_asset.uncollected_sector_limit` | `advisor/scoring.py` | `_apply_uncollected_context_limits` | 400 | `sector benchmark and stock` | quality | annotation | `SN.theme`, `SN.asset_type` | no sector limitation | `sector_relative_strength_not_collected` |
| `classify_asset.missing_ev_components` | `advisor/scoring.py` | `_apply_uncollected_context_limits` | 402 | `EV present and avg win/loss missing` | quality | annotation | `BT.expected_value_r`, `BT.avg_win_r`, `BT.avg_loss_r` | complete EV components | `ev_components_missing` |
| `classify_asset.cache_stale` | `advisor/scoring.py` | `_freshness_context` | 414 | `cache_age_seconds > 24h` | quality | annotation | `SN.cache_age_seconds` | fresh or unknown | stale with cache-age reason |
| `classify_asset.market_session_weekend` | `advisor/scoring.py` | `_market_session` | 430 | `weekday >= 5` | other | annotation | `effective_now_utc` | weekday | `closed` |
| `classify_asset.market_session_regular` | `advisor/scoring.py` | `_market_session` | 432 | `13:30 <= minutes < 20:00 UTC` | other | annotation | `effective_now_utc` | weekday outside regular | `regular` |
| `classify_asset.market_session_pre` | `advisor/scoring.py` | `_market_session` | 434 | `08:00 <= minutes < 13:30 UTC` | other | annotation | `effective_now_utc` | weekday outside pre | `pre_market` |
| `classify_asset.market_session_after` | `advisor/scoring.py` | `_market_session` | 436 | `20:00 <= minutes < 24:00 UTC` | other | annotation | `effective_now_utc` | weekday outside after | `after_hours` |
| `classify_asset.data_score_blocked` | `advisor/scoring.py` | `_data_quality_score` | 477 | `data_quality == blocked` | quality | annotation | `DQ` | any score | `0` |
| `classify_asset.data_score_limited` | `advisor/scoring.py` | `_data_quality_score` | 479 | `data_quality == limited` | quality | cap | `DQ` | score up to 95 | cap 65 |
| `classify_asset.data_score_high_severity` | `advisor/scoring.py` | `_data_quality_score` | 481 | `missing_severity == high` | quality | cap | `MS` | current score | cap 55 |
| `classify_asset.data_score_critical_severity` | `advisor/scoring.py` | `_data_quality_score` | 483 | `missing_severity == critical` | quality | cap | `MS` | current score | cap 20 |
| `classify_asset.data_score_earnings_missing` | `advisor/scoring.py` | `_data_quality_score` | 485 | `earnings_data_missing in limitations` | quality | cap | `L` | current score | cap 60 |
| `classify_asset.data_score_stale` | `advisor/scoring.py` | `_data_quality_score` | 487 | `stale_price_data in limitations` | quality | cap | `L` | current score | cap 50 |
| `classify_asset.confidence_sample_low` | `advisor/scoring.py` | `_decision_confidence_score` | 502 | `sample_size < 30` | confidence | cap | `BT.sample_size` | score | cap 45 |
| `classify_asset.confidence_sample_medium` | `advisor/scoring.py` | `_decision_confidence_score` | 504 | `elif sample_size < 100` | confidence | cap | `BT.sample_size` | score | cap 70 |
| `classify_asset.confidence_nonpositive_ev` | `advisor/scoring.py` | `_decision_confidence_score` | 506 | `EV is not None and EV <= 0` | confidence | cap | `BT.expected_value_r` | score | cap 50 |
| `classify_asset.confidence_earnings_missing` | `advisor/scoring.py` | `_decision_confidence_score` | 508 | `earnings_data_missing in limitations` | confidence | cap | `L` | score | cap 55 |
| `classify_asset.confidence_mixed_provider` | `advisor/scoring.py` | `_decision_confidence_score` | 510 | `mixed_provider_data in limitations` | confidence | cap | `L` | score | cap 55 |
| `classify_asset.confidence_neutral_market` | `advisor/scoring.py` | `_decision_confidence_score` | 512 | `market_not_risk_on in alerts` | confidence | cap | `A` | score | cap 75 |
| `classify_asset.confidence_risk_off` | `advisor/scoring.py` | `_decision_confidence_score` | 514 | `market_risk_off in alerts` | confidence | cap | `A` | score | cap 45 |
| `classify_asset.confidence_stale` | `advisor/scoring.py` | `_decision_confidence_score` | 516 | `stale_price_data in limitations` | confidence | cap | `L` | score | cap 45 |
| `classify_asset.confidence_news_low` | `advisor/scoring.py` | `_decision_confidence_score` | 518 | `rumor not confirmed or news confidence low` | confidence | cap | `L` | score | cap 55 |
| `classify_asset.confidence_news_not_collected` | `advisor/scoring.py` | `_decision_confidence_score` | 520 | `news_not_collected_* in limitations` | confidence | cap | `L` | score | cap 80 |
| `classify_asset.confidence_macro_not_collected` | `advisor/scoring.py` | `_decision_confidence_score` | 522 | `macro_not_collected_* in limitations` | confidence | cap | `L` | score | cap 75 |
| `classify_asset.confidence_sector_not_collected` | `advisor/scoring.py` | `_decision_confidence_score` | 524 | `sector_relative_strength_* in limitations` | confidence | cap | `L` | score | cap 70 |
| `classify_asset.confidence_ev_components` | `advisor/scoring.py` | `_decision_confidence_score` | 526 | `ev_components_missing in limitations` | confidence | cap | `L` | score | cap 60 |
| `classify_asset.event_crypto` | `advisor/scoring.py` | `_event_check_status` | 532 | `asset_type == crypto` | other | annotation | `SN.asset_type` | stock status | `not_applicable` |
| `classify_asset.event_source_unavailable` | `advisor/scoring.py` | `_event_check_status` | 534 | `earnings_unavailable in limitations` | quality | annotation | `L` | event status | `source_unavailable` |
| `classify_asset.event_not_collected` | `advisor/scoring.py` | `_event_check_status` | 536 | `earnings_data_missing in limitations` | quality | annotation | `L` | event status | `not_collected` |
| `classify_asset.event_verified` | `advisor/scoring.py` | `_event_check_status` | 538 | `event exists and days_to_earnings is not None` | quality | annotation | `SN.event` | unverified | `verified` |
| `classify_asset.bucket_known` | `advisor/scoring.py` | `_bucket_for_decision` | 544 | `decision in known bucket set` | other | annotation | final decision | unknown | same decision bucket |
| `classify_asset.bucket_speculative` | `advisor/scoring.py` | `_bucket_for_decision` | 546 | `decision == speculative_watch` | other | annotation | final decision | speculative | `technical_unvalidated` |
| `classify_asset.thesis_fundamental_gap` | `advisor/scoring.py` | `_thesis_status` | 552 | `fundamental validation gap` | other | annotation | `L` | any thesis status | `unknown` |
| `classify_asset.thesis_strengthening` | `advisor/scoring.py` | `_thesis_status` | 554 | `IQ >= 70 and swing >= 70` | other | annotation | SA scores | no status | `strengthening` |
| `classify_asset.thesis_weakening` | `advisor/scoring.py` | `_thesis_status` | 556 | `EV is not None and EV < 0` | other | annotation | `BT.expected_value_r` | no prior status | `weakening` |
| `classify_asset.thesis_stable` | `advisor/scoring.py` | `_thesis_status` | 558 | `IQ >= 55` | other | annotation | IQ | no prior status | `stable` |
| `classify_asset.sector_semiconductors` | `advisor/scoring.py` | `_sector_benchmark` | 564 | `theme == semiconductors` | other | annotation | `SN.theme` | no benchmark | `SMH` |
| `classify_asset.sector_software` | `advisor/scoring.py` | `_sector_benchmark` | 566 | `theme in software/software_ai` | other | annotation | `SN.theme` | no benchmark | `IGV` |
| `classify_asset.sector_cloud` | `advisor/scoring.py` | `_sector_benchmark` | 568 | `theme == cloud_ecommerce` | other | annotation | `SN.theme` | no benchmark | `QQQ` |
| `classify_asset.sector_healthcare` | `advisor/scoring.py` | `_sector_benchmark` | 570 | `theme == healthcare` | other | annotation | `SN.theme` | no benchmark | `XLV` |
| `classify_asset.short_setup_threshold` | `advisor/scoring.py` | `_short_setup_score` | 576 | `swing_trade_score <= 35` | risk | annotation | swing score | 0 | `100 - swing score` |
| `classify_asset.news_summary_empty` | `advisor/scoring.py` | `_news_summary` | 582 | `if not news_events` | other | annotation | `SN.news_events` | news list | `None` or joined normalized summary |
| `classify_asset.sample_quality_low` | `advisor/risk.py` | `rate_sample_quality` | 82 | `sample_size < 30` | confidence | annotation | `BT.sample_size` | unknown | `low` |
| `classify_asset.sample_quality_medium` | `advisor/risk.py` | `rate_sample_quality` | 84 | `sample_size < 100` | confidence | annotation | `BT.sample_size` | not low | `medium`, otherwise `high` |

### 4.4 Expressoes condicionais inline

| rule_id | source_path | function | line_start | branch_signature | axis | effect_type | inputs_consumed | possible_before_values | possible_after_values |
|---|---|---|---:|---|---|---|---|---|---|
| `classify_asset.sample_quality_setup_quality` | `advisor/scoring.py` | `classify_asset` | 117 | `backtest_stats and backtest_stats.setup_quality` | confidence | annotation | `BT.setup_quality` | no setup quality | supplied setup quality |
| `classify_asset.sample_quality_derived` | `advisor/scoring.py` | `classify_asset` | 119 | `backtest_stats` in sample-quality conditional | confidence | annotation | `BT.sample_size` | no BacktestStats | `rate_sample_quality` or `null` |
| `classify_asset.hard_gate_earnings_choice` | `advisor/scoring.py` | `classify_asset` | 152 | `"wait" if earnings_imminent else "watch_buy"` | decision | cap | `A.earnings_imminent` | hard-gate candidate | `wait` or `watch_buy` |
| `classify_asset.low_win_rate_choice` | `advisor/scoring.py` | `classify_asset` | 171 | `"avoid" if investment_quality_score < 35 else "technical_unvalidated"` | decision | cap | IQ, `BT.win_rate_2r` | weak win-rate candidate | `avoid` or `technical_unvalidated` |
| `classify_asset.last_price_timestamp_fallback` | `advisor/scoring.py` | `_freshness_context` | 411 | `snapshot.candles[-1].date if snapshot.candles else data_timestamp` | other | annotation | `SN.candles`, `SN.data_timestamp` | candle available | last candle date or data timestamp |
| `classify_asset.freshness_stale_reason_choice` | `advisor/scoring.py` | `_freshness_context` | 413 | `"price_cache_or_last_candle_stale" if is_stale else None` | quality | annotation | `is_stale` | not stale | stale reason or null |
| `classify_asset.apply_cap_rank_choice` | `advisor/scoring.py` | `_apply_cap` | 276 | `decision if rank(decision) >= rank(cap) else cap` | decision | cap | decision, cap, rank map | current rank >= cap rank | current decision or cap |
| `classify_asset.weaker_cap_rank_choice` | `advisor/scoring.py` | `_weaker_cap` | 280 | `current if rank(current) >= rank(new_cap) else new_cap` | decision | cap | current, new cap, rank map | current rank >= new cap rank | current or weaker cap |
| `classify_asset.technical_ev_presence` | `advisor/scoring.py` | `_is_technical_unvalidated` | 462 | `backtest_stats.expected_value_r if backtest_stats else None` | decision | annotation | `BT.expected_value_r` | no BacktestStats | EV or null for predicate |
| `classify_asset.confidence_sample_size_presence` | `advisor/scoring.py` | `_decision_confidence_score` | 501 | `sample_size = backtest_stats.sample_size if backtest_stats else 0` | confidence | annotation | `BT.sample_size` | no BacktestStats | actual sample size or zero |
| `classify_asset.last_price_timestamp_field` | `advisor/scoring.py` | `classify_asset` | 240 | `str(last_price_timestamp) if value else None` | other | annotation | freshness timestamp | no timestamp | string or null |
| `classify_asset.stale_reason_field` | `advisor/scoring.py` | `classify_asset` | 243 | `str(stale_reason) if value else None` | quality | annotation | freshness stale reason | no stale reason | string or null |
| `classify_asset.news_status_field` | `advisor/scoring.py` | `classify_asset` | 245 | `"collected" if news_events else "not_collected"` | quality | annotation | `SN.news_events` | empty news | collected or not collected |
| `classify_asset.gap_risk_field` | `advisor/scoring.py` | `classify_asset` | 257 | `"high" if recent_gap_risk in alerts else "unknown"` | risk | annotation | `A` | no gap alert | high or unknown |
| `classify_asset.short_status_field` | `advisor/scoring.py` | `classify_asset` | 259 | `"watch_only" if short_setup_score >= 70 else "not_evaluated"` | risk | annotation | short setup score | score below threshold | watch_only or not_evaluated |

### 4.5 Upstream producer boundary

Os branches abaixo podem alterar campos que aparecem em `AssetDecision`, mas nao
sao branches de `classify_asset`: eles ocorrem antes, enquanto `ScoredAsset` e
`RiskPlan` sao construidos. Por isso nao devem receber rule IDs de classifier ou
ser tratados como eventos de cap dentro do trace de `classify_asset`.

| producer | source range | campos afetados | tratamento no trace desta fase |
|---|---|---|---|
| `score_asset` | `advisor/scoring.py:21-110` | scores, risk plan, alerts, limitations, thesis, metrics, entries | registrar os valores allowlisted recebidos; nao reexecutar o producer |
| `_investment_quality_score` | `advisor/scoring.py:599-697` | investment score, alerts, limitations | registrar scores e condicoes materializadas em `SA` |
| `_swing_trade_score` | `advisor/scoring.py:700-758` | swing score, alerts, limitations | registrar scores e condicoes materializadas em `SA` |
| `_apply_news_context` | `advisor/scoring.py:347-361` | news alerts/limitations | registrar somente os quatro campos de news consumidos |
| `_apply_provider_context` | `advisor/scoring.py:382-390` | provider alerts/limitations | registrar status materializado, sem endpoint/resposta raw |
| `calculate_trade_plan` | `advisor/risk.py:10-65` | todos os campos de `RiskPlan` e risk alerts | tratar `RiskPlan` como input ja produzido |
| `rate_sample_quality` | `advisor/risk.py:81-86` | `sample_quality` | os dois thresholds estao no catalogo acima |

Essas fronteiras preservam o escopo real: o trace observa a execucao do
classifier, nao cria um segundo scorer nem reconstrói dados de provider.

### 4.6 Invocation model e early returns

O runtime trace nao e um grafo estatico de 97 eventos. Ele registra invocacoes
reais dos helpers, inclusive quando o mesmo helper e chamado mais de uma vez.
Para `theme = "software"`, o fluxo real de `_sector_benchmark` e:

1. `sector_semiconductors` e alcancado, avaliado e retorna `matched=false`;
2. a execucao continua para `sector_software`;
3. `sector_software` e alcancado, avaliado e retorna `matched=true`;
4. esse branch provoca `return`; somente branches posteriores ficam nao
   alcancados.

Os IDs abaixo preservam o ID canonico do catalogo (`classify_asset.*`). O nome
local do branch no helper e mostrado em `branch_label` como
`_sector_benchmark.sector_*`; ele nao cria um segundo rule ID.
O recorte usa o identificador local `_sector_benchmark#1`; no artifact completo,
`parent_invocation_id` liga esse identificador a `classify_asset#1` sem perder
determinismo.

```json
{
  "invocation_id": "_sector_benchmark#1",
  "function": "_sector_benchmark",
  "parent_invocation_id": "classify_asset#1",
  "call_ordinal": 1,
  "started_sequence": 1,
  "completed_sequence": 2,
  "termination": {
    "kind": "return",
    "rule_id": "classify_asset.sector_software",
    "sequence": 2
  },
  "termination_rule_id": "classify_asset.sector_software",
  "coverage_status": "complete",
  "coverage_complete": true,
  "invocation_coverage_complete": true,
  "last_reliable_sequence": 2,
  "observation_failure_sequence": null,
  "catalog_rule_ids": [
    "classify_asset.sector_semiconductors",
    "classify_asset.sector_software",
    "classify_asset.sector_cloud",
    "classify_asset.sector_healthcare"
  ],
  "reached_rule_ids": [
    "classify_asset.sector_semiconductors",
    "classify_asset.sector_software"
  ],
  "known_unreached_rule_ids": [
    "classify_asset.sector_cloud",
    "classify_asset.sector_healthcare"
  ],
  "unreached_rule_ids": [
    "classify_asset.sector_cloud",
    "classify_asset.sector_healthcare"
  ],
  "unknown_rule_ids": []
}
```

Os dois events materializados para essa invocation sao:

```json
[
  {
    "sequence": 1,
    "invocation_id": "_sector_benchmark#1",
    "rule_id": "classify_asset.sector_semiconductors",
    "branch_label": "_sector_benchmark.sector_semiconductors",
    "reached": true,
    "evaluated": true,
    "matched": false,
    "terminated": false,
    "termination_kind": null,
    "axis": "other",
    "effect_type": "annotation",
    "evidence_keys": ["asset.theme"],
    "condition_inputs": {"theme": "software"},
    "state_changes": {},
    "reason_codes_added": [],
    "alerts_added": [],
    "limitations_added": []
  },
  {
    "sequence": 2,
    "invocation_id": "_sector_benchmark#1",
    "rule_id": "classify_asset.sector_software",
    "branch_label": "_sector_benchmark.sector_software",
    "reached": true,
    "evaluated": true,
    "matched": true,
    "terminated": true,
    "termination_kind": "return",
    "axis": "other",
    "effect_type": "annotation",
    "evidence_keys": ["asset.theme"],
    "condition_inputs": {"theme": "software"},
    "state_changes": {},
    "reason_codes_added": [],
    "alerts_added": [],
    "limitations_added": []
  }
]
```

| branch | reached | evaluated | matched | terminated |
|---|---:|---:|---:|---:|
| `sector_semiconductors` | true | true | false | false |
| `sector_software` | true | true | true | true |
| branches posteriores | false | false | null | false |

`sector_semiconductors` pertence a `reached_rule_ids`, nunca a
`unreached_rule_ids`. Os branches posteriores nao geram events; somente podem
ser classificados como `unreached` quando a invocation tem cobertura completa.

Regras do modelo:

- `invocation_id` e unico dentro do asset trace e nao usa endereco de memoria;
- a forma canonica e o caminho de invocacao
  `<parent_invocation_id>/<function>#<call_ordinal>`; a raiz e
  `classify_asset#1`;
- `call_ordinal` e deterministico dentro do parent e segue a ordem real de
  chamada;
- `sequence` e global, crescente e unico por ativo;
- o mesmo `rule_id` pode aparecer em eventos de `invocation_id` diferentes;
- timestamps volateis nao participam da identidade;
- `reached_rule_ids` registra somente regras alcancadas;
- `coverage_status` e `complete`, `partial` ou `failed`;
- `coverage_complete` e a fonte booleana da condicao
  `invocation_coverage_complete`; se ambos forem serializados, devem ser sempre
  iguais;
- `last_reliable_sequence` e o ultimo ponto que o collector confirma;
- `observation_failure_sequence` aponta a primeira falha de observabilidade,
  quando houver;
- `unreached_rule_ids` so pode ser calculado com cobertura completa;
- `known_unreached_rule_ids` e `unknown_rule_ids` separam, respectivamente,
  regras provadamente nao alcancadas e regras ainda nao observadas em cobertura
  parcial.

Helpers relevantes com retornos antecipados ou retorno por fall-through:

| function | rule/return path | termination esperada |
|---|---|---|
| `_base_decision` | `base_avoid`, `base_wait`, `base_tradeable`, `base_technical_unvalidated` e fall-through `watch_buy` | `return` na primeira condicao verdadeira; caso contrario, retorno final |
| `_is_intc_like_case` | thresholds de investimento, gap e valuation | `return false` nas tres condicoes verdadeiras; retorno booleano final |
| `_market_session` | weekend, regular, pre-market, after-hours e fallback | todo ramo termina a invocacao |
| `_data_quality_score` | `data_quality == blocked` | `return 0` encerra antes dos caps seguintes |
| `_has_confidence_limiting_data_gap` | limitation explicita/pattern | `return true` encerra no primeiro match; retorno final `false` |
| `_data_quality` | blocked/limited/final ok | primeiro ramo verdadeiro retorna |
| `_missing_data_severity` | critical/high/medium/final low | primeiro ramo verdadeiro retorna |
| `_event_check_status` | crypto/unavailable/not collected/verified/final | primeiro ramo verdadeiro retorna |
| `_bucket_for_decision` | known/speculative/fallback | primeiro ramo verdadeiro retorna |
| `_thesis_status` | fundamental/strengthening/weakening/stable/final | primeiro ramo verdadeiro retorna |
| `_sector_benchmark` | quatro themes/fallback `None` | primeiro theme match retorna |
| `_short_setup_score` | threshold/fallback zero | todo ramo retorna |
| `_news_summary` | lista vazia/final joined text | ramo vazio retorna `None`; caso contrario retorno final |
| `rate_sample_quality` | low/medium/final high | primeiro threshold retorna |

Para um early return:

1. o evento do branch alcançado recebe `terminated=true`;
2. `termination.kind` e `return` ou `raise`, e aponta para `rule_id` e
   `sequence` do evento;
3. regras posteriores da mesma invocacao ficam ausentes de `events`;
4. com `coverage_complete=true`, a cobertura derivada marca essas regras como
   `unreached`/`not_reached_due_to_termination`, sem criar eventos
   `evaluated=false`;
5. com `coverage_complete=false`, regras posteriores ficam
   `unknown/unobserved`, sem entrar em `unreached_rule_ids`;
6. um retorno final por fall-through pode apontar para o ultimo evento avaliado,
   com nota explicando que a condicao falsa liberou o retorno final.

Caso uma condicao lance excecao antes de produzir booleano, o evento e:

```json
{
  "reached": true,
  "evaluated": false,
  "matched": null,
  "terminated": true,
  "termination_kind": "raise"
}
```

A observabilidade deve propagar a mesma excecao e nao fabricar decisao,
limitation, evento posterior ou resultado de fallback.

## 5. Convencao de rule IDs

Formato canonico:

```text
<component>.<function>.<stable_semantic_name>
```

Regras:

1. usar lowercase snake case depois dos pontos;
2. o prefixo `classify_asset.` identifica o closure observado;
3. o nome descreve o predicado/efeito, nao um valor de penalidade;
4. nao usar numero de linha como identidade;
5. `source_locator` deve carregar path relativo, funcao, line_start e `source_sha`;
6. renomear uma regra somente quando a semantica mudar; mover linhas nao exige
   novo ID;
7. um rule ID nao declara duplicidade, shadowing, prioridade futura ou budget;
8. regras desativadas devem ser removidas somente em uma mudanca de schema
   documentada, nunca silenciosamente.

Exemplo de registro:

```json
{
  "rule_id": "classify_asset.confidence_below_65",
  "source_locator": {
    "path": "advisor/scoring.py",
    "line_start": 198,
    "source_sha": "20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa"
  },
  "function": "classify_asset",
  "branch_signature": "if decision_confidence_score < 65",
  "axis": "decision",
  "effect_type": "cap",
  "evidence_keys": ["decision_confidence.score"]
}
```

## 6. Convencao de evidence keys

Formato:

```text
<domain>.<subject>.<attribute>[.<qualifier>]
```

Caracteres permitidos: lowercase ASCII, numeros e `_` dentro de cada segmento;
segmentos separados por `.`. O valor da evidence e o estado observado, nao uma
penalidade.

Namespaces iniciais:

| namespace | exemplos | origem observada |
|---|---|---|
| `asset` | `asset.type`, `asset.theme` | `AssetSnapshot` |
| `backtest` | `backtest.sample_quality`, `backtest.sample_size`, `backtest.win_rate_2r`, `backtest.expected_value_r` | `BacktestStats` |
| `market` | `market.regime`, `market.liquidity`, `market.session` | alerts, freshness e metadata |
| `fundamentals` | `fundamentals.earnings_status`, `fundamentals.market_cap`, `fundamentals.market_cap_status` | status/observacao materializados |
| `technical` | `technical.price_stale`, `technical.recent_gap` | evidencias factuais de freshness/gap |
| `data_quality` | `data_quality.missing_severity`, `data_quality.score`, `data_quality.limitation` | helpers de quality |
| `decision` | `decision_confidence.score`, `decision.base_score_inputs` | scores/evidencias de decisao |
| `risk` | `risk.position_too_small`, `risk.plan_source` | `RiskPlan` ja produzido |
| `event` | `event.earnings_status`, `event.earnings_imminent` | `event_check_status` e alerts |
| `news` | `news.confirmed_status`, `news.market_effect`, `news.confidence` | campos consumidos de news |
| `crypto` | `crypto.flow.funding`, `crypto.flow.open_interest`, `crypto.flow.liquidation` | alert/limitation materializado |
| `provider` | `provider.news.status`, `provider.data_source`, `provider.mixed_data` | status sem endpoint raw |

Exemplos de evidence keys validas:

```text
backtest.sample_quality
backtest.sample_size
backtest.expected_value_r
market.regime
market.liquidity
fundamentals.earnings_status
data_quality.missing_severity
crypto.flow.funding
crypto.flow.open_interest
provider.news.status
```

Tabela de correcoes factuais:

| key corrigida | fonte real | uso permitido |
|---|---|---|
| `decision_confidence.score` | `decision_confidence_score` calculado em `_decision_confidence_score` | evidencia numerica que pode ser referenciada pelo cap de decisao |
| `fundamentals.market_cap` | `AssetSnapshot.fundamentals.market_cap`, materializado no score | valor factual; nao nomeia o override |
| `fundamentals.market_cap_status` | presenca/ausencia ou status do market cap | status factual quando o valor numerico nao puder ser publicado |
| `data_quality.missing_severity` | `_missing_data_severity(limitations)` | severidade observada, sem afirmar qual cap sera aplicado |
| `technical.price_stale` | `freshness.is_stale`/`stale_price_data` | fato de freshness que pode ser usado por mais de uma regra |
| `technical.recent_gap` | alert `recent_gap_risk` produzido upstream | fato de gap, nao decisao final |
| `provider.news.status` | campos de news/status efetivamente consumidos | provenance factual sem payload raw |

Nao usar `confidence.cap`, `fundamentals.market_cap_gate` ou
`technical.technical_unvalidated`: os tres nomes descrevem politica, gate ou
estado resultante, e nao a evidencia factual.

Uma evidence key:

- identifica a evidencia observada;
- nao define penalidade, resultado, decisao, ticker ou valor dinamico;
- nao impede reaplicacao;
- nao declara duplicidade ou shadowing;
- nao implementa ledger ou budget;
- deve ser acompanhada de `provenance` quando a origem for material.

A mesma evidence key factual pode ser referenciada por varias regras e por
varios events. O serializer nao deduplica essas referencias: a multiplicidade
e preservada quando faz parte do trace runtime.

`not_implemented`, `not_configured`, `unavailable` e erro real sao valores de
status/provenance e devem permanecer semanticamente separados. A ausencia de
uma capacidade nao deve ser promovida automaticamente a falha critica.

## 7. API de instrumentacao

### 7.1 Opcoes

| opcao | forma | compatibilidade | risco principal |
|---|---|---|---|
| A | `classify_asset(scored, stats, trace_collector=None)` | callers posicionais continuam funcionando, mas a API publica muda | collector pode vazar para regras e a assinatura passa a ser contrato de producao |
| B | `classify_asset(scored, stats, observability_context=None)` | metadata centralizada, mas API publica muda | contexto tende a acumular politica e acoplar scoring a runtime |
| C | `classify_asset_with_trace(scored, stats) -> tuple[AssetDecision, RuntimeTrace]`, mantendo legacy | legacy fica estavel | duas entradas publicas podem divergir se a implementacao for duplicada |
| D | helper privado retorna resultado e trace; wrapper legacy chama o helper | assinatura publica atual permanece | exige disciplina para o helper ser a unica implementacao |

### 7.2 Recomendacao: D com public adapter C sobre a mesma implementacao

Na fase de implementacao futura:

```python
def classify_asset(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    effective_now_utc: datetime | None = None,
) -> AssetDecision:
    effective_now = effective_now_utc or clock.now_utc()
    decision, _trace = _classify_asset_observed(
        scored,
        backtest_stats,
        effective_now_utc=effective_now,
        observation_context=ObservationContext.disabled(effective_now),
    )
    return decision


def classify_asset_with_trace(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    effective_now_utc: datetime | None = None,
    observation_context: ObservationContext,
) -> tuple[AssetDecision, RuntimeTrace]:
    effective_now = effective_now_utc or clock.now_utc()
    return _classify_asset_observed(
        scored,
        backtest_stats,
        effective_now_utc=effective_now,
        observation_context=observation_context,
    )


def _classify_asset_observed(
    scored: ScoredAsset,
    backtest_stats: BacktestStats | None,
    *,
    effective_now_utc: datetime,
    observation_context: ObservationContext,
) -> tuple[AssetDecision, RuntimeTrace | None]:
    # uma unica implementacao do caminho atual, com hooks observacionais
    pass
```

Quando `effective_now_utc` nao e fornecido, `clock.now_utc()` pode ser lido uma
unica vez no inicio da classificacao; nenhum helper pode consultar o relogio
novamente. O valor capturado e passado a `_market_session` e aos helpers de
freshness, entra em `classification_inputs` e no `deterministic_payload`, e e o
mesmo valor usado pelos dois adapters nos testes de equivalencia.

`classify_asset` continua sendo o adapter legacy. `classify_asset_with_trace`
executa o classifier exatamente uma vez e retorna a decisao mais o trace da
mesma execucao. Ambos delegam ao mesmo `_classify_asset_observed`; nao existe
um caminho paralelo, simulador, reconstrução posterior ou copia da
logica. O `ObservationContext.disabled(effective_now)` impede caller de producao de
acessar o helper privado.

O pseudocontrato acima nao autoriza editar codigo nesta fase. Ele define a
fronteira para 3A.3.1. A adicao de `effective_now_utc` e somente uma proposta
futura, keyword-only e opcional, para preservar callers posicionais atuais.

Justificativa:

- risco de comportamento: menor, porque o wrapper legacy continua retornando
  somente `AssetDecision` e o helper e a unica implementacao;
- callers: `cli`, `audit` e todos os testes atuais continuam usando a forma
  legacy; novos testes usam somente o adapter publico com trace;
- testes: podem executar o caminho sem observer e o caminho instrumentado com os
  mesmos objetos;
- determinismo: o observer recebe snapshots do estado ja calculado, sem chamar
  novamente predicates ou providers;
- branches reais: hooks ficam na unica implementacao do classifier, inclusive em
  no-ops e overrides;
- manutencao: schema/serializer fica separado de scoring; nao ha rules engine,
  DSL ou duplicacao de `classify_asset`.

Regras obrigatorias de implementacao futura:

1. nao chamar um predicate duas vezes para observa-lo;
2. nao ordenar ou deduplicar listas de entrada in-place;
3. nao obter uma segunda hora do sistema para gerar o trace;
4. erro do observer nao pode alterar ou esconder erro do classifier;
5. observer desabilitado deve executar o mesmo caminho de decisao;
6. o trace deve ser opcional e o custo de serializacao deve ficar fora do
   critical path quando desabilitado.

## 8. Schema versionado do trace

### 8.1 Estrutura top-level proposta

O schema inicial e `1.0` e usa um artifact agregado por run:

```json
{
  "trace_schema_version": "1.0",
  "rule_catalog_version": "1.0",
  "rule_catalog_hash": "sha256:example-catalog-hash",
  "rule_catalog_entry_count": 97,
  "rule_catalog": [
    {
      "rule_id": "classify_asset.confidence_below_65",
      "source_locator": {
        "path": "advisor/scoring.py",
        "function": "classify_asset",
        "line_start": 198,
        "source_sha": "20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa"
      },
      "function": "classify_asset",
      "branch_signature": "if decision_confidence_score < 65",
      "axis": "decision",
      "effect_type": "cap",
      "evidence_keys": ["decision_confidence.score"]
    }
  ],
  "run": {
    "run_id": "example-run-001",
    "report_date": "2026-07-19",
    "schedule": "main",
    "source_sha": "20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa",
    "runtime_sha": null,
    "runtime_sha_status": "unavailable",
    "timezone": "America/Sao_Paulo",
    "asset_count": 1
  },
  "assets": [
    {
      "trace_schema_version": "1.0",
      "trace_id": "content-derived-stable-id",
      "symbol": "EXAMPLE",
      "asset_type": "stock",
      "report_date": "2026-07-19",
      "schedule": "main",
      "source_sha": "20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa",
      "runtime_sha": null,
      "trace_status": "complete",
      "observer_enabled": true,
      "coverage_complete": true,
      "last_reliable_sequence": 1,
      "active_invocation_id": null,
      "last_persisted_event_sequence": 1,
      "observation_failure_sequence": null,
      "observation_errors": [],
      "classification_inputs": {
        "effective_now_utc": "2026-07-19T21:00:00Z",
        "scored_asset": {
          "investment_quality_score": 70.0,
          "swing_trade_score": 72.0,
          "risk_plan": {
            "entry": 100.0,
            "stop": 96.0,
            "target_2r": 108.0,
            "target_3r": 112.0,
            "per_unit_risk": 4.0,
            "risk_amount": 250.0,
            "risk_fraction": 0.005,
            "max_position_units": 62,
            "max_position_value": 6200.0,
            "risk_reward_2r": "2.00:1",
            "alerts": [],
            "position_size_display": "62"
          },
          "alerts_before": ["market_not_risk_on"],
          "limitations_before": ["macro_not_collected_confidence_limited"],
          "thesis": "allowlisted thesis text",
          "metrics_summary": ["allowlisted metric text"],
          "ideal_entry": 100.0,
          "alternative_entry": 97.0,
          "hold_suggestion": "1-8 semanas"
        },
        "asset_snapshot": {
          "symbol": "EXAMPLE",
          "asset_type": "stock",
          "theme": "software",
          "last_candle_date": "2026-07-18",
          "data_timestamp": "2026-07-18T21:00:00Z",
          "data_source": "allowlisted-provider-name",
          "cache_age_seconds": 3600,
          "event": {"days_to_earnings": 30},
          "news_events": [
            {
              "news_event_type": "earnings",
              "confirmed_status": "confirmed",
              "already_priced": "unclear",
              "market_effect": "neutral",
              "news_confidence": "medium"
            }
          ]
        },
        "backtest_stats": {
          "setup_quality": "high",
          "sample_size": 120,
          "win_rate_2r": 0.62,
          "expected_value_r": 0.5,
          "median_days_to_2r": 8,
          "avg_win_r": 2.1,
          "avg_loss_r": -1.0
        },
        "sample_quality": "high",
        "missing_data_severity_before": "medium",
        "data_quality_score_before": 75,
        "decision_confidence_score_before": null
      },
      "classification": {
        "base_decision": "tradeable",
        "initial_state": {
          "decision": "tradeable",
          "max_decision": "tradeable",
          "data_quality": "limited",
          "missing_data_severity": "medium",
          "decision_confidence_score": 70,
          "alerts": ["market_not_risk_on"],
          "limitations": ["macro_not_collected_confidence_limited"],
          "reason_codes": []
        },
        "invocations": [
          {
            "invocation_id": "classify_asset#1",
            "function": "classify_asset",
            "parent_invocation_id": null,
            "call_ordinal": 1,
            "started_sequence": 1,
            "completed_sequence": 1,
            "termination": {
              "kind": "completed",
              "rule_id": null,
              "sequence": 1
            },
            "termination_rule_id": null,
            "coverage_status": "complete",
            "coverage_complete": true,
            "invocation_coverage_complete": true,
            "last_reliable_sequence": 1,
            "observation_failure_sequence": null,
            "reached_rule_ids": ["classify_asset.confidence_below_65"],
            "catalog_rule_ids": ["classify_asset.confidence_below_65"],
            "known_unreached_rule_ids": [],
            "unreached_rule_ids": [],
            "unknown_rule_ids": []
          }
        ],
        "events": [
          {
            "sequence": 1,
            "invocation_id": "classify_asset#1",
            "rule_id": "classify_asset.confidence_below_65",
            "reached": true,
            "source_locator": {
              "path": "advisor/scoring.py",
              "function": "classify_asset",
              "line_start": 198,
              "source_sha": "20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa"
            },
            "terminated": false,
            "termination_kind": null,
            "axis": "decision",
            "effect_type": "cap",
            "evidence_keys": ["decision_confidence.score"],
            "condition_inputs": {
              "decision_confidence_score": 70
            },
            "evaluated": true,
            "matched": false,
            "state_changes": {
              "decision": {
                "before": "tradeable",
                "candidate": null,
                "after": "tradeable",
                "changed": false
              }
            },
            "reason_codes_added": [],
            "alerts_added": [],
            "limitations_added": [],
            "notes": "no-op: confidence was not below threshold"
          }
        ],
        "final_state": {
          "decision": "tradeable",
          "max_decision": "tradeable",
          "data_quality": "limited",
          "missing_data_severity": "medium",
          "decision_confidence_score": 70,
          "alerts": ["market_not_risk_on"],
          "limitations": ["macro_not_collected_confidence_limited"],
          "reason_codes": ["market_not_risk_on", "macro_not_collected_confidence_limited"]
        },
        "final_decision": "tradeable",
        "final_reason_codes": ["market_not_risk_on", "macro_not_collected_confidence_limited"],
        "final_alerts": ["market_not_risk_on"],
        "final_limitations": ["macro_not_collected_confidence_limited"],
        "data_quality": "limited",
        "missing_data_severity": "medium",
        "data_quality_score": 75,
        "decision_confidence_score": 70,
        "serialized_asset_decision_hash": "sha256:example-decision-hash"
      },
      "runtime_metadata": {
        "trace_started_at": "2026-07-19T21:00:00.100Z",
        "trace_completed_at": "2026-07-19T21:00:00.110Z",
        "classification_status": "completed",
        "serialization_status": "complete",
        "exception_type": null
      }
    }
  ],
  "errors": [],
  "integrity": {
    "artifact_status": "complete",
    "asset_count": 1,
    "decision_counts": {"tradeable": 1},
    "trace_hash": "sha256:example-trace-hash",
    "source_sha": "20aa3ed7f5bd2588104dc80ea2ec3d2a87cad6aa",
    "runtime_sha": null
  }
}
```

O exemplo e ilustrativo; os valores nao sao uma fixture nem uma decisao real.
Para manter o exemplo legivel, `rule_catalog` mostra uma entrada
representativa; o artifact real deve conter as 97 entradas unicas e calcular o
`rule_catalog_hash` sobre a lista completa.

### 8.2 Semantica dos campos

Campos obrigatorios por ativo:

```text
trace_schema_version
trace_id
symbol
asset_type
report_date
schedule
source_sha
runtime_sha
trace_status
observer_enabled
coverage_complete
last_reliable_sequence
active_invocation_id
last_persisted_event_sequence
observation_failure_sequence
observation_errors
classification_inputs
classification
runtime_metadata.trace_started_at
runtime_metadata.trace_completed_at
```

`classification_inputs` deve conter os allowlists da secao 3, alerts e
limitations pre-classificacao, wrappers de feature/status realmente relevantes,
sample quality, scores quando ja estiverem disponiveis e
`effective_now_utc`. A hora efetiva e parte do input deterministico; os
timestamps `trace_started_at`/`trace_completed_at` sao somente metadata runtime.

O artifact tem um `rule_catalog` estatico com 97 entradas unicas e uma lista de
`events` runtime por ativo. O catalogo e incluido uma vez por run; os eventos
nao repetem a entrada completa do catalogo e existem somente para regras
alcancadas por uma invocacao real. Uma regra ausente de `events` somente
significa `unreached` quando `observer_enabled=true`, `trace_status=complete` e
`invocation_coverage_complete=true`; com observer desabilitado ou em qualquer
trace partial/failed significa `unknown/unobserved`.

Cada event deve conter, no minimo, `sequence`, `invocation_id`, `rule_id`,
`reached`, `evaluated`, `matched`, `terminated`, `termination_kind`, `axis`,
`effect_type`, `evidence_keys`, `condition_inputs`, `state_changes`,
`reason_codes_added`, `alerts_added` e `limitations_added`. `reached` e sempre
`true` para um evento materializado. `evaluated=false` somente representa uma
excecao real depois que o ponto foi alcancado e antes de a condicao produzir um
booleano; nesse caso `matched=null`, `terminated=true`,
`termination_kind=raise`, e a excecao original continua sendo propagada.

`initial_state` e `final_state` sao os unicos snapshots completos por ativo.
Cada evento leva apenas `state_changes`, com `before`, `candidate`, `after` e
`changed` para os eixos relevantes. Um evento `matched=true` pode ter
`changed=false` quando o candidato nao supera o estado atual; esse no-op e
auditavel sem repetir o snapshot completo. `condition_inputs` e referencias de
evidencia contem somente os campos allowlisted realmente usados.

Cada invocacao tem `invocation_id` deterministico, sequencias globalmente
crescentes no ativo e `termination`. Com `coverage_complete=true`,
`catalog_rule_ids`, `reached_rule_ids` e `unreached_rule_ids` formam uma
particao coerente do catalogo aplicavel, respeitando a ordem e o early return.
Com `coverage_complete=false`, `catalog_rule_ids` nao pode ser dividido
integralmente entre reached e unreached: use `known_unreached_rule_ids` e
`unknown_rule_ids`. Em early return parcial, regras posteriores ficam ausentes
dos eventos e permanecem `unknown/unobserved`, sem eventos artificiais.

`base_decision` e a saida de `_base_decision` ou `blocked` quando o gate inicial
de dados bloqueia antes do base. `final_decision` e somente a decisao retornada
no `AssetDecision` original.

### 8.3 Status do asset trace

O contrato de cada asset e:

```json
{
  "trace_status": "complete",
  "observer_enabled": true,
  "coverage_complete": true,
  "last_reliable_sequence": 42,
  "active_invocation_id": null,
  "last_persisted_event_sequence": 42,
  "observation_failure_sequence": null,
  "observation_errors": []
}
```

Semantica:

- `complete`: todas as invocations iniciadas foram cobertas; ausencia de event
  pode ser interpretada conforme o catalogo aplicavel;
- `partial`: a decisao existe, parte do trace foi coletada e existem regras ou
  invocations `unknown/unobserved`;
- `failed`: nao foi possivel produzir trace confiavel; nao inferir reached,
  matched ou unreached alem do que estiver explicitamente registrado como
  confiavel.

O status agregado deriva dos assets: `complete` exige todos os assets completos;
`partial` ocorre quando pelo menos um asset e parcial ou tem
`serialization_error`, sem requisito fatal do artifact; `failed` significa que
o artifact nao pode ser produzido de maneira auditavel.

## 9. Determinismo e integridade

### 9.1 Separacao

O artifact deve separar:

- `deterministic_payload`: `effective_now_utc`, inputs allowlisted, catalog
  hash, invocations, rule events, estados, decisions, reason codes e
  provenance deterministica;
- `runtime_metadata`: `trace_started_at`, `trace_completed_at`, duracao,
  observer enabled, status de erro e identificadores do ambiente que nao sao
  parte da decisao.

`trace_hash` e o SHA-256 do JSON canonico de `deterministic_payload` somente.
Inclui a hora efetiva usada pelo classifier, mas nao inclui timestamps de
observacao, duracao, PID, diretorio temporario, ID aleatorio, caminho absoluto,
mensagem de ambiente ou stack trace.

### 9.2 Normalizacao

- timestamps: ISO-8601 com timezone explicito, normalizados para UTC e sufixo
  `Z`; `report_date` deriva de BRT;
- floats na equivalencia e no payload deterministico: representacao lossless,
  por exemplo `float.hex()` ou forma decimal exata documentada; nao arredondar,
  truncar ou converter para duas casas;
- floats na apresentacao: formatacao humana separada, sem reutilizar o valor
  formatado para hash ou comparacao;
- enums: strings lowercase conforme os valores atuais; valor desconhecido vira
  `unknown` somente quando ja for o contrato do modelo, nunca como fallback de
  uma evidencia ausente;
- listas: manter ordem e duplicatas por padrao; ordenar somente os campos
  explicitamente declarados como unordered no allowlist do serializer;
- dicts: chaves ordenadas lexicograficamente; nenhum `set` generico ou lista
  "set-like" pode ser normalizado por convencao implícita;
- paths: somente caminhos relativos ao repositorio, com `/`; paths absolutos
  sao rejeitados;
- nulls: preservados como `null`; nao substituir por zero, string vazia ou
  valor estimado;
- NaN/infinity: rejeitados pelo serializer ou convertidos para `null` somente
  junto com um status explicito `non_finite_unavailable`; nunca serializar como
  string ou numero;
- timezone: uma unica representacao UTC para timestamps; timezone BRT somente
  para `report_date` e campos de apresentacao;
- line endings: UTF-8, LF, newline final unico;
- `trace_id`: derivado deterministicamente de `source_sha`, report date,
  schedule, symbol e payload canonico; nao usar UUID aleatorio no hash;
- serializacao: JSON sem espacos dependentes de runtime, com chaves ordenadas e
  `ensure_ascii=false` equivalente documentado.

### 9.3 Hashes

Devem existir dois hashes distintos, com responsabilidades distintas:

1. `serialized_asset_decision_hash`: SHA-256 da serializacao canonica
   lossless e explicitamente allowlisted de `AssetDecision`, incluindo todos os
   campos retornados, listas na ordem e com duplicatas, `null` e enums;
2. `integrity.trace_hash`: SHA-256 do `deterministic_payload` canonico do
   trace, incluindo `effective_now_utc`, inputs, invocations, events, decisao
   final, decision hash, catalog hash, source/runtime/schema e provenance
   permitida.

O hash de decisao deve ser igual entre `classify_asset` e
`classify_asset_with_trace` para os mesmos inputs e `effective_now_utc`. O
`trace_hash` deve ser igual quando inputs, hora efetiva, codigo e schema forem
iguais. Nunca comparar `serialized_asset_decision_hash` com `trace_hash`: eles
nao precisam e nao devem ser iguais.

### 9.4 Tres serializers obrigatorios

Os tres contratos nao podem ser substituidos por um normalizador generico:

| serializer | finalidade | regra principal |
|---|---|---|
| `AssetDecision` equivalence serializer | provar paridade do resultado | lossless, todos os campos, sem arredondar, deduplicar ou ordenar listas por conveniencia |
| Trace deterministic serializer | produzir `deterministic_payload` e `trace_hash` | chaves de dict ordenadas, events por `sequence`, assets por ordem deterministica, invocations por `started_sequence`, floats lossless, paths relativos e UTC normalizado |
| Artifact presentation serializer | leitura humana e renderizacao | pode formatar floats e textos, mas conserva o valor canonico quando precisar sustentar auditoria |

Somente uma allowlist declarada pode marcar um campo como unordered. Todos os
demais arrays preservam ordem e duplicatas. O serializer de equivalencia deve
representar `null` e enums explicitamente e rejeitar campos omitidos ou
desconhecidos.

## 10. Seguranca, privacidade e allowlist

### 10.1 Proibicoes

Nao serializar:

- secrets, API keys, tokens, cookies ou auth headers;
- URLs completas de provider com query string ou fragmento;
- caminhos pessoais absolutos, nomes de usuario ou diretórios temporarios;
- resposta completa de provider, headlines, payloads raw ou bodies HTTP;
- variaveis de ambiente;
- exception message completa, stack trace ou request dump;
- campos de modelo que nao estejam na allowlist do classifier;
- `vars(obj)`, `obj.__dict__` ou `dataclasses.asdict(obj)` para objetos de
  scoring quando isso puder incluir campos nao auditados.

### 10.2 Serializer explicito

Cada tipo deve ter uma funcao de serializacao explicita, por exemplo:

```text
serialize_scored_asset_inputs(scored)
serialize_snapshot_classifier_inputs(snapshot)
serialize_backtest_classifier_inputs(stats)
serialize_risk_plan(plan)
serialize_asset_decision(decision)
serialize_trace_event(event)
serialize_asset_decision_equivalence(decision)
serialize_trace_deterministic(trace)
serialize_artifact_presentation(artifact)
```

Os tres serializers devem rejeitar chaves desconhecidas em payloads controlados
e preservar `null`. O serializer de equivalencia e o deterministico sao
lossless; nao podem aplicar a regra generica de duas casas, deduplicar listas ou
reordenar arrays. Ordenacao e permitida somente para os campos explicitamente
marcados como unordered na allowlist.

### 10.3 Sanitizacao

- provider URL: armazenar apenas provider e endpoint path allowlisted, removendo
  query e fragmento; se nao for possivel sanitizar, armazenar somente status;
- exception: armazenar `exception_type` e um `error_code` interno allowlisted;
  nunca mensagem ou argumentos;
- path: converter para locator relativo e verificar que nao contem drive letter,
  prefixo UNC ou segmento acima da raiz;
- strings de news/thesis/metrics: aplicar limite de tamanho e lista de campos;
  nao guardar texto provider raw;
- segredo detectado em qualquer valor: falhar fechado e nao escrever o artifact;
- arquivos parciais: escrever em temporario dentro de `reports/runtime/` e
  renomear atomicamente somente depois da validacao do schema e do hash.

## 11. Artifact agregado por run

### 11.1 Nome e granularidade

Preferencia: um artifact agregado por run, nao um arquivo por ativo.

Formato normal obrigatorio:

```text
reports/runtime/scoring-runtime-trace.json.gz
```

O nome logico do run, `report_date` BRT, `schedule` (`main`, `close` ou
`nightly`) e `run_id` ficam em `run`; nao podem trocar o formato normal nem
criar um arquivo por ativo. Para preservar historico, o caller pode copiar o
mesmo conteudo para storage versionado fora do contrato local, sem alterar o
payload.

O conteudo logico obrigatorio e:

```text
trace_schema_version
run
rule_catalog_version
rule_catalog_hash
rule_catalog
assets
errors
integrity
```

`rule_catalog` aparece uma vez por run. Os assets sao ordenados por `symbol`,
depois `asset_type`, e cada asset leva somente events de regras alcancadas. O
artifact inclui todos os ativos classificados daquele run, inclusive os que
terminam em `avoid`, `wait`, `blocked` ou `technical_unvalidated`. O ramo
`_unscorable_decision` deve ser distinguido no resultado do scan e nao pode ser
falsamente apresentado como execucao de `classify_asset`.

Gzip e obrigatorio e deterministico: usar `mtime` fixo (zero), nome de arquivo
e ordem de membros fixos, JSON UTF-8/LF canonico e os mesmos bytes de entrada
para o mesmo payload. A extensao `.json.gz` nao autoriza comprimir um JSON
incompleto ou truncado.

### 11.2 Erros por ativo e status do artifact

O collector deve separar decisao valida de erro de observabilidade:

| tipo | comportamento da decisao | comportamento do trace/artifact |
|---|---|---|
| `classification_error` | preservar o comportamento do caller; nao engolir excecao, criar fake decision ou fazer fallback | se o caller isolar o ativo, registrar somente tipo/codigo/simbolo/invocacao sanitizados e marcar erro; caso contrario propagar a mesma excecao |
| `trace_collection_error` | classificacao continua normalmente; `AssetDecision` valida permanece inalterada | registrar ultima sequence confirmada, invocation ativa, ultimo event completamente persistido e `observation_failure_sequence`; asset `trace_status=partial`, `coverage_complete=false`, `collector_status=error`, erro sanitizado e artifact `partial` |
| `serialization_error` | decisao e report permanecem inalterados | marcar o asset, artifact `partial`, continuar os demais assets quando seguro; nao publicar trace falso como completo |
| falha do writer/schema/gzip | decisao e reports permanecem inalterados | artifact `failed`, sem publicar um `complete`; conservar razao sanitizada |

Os unicos status de artifact sao `complete`, `partial` e `failed`. `errors` e
uma lista sanitizada por run; mensagens, stack traces, secrets, paths absolutos
e payloads raw ficam fora. `integrity.artifact_status` deve concordar com o
status real da escrita.

Quando `trace_collection_error` ocorre durante a execucao, o codigo decisorio
pode continuar executando. Branches posteriores podem ser executados sem serem
observados; suas regras ausentes ficam `unknown/unobserved`, nao entram em
`unreached_rule_ids` e nao recebem `matched=false` artificialmente. A
`AssetDecision` final continua valida e inalterada. Se a implementacao expuser
`trace_status=error` como estado transitório do collector, ele tem a mesma
semantica de cobertura parcial; o status final serializado do asset deve ser
`partial` ou `failed`.

Terminologia obrigatoria:

- `unreached`: existe evidencia de cobertura completa de que o fluxo nao chegou
  ao branch;
- `unobserved`: o collector nao possui evidencia suficiente para afirmar se o
  branch foi executado;
- `matched=false`: existe event confirmando que o branch foi alcancado e a
  condicao foi falsa.

### 11.3 Budget, warning e chunking deterministico

Defaults do contrato:

- soft budget: 25 MiB descomprimidos;
- hard budget: 25 MiB comprimidos por arquivo.

Antes de ultrapassar o soft budget, emitir warning no metadata/erro sanitizado,
sem remover events, evidencias, invocations ou alterar qualquer decisao. Se o
arquivo comprimido ultrapassar o hard budget, o writer deve fazer chunking
deterministico, nunca truncamento:

```text
reports/runtime/scoring-runtime-trace.index.json
reports/runtime/scoring-runtime-trace.part-0001.json.gz
reports/runtime/scoring-runtime-trace.part-0002.json.gz
reports/runtime/scoring-runtime-trace.part-NNNN.json.gz
```

O index contem schema, run, catalog hash, status, partes, hashes, tamanhos e
simbolos cobertos. Cada parte fica abaixo de 25 MiB comprimidos e preserva
ordem de assets/eventos; o catalogo aparece no index e e referenciado pelas
partes, sem duplicar 97 eventos por asset. Se um unico asset nao couber no hard
budget mesmo isolado, o artifact fica `failed` com razao sanitizada e a decisao
permanece inalterada. Nao existe politica de remover eventos para caber.

Escala estimada:

```text
O(assets * invocacoes_reais_por_asset * branches_reached)
```

Nao usar `97 * assets` como estimativa de eventos. O catalogo e unico por run;
invocacoes repetidas, eventos alcancados, deltas de estado, inputs compartilhados
e gzip/chunking deterministico controlam o custo sem materializar regras
inalcancadas.

### 11.4 Integridade do artifact

`integrity` deve conter:

```text
asset_count
decision_counts
trace_hash
source_sha
runtime_sha
schema_validation_status
artifact_status
gzip_deterministic
soft_budget_bytes
hard_budget_bytes
chunk_count
```

`decision_counts` e descritivo e deriva de `final_decision`; nao e uma politica
de gate. O artifact nao deve ser considerado operationally valid apenas por
existir: selecao futura deve verificar SHA, data BRT, schema, catalog hash,
partes, hashes e status. Uma falha de trace jamais substitui a decisao ou o
report ja produzidos.

## 12. Matriz de compatibilidade dos callers

| caller/consumer | chamada atual | contrato futuro | mudanca nesta fase |
|---|---|---|---|
| `advisor.cli._scan` | `classify_asset(scored, stats)` | recebe a mesma `AssetDecision` | nenhuma |
| `advisor.audit._trace_gates` | mesma assinatura | continua auditoria independente; nao deve ler trace para decidir | nenhuma |
| `tests/test_hardening.py` | chamadas diretas e comparacoes de campos | mesma API; adicionar testes de paridade depois | nenhuma |
| `tests/test_scoring_regime_report.py` | chamadas diretas | mesma API; fixture controlada depois | nenhuma |
| `advisor.report` | recebe `list[AssetDecision]` | continua consumindo decisao; trace nao altera report | nenhuma |
| `advisor.cache._signal_row` | recebe `AssetDecision` | continua persistindo colunas atuais; trace separado | nenhuma |
| `advisor.analyst_review` | parseia Markdown | preserva artifact, nao interpreta trace v1 | nenhuma |
| `advisor.telegram_notify` | le report/Final Review | pode receber referencia/hash, nunca nova decisao | nenhuma |
| `classify_asset_with_trace` | inexistente hoje | adapter publico futuro; uma execucao, mesma decisao e `RuntimeTrace` | somente em 3A.3.1 |
| `_classify_asset_observed` | inexistente hoje | helper privado unico chamado pelos dois adapters | somente em 3A.3.1; nenhum caller direto |
| runtime artifact writer | inexistente hoje | recebe resultados do collector, serializa, valida e escreve gzip/chunks | somente em 3A.3.2 |
| main/close scripts | executam CLI | futuramente agregam artifact por run | nenhuma |
| nightly workflow | baixa e publica artifacts | futuramente preserva trace por SHA/data | nenhuma |

`classify_asset` continua retornando somente `AssetDecision`; nenhum caller
existente deve mudar de assinatura para receber trace. A API futura
`classify_asset_with_trace` e um adapter fino e explicito sobre
`_classify_asset_observed`, sem segunda implementacao, copia, reconstrucao ou
um caminho paralelo. O helper privado nao entra em nenhum caller matrix como
entrada permitida.

## 13. Plano de testes de equivalencia decisoria

As fixtures abaixo sao contratos a criar em fase posterior; nao sao criadas em
3A.3.0.2.

Para cada fixture `F`:

```python
effective_now_utc = F.effective_now_utc
result_without_trace = classify_asset(
    F.scored,
    F.backtest_stats,
    effective_now_utc=effective_now_utc,
)
result_with_trace, trace = classify_asset_with_trace(
    F.scored,
    F.backtest_stats,
    effective_now_utc=effective_now_utc,
    observation_context=F.observation_context,
)

assert serialize_asset_decision_equivalence(result_without_trace) == serialize_asset_decision_equivalence(result_with_trace)
assert serialized_asset_decision_hash(result_without_trace) == trace.classification.serialized_asset_decision_hash
assert trace.classification_inputs.effective_now_utc == effective_now_utc
```

Quando `AssetDecision.__eq__` for suficiente, ele pode ser uma assercao
adicional; a comparacao deve cobrir o objeto inteiro e o serializer lossless,
incluindo listas ordenadas, duplicatas, `null`, enums, scores, sizing, risk e
campos de short. O hash canonico continua obrigatorio para detectar campos
omitidos.

### 13.1 Matriz minima de fixtures

| fixture ID | asset type | caso coberto | verificacoes adicionais |
|---|---|---|---|
| `equity_tradeable_base` | stock | base `tradeable` sem cap final | base decision e evento de no-op |
| `equity_watch_buy` | stock | `watch_buy` | cap inicial e reason codes |
| `equity_wait` | stock | `wait` | gate de regime/evento |
| `equity_avoid` | stock | `avoid` | base baixa e preservacao de risco |
| `equity_technical_unvalidated` | stock | `technical_unvalidated` | predicate tecnico e thesis |
| `crypto_tradeable_or_watch` | crypto | caminho crypto | `event_check_status=not_applicable` e campos crypto allowlisted |
| `low_sample` | stock/crypto | `sample_size < 30` | sample quality low, cap e confidence |
| `stale_data` | stock/crypto | stale por limitation/cache | stale annotation, wait cap e timestamps |
| `risk_off` | stock/crypto | regime `risk_off` | alert, confidence cap e ordem de eventos |
| `missing_severity_high` | stock | severity high | high-severity cap e data quality |
| `below_minimum_market_cap` | stock/crypto | alert de market cap minimo | override exato para `avoid` |
| `earnings_event_near` | stock | earnings <= 10 | alert e cap correspondente |
| `earnings_event_imminent` | stock | earnings <= 5 | conversao final `watch_buy -> wait` |
| `liquidity_gate` | stock/crypto | low liquidity/position too small | RiskPlan recebido e hard gate |
| `confidence_cap` | stock/crypto | `decision_confidence_score < 65` | no cap adicional quando candidato ja e mais fraco |
| `no_op_weaker_cap` | stock/crypto | cap matched mas estado ja e mais fraco | `evaluated=true`, `matched=true`, `changed=false` |
| `explicit_market_cap_override` | stock/crypto | override independente do max decision | final decision e `avoid` |
| `blocking_data_gap` | stock/crypto | limitation bloqueante | `blocked` antes de `_base_decision` |
| `news_rumor_low_confidence` | stock/crypto | rumor e confidence low | reason codes e limits sem raw news |
| `fundamental_gap_thesis` | stock | gap fundamental | thesis final preservada e hash |
| `early_return_helper` | stock/crypto | `_data_quality_score` ou `_sector_benchmark` retorna cedo | termination aponta para o evento, regras posteriores ausentes |
| `repeated_helper_invocation` | stock/crypto | helper chamado duas vezes pelo caminho real | mesmo `rule_id` preserva `invocation_id`, `sequence` e `call_ordinal` distintos |
| `real_classification_exception` | stock/crypto | predicate real lança excecao | `evaluated=false`, `matched=null`, raise propagado sem fake decision |

### 13.2 Testes de invariantes de entrada

Adicionar, em fase futura:

- verificar que o caminho instrumentado nao muta `ScoredAsset`, snapshot,
  `alerts`, `limitations`, candles ou `BacktestStats`;
- executar o adapter legacy e `classify_asset_with_trace` com o mesmo
  `effective_now_utc` e comparar custo/resultado;
- verificar todos os campos do `AssetDecision`, inclusive `RiskPlan`,
  `backtest_stats`, `bucket`, status, scores, sizing e campos de short;
- verificar que um erro do serializer nao altera o resultado retornado pelo
  classifier quando a politica de erro do artifact permitir continuar;
- verificar que erro de input permanece erro de input e nao e convertido em uma
  decisao ou em evidencia inventada.

### 13.3 Testes negativos obrigatorios

Os testes futuros devem falhar se ocorrer qualquer um destes comportamentos:

- materializar 97 events `evaluated=false` para regras inalcancadas;
- deixar a invocacao continuar depois de early return ou criar evento posterior
  artificial;
- colocar uma condicao falsa em `unreached_rule_ids`;
- omitir o event `matched=false` de
  `classify_asset.sector_semiconductors` para `theme=software`;
- inferir `unreached` pela ausencia de event em trace parcial;
- classificar regras posteriores a `trace_collection_error` como `unreached`;
- omitir `unknown_rule_ids` quando `coverage_status=partial` ou declarar
  `coverage_complete=true` em invocation partial;
- perder a identidade da invocacao quando um `rule_id` se repete;
- usar `evaluated=false` para input ausente sem uma excecao real antes do
  booleano;
- comparar `serialized_asset_decision_hash` com `trace_hash`;
- arredondar float, remover `null`, deduplicar ou reordenar lista ordenada;
- omitir o adapter publico, chamar o helper privado de um caller ou executar
  duas vezes para obter o trace;
- deixar `_market_session` ou helper temporal ler clocks diferentes ou omitir
  `effective_now_utc` do payload deterministico;
- mover `effective_now_utc` para `runtime_metadata`, colocar
  `trace_started_at` em `classification_inputs` ou incluir timestamps de
  observacao no hash decisorio;
- deixar erro de coleta/serializacao alterar a decisao;
- fazer um titulo de fase contradizer seu corpo;
- truncar artifact, omitir budget/chunking, publicar `complete` apos falha ou
  materializar evento de branch inalcancado.

## 14. Determinismo do trace

Para o mesmo conjunto de inputs e mesmo `source_sha`:

1. `deterministic_payload` deve ser byte-a-byte identico;
2. assets devem estar na mesma ordem;
3. events devem seguir a sequencia real de avaliacao;
4. `initial_state` e `final_state` sao snapshots allowlisted; cada event leva
  somente `state_changes` delta;
5. o decision hash e igual entre os dois adapters; `trace_hash` e calculado
  separadamente e nunca e comparado ao decision hash;
6. timestamps/duracao podem divergir somente dentro de `runtime_metadata`;
7. o clock usado por freshness/market session deve ser capturado uma vez e
  passado ao helper, para nao introduzir uma segunda observacao temporal;
8. `effective_now_utc` aparece em `classification_inputs` e no payload
  deterministico;
9. nenhuma ordenacao de listas de input pode mudar a semantica de uma decisao;
10. campos ausentes continuam `null`/unavailable com provenance, sem inferencia;
11. line numbers sao metadata de locator e nunca identidade de rule.

Um teste de determinismo deve executar o serializer duas vezes com o mesmo
objeto e comparar bytes, hash e listas. Outro deve variar somente os timestamps
runtime e provar que o `trace_hash` nao muda.

## 15. Integracao futura, sem implementar agora

Divisao normativa das fases futuras:

| fase | titulo | fronteira |
|---|---|---|
| 3A.3.1 | Instrumentacao local e equivalencia decisoria | `ObservationContext`, adapter publico, helper unico, invocations/events e testes; sem artifact de producao |
| 3A.3.2 | Artifact runtime local | serializers, hashes, gzip, budgets, chunking, erros e integridade local |
| 3A.3.3 | Integracao ao pipeline | main, close, nightly, metadata, upload, selecao e preservacao/referencia; Telegram sem interpretar |
| 3A.3.4 | Novo ciclo real e auditoria | execucao instrumentada, paridade, completude, provenance, sequencia runtime e prontidao para ledger |

### 15.1 3A.3.1 — Instrumentacao local e equivalencia decisoria

Objetivo: instrumentacao local e prova de equivalencia, sem escrever artifact
de producao e sem alterar pipeline/workflow. Arquivos que seriam criados ou
modificados somente nessa fase:

- `advisor/scoring.py`, somente para extrair o helper privado unico e inserir
  hooks observacionais sem alterar a logica decisoria;
- `advisor/runtime_scoring_observability.py`, para `ObservationContext`, tipos
  de invocacao/evento e collector in-memory minimo;
- testes locais para `classify_asset` versus
  `classify_asset_with_trace`, effective time, invocations, early returns,
  exceptions e ausencia de eventos inalcancados.

Nao gerar `reports/runtime/scoring-runtime-trace.json.gz`, nao integrar main,
close ou nightly e nao alterar `advisor/report.py`,
`advisor/telegram_notify.py`, workflows, Final Review ou ledger em 3A.3.1.

### 15.2 3A.3.2 — Artifact runtime local

Objetivo: implementar o artifact runtime local, ainda sem acoplar o pipeline.
Possiveis arquivos futuros:

- `advisor/runtime_scoring_observability.py`, para os tres serializers, hashes
  separados, schema, gzip deterministico, erros por ativo, integrity e
  chunking;
- testes locais de JSON, hash, gzip, soft/hard budget, index/parts e falhas
  `partial`/`failed`.

Essa fase ainda nao liga writer a main/close, nao publica artifact no nightly e
nao altera Markdown/HTML, SQLite, Telegram, Final Review ou workflow. O
artifact local deve provar que um erro de observabilidade nao altera decisao.

### 15.3 3A.3.3 — Integracao ao pipeline

Objetivo: integrar o artifact ao pipeline main/close/nightly, metadata, upload
e selecao, sem interpretar sua politica.

Pontos futuros:

- `advisor/cli.py` e scripts main/close: iniciar o collector por run, anexar
  somente resultados das classificacoes reais e escrever o formato normal;
- `.github/workflows/financial-advisor-nightly-review.yml`: garantir que o
  artifact runtime seja incluído no upload e que o metadata registre nome,
  schema, SHA e status de preservacao;
- `scripts/fetch-latest-github-reports.ps1`: localizar o artifact runtime do
  mesmo `source_head_sha` e `brt_date` do par main/close;
- `advisor/artifact_selection.py`: aceitar somente artifact nao expirado,
  mesmo SHA, mesmo dia BRT e schema suportado; rejeitar par incompleto;
- `scripts/run-nightly-analyst-review.ps1`: copiar/preservar o arquivo sem
  transformar seu conteudo;
- testes de selecao e metadata.

Main/close continuam produzindo a decisao e reports existentes. O nightly nao
deve reexecutar `classify_asset` para reconstruir o trace. Final Review somente
preserva ou referencia artifact, schema, SHA e status; Telegram nao interpreta
events, caps, duplicidade, shadowing ou recomendacao.

### 15.4 3A.3.4 — Novo ciclo real e auditoria

Objetivo: executar um novo ciclo real instrumentado e auditar paridade,
completude, provenance, sequencia runtime, early returns, erros e integridade
do artifact. Ao final, decidir com evidencia se o ledger pode comecar; ledger
nao e implementado automaticamente.

Nao criar nova implementacao de Final Review ou Telegram em 3A.3.4. A
preservacao/referencia operacional pertence a 3A.3.3; esta fase somente audita
se ela preservou a decisao e o artifact sem interpretacao.

## 16. Riscos conhecidos e mitigacoes

| risco | impacto | mitigacao especificada |
|---|---|---|
| observer reavalia predicate | ordem/resultado pode mudar | capturar o booleano uma vez; nao chamar helper duas vezes |
| listas mutaveis compartilhadas | trace pode alterar decisao | copiar antes/depois; nunca ordenar input in-place |
| leituras diferentes do relogio alteram `market_session` e quebram equivalencia | freshness/session nao deterministas | capturar `effective_now_utc` uma unica vez como input deterministico, fornece-lo aos dois caminhos e manter somente `trace_started_at`, `trace_completed_at` e duration em `runtime_metadata` |
| rule ID baseado em linha | trace quebra em refactor | ID semantico + locator com source SHA |
| dataclass ganha campo sensivel | vazamento | serializer allowlist, sem `__dict__`/`asdict` |
| provider URL com segredo | exposicao de credencial | remover query/fragmento ou rejeitar |
| exception raw | caminho/segredo no artifact | somente classe e error code |
| trace grande | custo e upload excessivos | registrar somente campos consumidos; sem candles completos/news raw |
| artifact parcial | consumidor usa evidencia incompleta | escrita atomica, schema/hash antes de publicar |
| trace lido como nova politica | decisoes diferentes | Final Review v1 apenas preserva; testes de decision immutability |
| runtime SHA indisponivel | provenance incompleta | `null` + status explicito, nunca SHA inventado |
| rerun no mesmo BRT | sobrescrita/ambiguidade | run ID no nome e selecao por SHA/date/run |
| divergencia entre main e close | comparacao incorreta | cada run tem schedule e hash proprios; nightly nao reconcilia por inferencia |

## 17. Non-goals

Fora do escopo da Fase 3A.3.0.2:

- ledger;
- penalty budget;
- deduplicacao;
- shadowed classification;
- alteracao de gates, thresholds, caps, confidence, risk ou sizing;
- produzir mais trades;
- execucao de ordens;
- broker ou integracao de corretora;
- interpretacao financeira do novo trace;
- replay do run antigo;
- alterar reports, Telegram ou workflows nesta fase;
- criar fixtures, testes de instrumentacao ou rule IDs no codigo nesta fase;
- usar o trace para aprovar candidatos ou mudar `melhor decisao`.

## 18. Plano incremental de implementacao futura

1. **3A.3.1:** instrumentacao local, `ObservationContext`, adapter publico,
   helper privado unico, invocations/events e equivalencia; nenhum artifact de
   producao ou workflow.
2. **3A.3.2:** serializers de equivalencia/deterministico/apresentacao, hashes
   separados, gzip, chunking, erros por ativo e integrity; somente testes e
   artifact local.
3. **3A.3.3:** integrar main/close/nightly, metadata, upload e selecao; Final
   Review apenas preserva/referencia e Telegram nao interpreta o trace.
4. **3A.3.4:** executar novo ciclo real e auditar paridade, completude,
   provenance e sequencia runtime; decidir se o ledger pode comecar. Nao criar
   nova implementacao de Final Review/Telegram nessa fase.
5. Cada etapa deve executar a suite relevante, comparar o decision hash de
   `AssetDecision` com trace desligado/ligado e mostrar `git diff --check` antes
   de qualquer stage futuro.

## 19. Checklist de aceite da especificacao

- [x] call graph real de acquisition -> scoring -> classify -> decision -> report;
- [x] assinatura atual e callers reais registrados;
- [x] construcao de `ScoredAsset`, `AssetSnapshot`, `BacktestStats` e
  `AssetDecision` registrada;
- [x] alerts, limitations, sample quality, severity, data quality e confidence
  mapeados;
- [x] inventario de 97 pontos condicionais da closure do classifier (82 nos
  `if`/`elif` e 15 expressoes inline), com rule IDs, paths, funcoes, linhas,
  assinaturas, axes, effects, inputs e transicoes;
- [x] `rule_catalog` estatico com 97 entradas unicas separado de events runtime
  somente alcancados, sem 97 eventos `evaluated=false` artificiais;
- [x] contrato de invocations, sequence global por ativo, repeated rule IDs e
  early returns/raises com termination e regras posteriores ausentes;
- [x] exemplo de `_sector_benchmark` com `theme=software` registra
  `sector_semiconductors` como reached/matched=false e `sector_software` como
  reached/matched=true/return, sem evento posterior artificial;
- [x] ausencia de event condicionada a observer enabled, trace complete e
  invocation coverage complete, com `unknown_rule_ids` para trace partial;
- [x] coverage status/last reliable sequence/failure sequence no invocation e
  status, errors e coverage no asset trace;
- [x] campos obrigatorios de evento: reached, evaluated, matched, terminated,
  termination kind, axis/effect, evidence, condition inputs e deltas de estado;
- [x] correcoes de catalogo para `confidence_below_65` e
  `backtest_branch_entry`, com revisao explicita de axis/effect;
- [x] fronteira upstream de scores/risk/sizing explicitada sem duplicar regras;
- [x] schema JSON versionado e fields obrigatorios propostos;
- [x] adapter publico `classify_asset_with_trace` sobre um unico helper privado,
  effective time capturado uma vez e proibicao de caller privado/double run;
- [x] evidence keys e rule IDs convencionados;
- [x] evidence keys factuais corrigidas, sem nomes de penalty/gate/resultado e
  com fontes permitidas;
- [x] quatro APIs comparadas e opcao D recomendada;
- [x] matriz de compatibilidade de callers;
- [x] matriz de fixtures e estrategia de equivalencia definidas, sem cria-las;
- [x] plano de determinismo, canonicalizacao e hashes;
- [x] tres serializers separados, floats lossless, listas ordenadas somente por
  allowlist explicita e decision hash separado de trace hash;
- [x] `initial_state`/`final_state` por ativo e `state_changes` delta por evento;
- [x] allowlist e campos proibidos;
- [x] artifact agregado por run definido;
- [x] formato `.json.gz` deterministico, catalogo uma vez, errors/integrity,
  status por ativo, soft/hard budgets de 25 MiB e chunking sem truncamento;
- [x] `trace_collection_error` preserva a decisao, marca cobertura parcial e
  nao infere branches posteriores;
- [x] escala definida por assets x invocacoes reais x branches alcancados;
- [x] integracoes 3A.3.2, 3A.3.3 e 3A.3.4 mapeadas;
- [x] Final Review explicitamente impedido de interpretar o trace v1;
- [x] arquivos que seriam alterados em 3A.3.1 listados;
- [x] fases futuras corrigidas: 3A.3.1 local, 3A.3.2 artifact local,
  3A.3.3 pipeline/preservacao e 3A.3.4 ciclo real/auditoria sem nova
  implementacao de Final Review/Telegram;
- [x] matriz de risco separa `effective_now_utc` deterministico de timestamps
  de observacao em `runtime_metadata`;
- [x] matriz de equivalencia e testes negativos cobrem early return, repeated
  helper, excecao real, tempo efetivo, erros e budgets;
- [x] riscos e non-goals registrados.

## 20. Estado desta entrega

Arquivo corrigido nesta fase:

```text
docs/PHASE3_RUNTIME_SCORING_OBSERVABILITY_SPEC.md
```

Nenhum arquivo de producao foi alterado. Em particular, `advisor/scoring.py`
permanece sem modificacao. Nenhum item untracked fora do escopo foi adicionado,
removido ou modificado. Nao houve stage, commit ou push.

Esta entrega permanece limitada a `FASE 3A.3.0.2`: correcao final de consistencia
dos contratos. Instrumentacao, ledger, artefato de producao, mudanca de gates,
reports, Telegram e workflows aguardam fases futuras e aprovacao independente.
Aguardar nova revisao independente antes de iniciar a 3A.3.1.
