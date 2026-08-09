# FASE 3B.1 — Canonical Signal Observation Ledger

Status: implementação da fonte canônica de observações para `report main` e
`report close`. Esta fase registra decisões e sua proveniência no instante da
execução; não calcula nem atualiza outcomes.

## Autoridade e separação

`signal_observations` é a autoridade histórica nova. O `signal_journal` é
legado: permanece legível e continua disponível para `advisor scan` e para o
caminho legado `signals update-results`, mas não recebe novas linhas de
`report main|close` e não é usado pelo ledger.

`advisor scan` não escreve `signal_observations`. Somente o fluxo
`advisor report main` ou `advisor report close`, depois de scoring,
classification e atribuição de `universe_origin`, persiste as mesmas
`AssetDecision` e `AssetSnapshot` já produzidas. A persistência não rescora,
reclassifica, consulta provider ou altera report, Final Review, Telegram,
risco, sizing ou qualquer decisão financeira.

## Metadata da execução

Cada execução canônica compartilha um único `signal_timestamp_utc`, em ISO-8601
UTC explícito, e deriva `report_date_brt` desse instante. O timezone lógico
persistido é `America/Sao_Paulo`; a conversão atual usa UTC-03:00, a convenção
existente no projeto e vigente sem DST no Brasil desde 2019.

| Campo | Regra |
| --- | --- |
| `schema_version` | exatamente `1.0` |
| `source_sha` | hexadecimal lowercase com exatamente 40 ou 64 caracteres; sem placeholder e sem trim permissivo |
| `run_id` | `GITHUB_RUN_ID` numérico quando disponível; caso contrário `local-<uuid4 hex>` por invocation |
| `run_origin` | `github` ou `local` |
| `report_type` | `main` ou `close` |
| `signal_timestamp_utc` | capturado uma vez por execução e compartilhado pelos assets |
| `report_date_brt` | data BRT derivada de `signal_timestamp_utc` |

Se o source SHA não puder ser resolvido, nenhuma observação canônica é
inserida. O report continua e emite somente
`signal_observation_status=unavailable error_code=source_sha_unavailable`.

## Identidade e hashes

A identidade lógica é o objeto JSON com exatamente estas chaves:

```json
{"report_type":"main|close","run_id":"...","schema_version":"1.0","source_sha":"...","symbol":"..."}
```

`signal_id = SHA-256(UTF-8(canonical_json(identity)))`, em hexadecimal
lowercase. A serialização canônica usa `sort_keys=True`, separadores
`(',', ':')`, UTF-8, `ensure_ascii=False` e `allow_nan=False`.

`observation_hash` usa a mesma serialização sobre o conteúdo financeiro
imutável da observação, exceto `observation_hash`, `persisted_at_utc` e
`signal_timestamp_utc`. O `signal_timestamp_utc` é metadata de captura da
invocation: em retry técnico ele pode ser recapturado sem que decisão, dados
point-in-time ou proveniência tenham mudado. A linha original preserva o
primeiro timestamp e nunca é atualizada. Portanto o hash não depende de row
id, caminho do DB, ordem de inserção ou momento da invocation/INSERT; muda
quando muda qualquer conteúdo de decisão, risco, dados point-in-time, contexto
ou proveniência permitida.

## Schema canônico

`signal_observations.signal_id` é a primary key. Também existe uma restrição
unique independente em:

```text
(source_sha, run_id, report_type, asset)
```

As colunas são:

```text
signal_id, schema_version, source_sha, run_id, run_origin,
report_date_brt, report_type, signal_timestamp_utc,
asset, asset_type, universe_origin, market_session, market_timezone,
decision_label, bucket, investment_quality_score, swing_trade_score,
decision_confidence_score, data_quality_score, expected_value_r,
backtest_sample_size, sample_quality, data_quality, missing_data_severity,
ideal_entry, alternative_entry, entry_semantics,
alternative_entry_semantics, stop, target_2r, target_3r, per_unit_risk,
risk_amount, risk_fraction, max_position_units, max_position_value,
reason_codes, data_source, data_timestamp, last_price_timestamp, provider,
is_stale, stock_regime, crypto_regime, relative_strength_vs_spy,
relative_strength_vs_qqq, relative_strength_vs_sector, sector_benchmark,
evaluation_role, provenance_json, observation_hash, persisted_at_utc
```

