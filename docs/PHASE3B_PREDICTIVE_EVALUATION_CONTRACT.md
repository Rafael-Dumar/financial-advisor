# FASE 3B.3.1 — Benchmark-Aligned Evaluation Dataset

Status: implementação isolada sobre os ledgers congelados de 3B.1 e 3B.2.
Este contrato constrói unidades determinísticas de avaliação; não calcula
qualquer estatística agregada, não altera autoridade financeira e não inicia a
3B.3.2.

## Entrada e execução

O módulo é executável sem integração com `scan`, `report`, Final Review,
Telegram, runtime, workflows ou provider loaders:

```text
python -m advisor.predictive_evaluation \
  --db <path> \
  --benchmark-input-path <json> \
  --output-path <file>
```

O DB é aberto exclusivamente com URI SQLite `mode=ro` e a conexão executa
`PRAGMA query_only=ON`. O evaluator não cria schema, não usa `SQLiteCache` e
não escreve no DB. DB SQLite inválido ou corrompido retorna código diferente
de zero. Se `signal_observations` ou `signal_forward_outcomes` estiver
ausente, a ausência é um estado válido e produz amostra zero.

O único input de benchmark é um JSON local:

```json
{
  "schema_version": "1.0",
  "benchmarks": {
    "SPY": {
      "asset_type": "stock",
      "provider": "fixture",
      "price_basis": "split_adjusted_ohlc",
      "candles": [
        {"date":"2026-08-09","open":200,"high":202,"low":199,"close":201,"volume":1000}
      ]
    }
  }
}
```

Cada candle exige data `YYYY-MM-DD`, preços positivos, volume não negativo,
números finitos, ordenação OHLC válida e data única. A série é canonicalizada
em ordem crescente. Nenhuma chamada de rede é feita.

### Validade global e por benchmark

Erros estruturais do documento inteiro — JSON não parseável, top-level que não
é object, `schema_version` inválida, `benchmarks` ausente ou `benchmarks` que
não é object — invalidam globalmente o input. Nesses casos, qualquer benchmark
solicitado recebe `invalid_benchmark_input`, preservando o estado global de
input inválido.

Depois que o documento é estruturalmente válido, cada símbolo é validado
independentemente. Uma entrada que não é object, tem asset type/candles/candle
estruturalmente inválidos, provider ou price basis estruturalmente inválidos,
ou contém datas, números ou OHLCV inválidos torna
somente aquele símbolo `invalid_benchmark_input`; os demais continuam
utilizáveis. O catálogo mantém, por símbolo, uma série parseada ou uma marca
de série inválida, sem um booleano que contamine benchmarks independentes.

Símbolo ausente no documento continua sendo `benchmark_missing`. Uma série
presente mas inválida não é tratada como ausente. Uma série estruturalmente
válida com basis semanticamente incompatível continua sendo
`incompatible_price_basis` — por exemplo, `SPY` com `price_basis="raw_ohlcv"`
em uma comparação stock — e não `invalid_benchmark_input`.

## Binding canônico

Cada outcome é aceito somente quando existe observation com o mesmo
`signal_id` e quando `outcome.observation_hash == observation.observation_hash`.
Outcomes órfãos ou estruturalmente incompatíveis não geram row. Os hashes
persistidos de 3B.1/3B.2 são usados como binding; o evaluator não reconstrói
esses hashes.

O ativo é sempre lido exclusivamente de `asset_bars_json` do outcome. A série
é válida somente se o JSON for canônico, tiver exatamente `horizon_bars` barras,
datas crescentes, primeira data igual a `horizon_start_date`, última data igual
a `horizon_end_date` e SHA-256 dos bytes canônicos igual a `asset_bars_hash`.
Falha nessa validação produz `row_status="invalid_asset_outcome"` e todos os
retornos alinhados ficam nulos.

## Policy e âncoras

```text
schema_version              = "1.0"
benchmark_policy_version    = "1.0"
evaluation_policy_version   = valor persistido no outcome
anchor_policy_version       = "first_forward_open_to_horizon_close_v1"
```

`aligned_asset_return_pct` usa somente o open da primeira barra do outcome e
o close da última barra:

```text
(last_asset_close / first_asset_open) - 1
```

`forward_return_pct` da 3B.2 é preservado como campo descritivo separado e
nunca é sobrescrito. `cash_zero_reference_return_pct` é sempre `0.0` e
`absolute_positive_return` é exatamente `aligned_asset_return_pct > 0`.

As classes `trade_candidate`, `conditional_candidate`,
`observational_candidate`, `observational_wait`, `observational_avoid`,
`observational_blocked` e `observational_other` usam a mesma matemática.
Nenhuma classe é invertida para representar posição vendida.

## Price basis

A policy fecha os valores esperados sem aliases ou inferência por provider:

```text
stock  -> split_adjusted_ohlc
crypto -> raw_ohlcv
```

Para comparação disponível, o basis do asset outcome, do benchmark e o basis
esperado da classe precisam ser iguais. `unknown`, `raw`, `adjusted`,
`unadjusted`, `raw_unadjusted` ou texto arbitrário não são normalizados. Falha
produz `incompatible_price_basis` e nulos nos campos de benchmark alinhado e
de diferença.

## Primary e secondary

O primary é escolhido pela classe e pelo símbolo, antes de observar qualquer
retorno:

