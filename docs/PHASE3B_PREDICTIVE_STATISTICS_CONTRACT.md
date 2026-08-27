# FASE 3B.3.2 — Predictive Statistics & Readiness Contract

Status: implementação isolada sobre o artifact congelado da FASE 3B.3.1.

Esta fase mede se os outputs congelados apresentam comportamento futuro diferente de forma consistente e se há amostra temporal suficiente para avaliação formal. Não calibra scoring, não altera thresholds, weights ou gates e não autoriza a FASE 3B.4.

## Entrada e execução

A única autoridade é o artifact local da 3B.3.1:

```text
python -m advisor.predictive_statistics \
  --input-path <predictive-evaluation.json> \
  --output-path <predictive-statistics.json>
```

O módulo lê somente o input local e escreve somente o output local. Não lê banco, candles, ledgers, benchmark original ou providers; não usa rede; não altera `advisor/cli.py` nem os módulos congelados.

O input exige `schema_version="1.0"`, `benchmark_policy_version="1.0"` e `anchor_policy_version="first_forward_open_to_horizon_close_v1"`. Primeiro, o `artifact_hash` recebido é recomputado sobre o JSON raw, excluindo somente `artifact_hash`, e comparado com o valor fornecido; a autenticação ocorre antes de qualquer ordenação. Cada row exige `evaluation_row_id` válido e `evaluation_row_hash` igual ao SHA-256 do JSON canônico da row excluindo somente `evaluation_row_hash`. `evaluation_row_id` duplicado é input ambíguo e rejeitado. Qualquer divergência rejeita o input com exit code diferente de zero.

Depois da autenticação e da validação individual das rows, a representação semântica é canonicalizada ordenando todas as rows por `evaluation_row_id` crescente. O campo de output `input_artifact_hash` é o `semantic_input_hash`: SHA-256 do JSON canônico de `schema_version`, `benchmark_policy_version`, `anchor_policy_version`, `dataset_status`, `coverage` preservado e rows nessa ordem canonicalizada. Ele não é uma cópia literal do `artifact_hash` raw recebido; o hash raw serve somente para autenticação e não é persistido no output. Assim, apenas a ordem da lista é irrelevante: alteração de conteúdo continua alterando o hash semântico.

`dataset_status=NO_CANONICAL_SAMPLE` com `rows=[]` é válido e produz `statistics_status=INSUFFICIENT`, `cells=[]`, `calibration_authorized=false` e exit code zero.

## Policy e output

```text
schema_version            = "1.0"
statistics_policy_version = "1.0"
bootstrap_replicates      = 10000
```

O output contém `schema_version`, `statistics_policy_version`, `input_artifact_hash`, `input_dataset_status`, `statistics_status`, `overall_market_relative_readiness`, `calibration_authorized=false`, `cells` e `artifact_hash`. Não contém timestamp. Seu hash é SHA-256 do JSON canônico excluindo apenas o próprio `artifact_hash`; a gravação é UTF-8, compacta, com chaves ordenadas e uma quebra de linha final.

## Cell e validade

A cell primária é exatamente `asset_type + report_type + horizon_bars`. Não há combinação de assets, `main` com `close` ou horizons.

Para métricas absolutas, a row exige `row_status=available` e `aligned_asset_return_pct` não nulo e finito. Para métricas market-relative, exige também `primary_benchmark_status=available` e `primary_excess_aligned_price_return_pct` não nulo e finito. Secondary nunca substitui primary, cash não é benchmark e `forward_return_pct` não é excesso.

`raw_signal_count` é `distinct signal_id`; `unique_asset_date_count` é `distinct (asset, signal_market_date)`. `duplicate_analysis_unit_count` conta as chaves `(asset, signal_market_date, report_type, horizon_bars)` que aparecem em mais de uma row; a cobertura bruta mantém essas rows.

Rows com `primary_benchmark_status=self_benchmark_unavailable` são reportadas em `self_benchmark_unavailable_count`, mas ficam fora do denominador. A cell reporta `primary_eligible_count`, `primary_available_count`, seus counts distintos por asset-date e `primary_coverage=primary_available_count/primary_eligible_count`; com denominador zero, coverage é `null`. Secondary disponível não aumenta primary coverage.

## Independência temporal

Rows absolutas válidas são agrupadas por `signal_market_date`; cada unidade preserva todas as rows da data, incluindo assets e roles. O intervalo é `min(aligned_asset_start_date)` até `max(aligned_asset_end_date)`.

As unidades são ordenadas por `end_date`, `start_date`, `signal_market_date`. A seleção greedy escolhe a primeira e somente escolhe a próxima quando `next.start_date > last_selected.end_date`. A cell reporta `signal_date_count`, `independent_time_unit_count` e `overlapping_signal_date_count=signal_date_count-independent_time_unit_count`.