Não existem colunas de outcome nesta tabela: não há `return_*`, `hit_*`,
`realized_r`, `mfe`, `mae` ou `exit_price`.

Constraints fecham `schema_version`, `report_type`, `asset_type`,
`run_origin`, `market_timezone`, `evaluation_role`, semânticas de entrada e
booleano `is_stale` como `0|1`. Triggers rejeitam qualquer `UPDATE` ou
`DELETE`; a tabela é append-only.

## Semântica da decisão

`ideal_entry` é uma referência do último close usado pelo modelo, não um fill.
Toda observação usa `entry_semantics=reference_close_not_fill`. Quando existe
`alternative_entry`, usa `alternative_entry_semantics=conditional_untracked`;
sem alternativa usa `not_present`. Nenhuma linha representa execução
comprovada.

`evaluation_role` é descritivo e nunca entra no scoring:

```text
tradeable              -> trade_candidate
watch_buy              -> conditional_candidate
technical_unvalidated  -> observational_candidate
wait                   -> observational_wait
avoid                  -> observational_avoid
blocked                -> observational_blocked
outro                  -> observational_other
```

O timezone do ativo é `America/New_York` para stocks e `UTC` para crypto.

## Proveniência

`provenance_json` é JSON determinístico, UTF-8, com chaves ordenadas e
`allow_nan=False`. A allowlist atual é:

```text
data_source, data_timestamp, last_price_timestamp, provider,
cache_age_seconds, quote_status, quote_timestamp, quote_source,
quote_age_seconds, quote_is_intraday, fetched_at, cache_fetched_at,
source_timestamp, source_age_seconds, cache_hit, fallback_used,
fallback_from, fallback_to, granularity, market_data_kind
```

Endpoint, querystring, path, headers, Authorization/Bearer, environment,
exception e payload bruto de provider não são copiados. Valores de texto com
esses marcadores são omitidos da proveniência ou normalizados para
`unknown` nos campos textuais da observação.

## Insert, retry e falha

`SQLiteCache.save_signal_observations` prepara todos os rows e executa um único
batch em `BEGIN IMMEDIATE`:

| Situação | Resultado |
| --- | --- |
| identidade nova | insert; `written` |
| identidade existente e hash igual | no-op; `duplicate_same` |
| identidade existente e hash diferente | rollback integral; `conflict` |
| erro de serialização ou storage | rollback integral; `unavailable` |

Não há `INSERT OR REPLACE`, `UPDATE ON CONFLICT` ou last-writer-wins. Um
conflito ou erro no segundo asset não deixa o primeiro asset do batch gravado.
Falhas do ledger são fail-open para a execução financeira e só expõem status
diagnóstico sanitizado no stdout.

Um rerun/retry GitHub conserva `GITHUB_RUN_ID`; `GITHUB_RUN_ATTEMPT` não entra
em `signal_id`. Assim, clocks de captura diferentes com o mesmo conteúdo
financeiro resultam em `duplicate_same`, enquanto qualquer divergência de
decisão, risco, dados point-in-time ou proveniência resulta em `conflict`.

## Cross-date GitHub retry

`report_date_brt` continua persistido, `NOT NULL` e dentro do
`observation_hash`, mas nao faz parte de `signal_id` nem da unique identity.
Assim, o mesmo `source_sha + run_id + report_type + asset` conserva o mesmo
`signal_id`. Um retry na mesma data com payload imutavel igual e apenas
`persisted_at_utc` diferente e `duplicate_same`; se a tentativa mudar
`report_date_brt`, o hash muda e o resultado e `conflict`, preservando a
primeira row. `GITHUB_RUN_ATTEMPT` nao participa da identidade.

## Escopo futuro

Outcomes, candles futuros, benchmark futuro, fills, MFE/MAE, realized R,
backfill, calibration e `signals update-results` não fazem parte deste
contrato. Pertencem a fases posteriores e não podem mutar esta tabela.