```text
stock  asset != SPY -> SPY
crypto asset != BTC -> BTC
SPY ou BTC          -> null, self_benchmark_unavailable
```

Ausência do símbolo primary produz `benchmark_missing`; a row permanece e não
há substituição por secondary.

O secondary existe somente para stocks, a partir de
`observation.sector_benchmark`, se o valor literal estiver em `SMH`, `IGV`,
`QQQ`, `XLV` e for diferente do asset. Crypto sempre recebe
`not_applicable`. Primary e secondary são validados somente contra o símbolo
solicitado: benchmark inválido não contamina o outro. Secondary é diagnóstico
independente e nunca vira primary, mesmo quando o primary está ausente ou
inválido.

Os estados primary são exatamente:

```text
available
benchmark_missing
self_benchmark_unavailable
incompatible_price_basis
missing_required_dates
invalid_benchmark_input
```

Os estados secondary são exatamente os estados acima mais:

```text
not_applicable
not_recorded
not_allowlisted
```

## Alinhamento e evidência

As datas requeridas são exatamente as datas de `asset_bars_json`. O benchmark
precisa conter todas elas. Não existe nearest-date, seleção por posição,
interpolação, forward-fill ou backfill. Datas adicionais são aceitas e
ignoradas na evidência da row.

Quando disponível, o evaluator seleciona somente as candles do benchmark que
possuem essas datas, em ordem crescente, e persiste:

```text
<prefix>_benchmark_bars_json
<prefix>_benchmark_bars_hash
<prefix>_benchmark_start_date
<prefix>_benchmark_end_date
<prefix>_benchmark_start_open
<prefix>_benchmark_end_close
<prefix>_benchmark_aligned_return_pct
<prefix>_excess_aligned_price_return_pct
```

O hash é SHA-256 dos bytes UTF-8 do JSON canônico compacto, com chaves
ordenadas, `allow_nan=false` e exatamente as datas requeridas. O retorno do
benchmark usa a mesma âncora:

```text
(benchmark_end_close / benchmark_start_open) - 1
```

A diferença só existe com status primary `available`:

```text
primary_excess_aligned_price_return_pct =
    aligned_asset_return_pct - primary_benchmark_aligned_return_pct
```

Secondary produz o campo equivalente separado
`secondary_excess_aligned_price_return_pct`. A terminologia da row é
`excess_aligned_price_return`; não há decisão, sizing ou ajuste de parâmetros.

## Identidade e hashes

`evaluation_row_id` é SHA-256 do JSON canônico com exatamente:

```json
{
  "schema_version": "1.0",
  "benchmark_policy_version": "1.0",
  "anchor_policy_version": "first_forward_open_to_horizon_close_v1",
  "signal_id": "...",
  "observation_hash": "...",
  "outcome_id": "...",
  "outcome_hash": "...",
  "horizon_bars": 5
}
```

Performance, caminhos, clock, output path e DB path não participam da
identidade. `evaluation_row_hash` cobre o conteúdo imutável serializado da
row, incluindo observation/outcome metadata, hashes, scores, role, regimes,
asset evidence, benchmark policy/status/evidence, retornos, diferenças e
cash-zero fields. Somente o próprio `evaluation_row_hash` é removido do
payload do hash; `persisted_at_utc` não é emitido.

## Artifact e coverage

O artifact contém somente:

```json
{
  "schema_version": "1.0",
  "benchmark_policy_version": "1.0",
  "anchor_policy_version": "...",
  "dataset_status": "...",
  "coverage": {},
  "rows": [],
  "artifact_hash": "..."
}
```

O JSON é UTF-8, LF, compacto, com chaves ordenadas e exatamente uma quebra de
linha final. `artifact_hash` é SHA-256 do conteúdo canônico excluindo somente
`artifact_hash`. Não existe timestamp de geração.

`coverage` contém apenas fatos:

```text
observations_total
outcomes_total
rows_total
primary_benchmark_available
primary_benchmark_unavailable
secondary_benchmark_available
secondary_benchmark_unavailable
by_horizon
by_asset_type
by_report_type
by_evaluation_role
```

Os únicos estados de dataset são:

```text
NO_CANONICAL_SAMPLE
CANONICAL_SAMPLE_NO_VALID_BENCHMARK
CANONICAL_EVALUATION_ROWS_AVAILABLE
```

Sem ledgers, com ledgers vazios ou sem binding canônico válido, `rows=[]` e o
estado é `NO_CANONICAL_SAMPLE`, com exit code zero. Rows canônicas sem primary
disponível usam `CANONICAL_SAMPLE_NO_VALID_BENCHMARK`, mesmo que alguma row
tenha secondary disponível. Basta uma row com primary `available` para usar
`CANONICAL_EVALUATION_ROWS_AVAILABLE`.

## Exclusões e sanitização

Esta fase não calcula médias, medianas, taxas agregadas, score buckets,
correlações, bootstrap, intervalos, blocos temporais, readiness, calibration,
threshold optimization, train/validation/test, recomendação ou ajuste de
score, gate, confidence, sizing ou expected value.

O artifact não contém paths absolutos, secrets, Authorization, Bearer, API
keys, URLs/querystrings, headers, payload bruto ou mensagens de falha. Textos
de metadata são bounded e sanitizados. A publicação, stage, commit, push e a
3B.3.2 estão fora do escopo desta fase.