Descritivas usam todas as rows válidas. Bootstrap, CIs, comparações inferenciais e readiness temporal usam somente as unidades independentes; bootstrap reamostra unidades com replacement e nunca rows individuais.

## Métricas e roles

`absolute` reporta count, mean/median de `aligned_asset_return_pct`, positive return rate, median MFE/MAE e stop/target 2R/target 3R touch rates. MFE, MAE e touches são métricas da 3B.2 ancoradas em `ideal_entry`; não são chamadas de aligned ou market-relative.

`market_relative` reporta count, mean/median de `primary_excess_aligned_price_return_pct` e `benchmark_outperformance_rate`, definido por excesso estritamente maior que zero. Zero é empate; não há termo alpha.

`role_summaries` mantém separados os roles existentes: `trade_candidate`, `conditional_candidate`, `observational_candidate`, `observational_wait`, `observational_avoid`, `observational_blocked` e `observational_other`. Nenhuma classe é invertida; avoid positivo continua positivo.

Comparações pré-registradas, somente estas e nessa ordem: `trade_candidate_vs_conditional_candidate`, `trade_candidate_vs_observational_wait` e `trade_candidate_vs_observational_avoid`. O sinal é sempre lhs-rhs; as métricas são `difference_in_median_primary_excess` e `difference_in_mean_primary_excess`. Status: `available`, `missing_group`, `insufficient_group_sample` ou `insufficient_independent_time_units`.

## Spearman

Os únicos scores são `investment_quality_score`, `swing_trade_score`, `decision_confidence_score`, `data_quality_score` e `expected_value_r`. Cada relação reporta `valid_count` e `missing_count`; `expected_value_r=null` é missing, nunca zero.

Spearman usa ranks médios para ties e Pearson entre ranks, sem p-value. Status: `available`, `insufficient_sample` ou `insufficient_variation`. Exige >=20 pares, >=3 scores distintos e >=3 outcomes distintos. Há relação absoluta e, quando primary está disponível, relação de primary excess.

## Bootstrap e CIs

Cada seed é `SHA-256(canonical JSON({statistics_policy_version, input_artifact_hash, cell_key, metric_key}))` convertido deterministicamente para inteiro e usado em `random.Random` privado; `input_artifact_hash` aqui é sempre o hash semântico canonicalizado, nunca o hash raw recebido. Não usa random global, clock, UUID ou entropia externa.

São solicitadas exatamente 10.000 replicates. Uma replicate de comparação sem ambos os grupos é descartada; `bootstrap_replicates_effective` é reportado. O percentile 95% usa `floor(0.025*(n-1))` para o limite inferior e `ceil(0.975*(n-1))` para o superior. Com menos de 4 unidades independentes com evidência útil, CIs são `null` e effective é zero.

CIs obrigatórios: mean primary excess, median primary excess, benchmark outperformance rate, as duas diferenças pré-registradas e Spearman primary excess. CI não é causalidade.

## Readiness

Cada cell reporta `market_relative_readiness` e `absolute_sample_status`. O status absoluto somente pode ser `INSUFFICIENT` ou `EXPLORATORY`; retorno absoluto sozinho nunca é `EVALUATION_READY`.

Market-relative é `INSUFFICIENT` se `independent_time_unit_count < 4`, `primary_eligible_unique_asset_date_count < 30`, `primary_available_unique_asset_date_count < 30`, `primary_coverage < 0.90` ou `primary_eligible_count == 0`.

É `EXPLORATORY` quando não é ready e há >=4 unidades, >=50 asset-dates elegíveis, >=50 disponíveis, coverage >=0.90 e >=2 regimes com >=10 asset-dates cada. Regime é `stock_regime` para stock e `crypto_regime` para crypto.

É `EVALUATION_READY` somente com >=12 unidades, >=200 asset-dates elegíveis, >=200 disponíveis, coverage >=0.95, >=2 regimes com >=30 asset-dates cada e uma comparação pré-registrada disponível com >=50 asset-dates em cada lado.

`overall_market_relative_readiness` é o pior estado entre cells com pelo menos uma row primary elegível, na ordem `INSUFFICIENT < EXPLORATORY < EVALUATION_READY`; sem cells elegíveis é `INSUFFICIENT`. `statistics_status` é igual ao overall.

Mesmo em `EVALUATION_READY`, `calibration_authorized=false`. O output não produz threshold, weight, gate, cutoff, score otimizado ou recomendação.

## Limites e sanitização

Não há random split, train, validation, test ou walk-forward optimization nesta fase. Uma futura 3B.4, se autorizada, deverá usar walk-forward cronológico com purge/embargo de 40 barras.

O artifact não contém input/output paths, filesystem paths, secrets, URLs, headers, credenciais, exceptions ou metadata externa. A fase não altera autoridade financeira, sizing, blockers, seleção, relatório, Telegram, runtime, workflows ou 3B.1/3B.2/3B.3.1.
